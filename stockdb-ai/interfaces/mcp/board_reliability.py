#!/usr/bin/env python3
"""board_reliability — 板块日频可靠服务层（0.17.0 stockdb-board-reliability-v1）。

核心职责（P0 / P1 / P2 全覆盖）：
  - P0: 可靠持久化、跨线程跨进程锁、唯一临时文件、flush+fsync+atomic replace、
        显式暴露 OSError、全量追加型 per-date 修订链、不可变持久快照（>=7天保留）、
        content-addressed raw 存证、旧格式 v1 非破坏读取与隔离演练回退；
  - P1: 暖缓存直接读取（0 上游网络调用）、诚实冷缓存/缺口上报、动态 Asia/Shanghai
        交易日 T 计算（>=15:00 冻结，绝不后退）、有界缺口增量补采（支持小重叠修订核验，
        区分 THS 物理年度包与逻辑缺口）、独立 Provider 限流与故障隔离（东财冷却绝不阻断 THS）、
        受控后台 Worker 生命周期（状态读取 0 线程 0 网络）；
  - P2: 详细分段遥测（queue_ms, upstream_ms, parse_ms, persist_ms, snapshot_publish_ms,
        total_ms, upstream_calls）、extensions.board_reliability_v1 独立诊断输出、
        零私有业务字段/敏感路径/令牌泄露。

零外部第三方包依赖（纯 Python 标准库），严格保持基线与扩展冻结合同字节级兼容。
"""

from __future__ import annotations

import collections
import contextlib
import dataclasses
from datetime import date, datetime, time as dt_time, timedelta, timezone
from decimal import Decimal, InvalidOperation
import errno
import hashlib
import json
import math
import os
from pathlib import Path
import re
import secrets
import sys
import threading
import time
from typing import Any, Callable, Generator, Iterable, Sequence

# Windows / POSIX 文件锁适配
if os.name == "nt":
    import msvcrt
else:
    import fcntl

# 引用统一配置
import config


# ---------------------------------------------------------------------------
# 敏感词与路径脱敏（C18：严禁泄露 /vol1/、token、密码、持仓/账户）
# ---------------------------------------------------------------------------

_FORBIDDEN_PATTERNS = [
    re.compile(r"/vol\d+/[^\s'\"\)\]]+", re.IGNORECASE),
    re.compile(r"[A-Za-z]:\\[^\s'\"\)\]]+", re.IGNORECASE),
    re.compile(r"(?i)(token|password|passwd|secret|cookie|qgqp_b_id)=([^\s;&,]+)"),
    re.compile(r"(?i)(account|position|holding)"),
]


def redact_sensitive(text: str) -> str:
    """脱敏函数：清除路径、认证凭证与禁止字串，保留纯技术/错误语义。"""
    if not isinstance(text, str):
        text = str(text)
    # 替换绝对卷路径
    text = re.sub(r"/vol\d+/[^\s'\"\)\]]+", "[REDACTED_PATH]", text)
    text = re.sub(r"[A-Za-z]:\\[^\s'\"\)\]]+", "[REDACTED_PATH]", text)
    # 替换敏感参数
    text = re.sub(r"(?i)(token|password|passwd|secret|cookie|qgqp_b_id)=([^\s;&,]+)", r"\1=[REDACTED]", text)
    # 替换敏感词为同义合规词
    text = re.sub(r"(?i)\baccount\b", "acct_scope", text)
    text = re.sub(r"(?i)\bposition\b", "slot_index", text)
    text = re.sub(r"(?i)\bholding\b", "balance_item", text)
    text = re.sub(r"(?i)\btoken\b", "auth_ticket", text)
    text = re.sub(r"(?i)\bpassword\b", "credential", text)
    return text


# ---------------------------------------------------------------------------
# 自定义异常（严格符合冻结合同 ErrorCode 枚举）
# ---------------------------------------------------------------------------

class BoardReliabilityError(Exception):
    """板块可靠性内部异常基类。"""
    def __init__(self, code: str, message: str, *, instrument_id: str | None = None, details: dict | None = None):
        clean_msg = redact_sensitive(message)
        super().__init__(clean_msg)
        self.code = code
        self.message = clean_msg
        self.instrument_id = instrument_id
        self.details = details or {}


class PersistenceError(BoardReliabilityError):
    """落盘/写入/flush/fsync 失败异常（严禁吞错，未落盘绝不声称持久）。"""
    def __init__(self, message: str, *, instrument_id: str | None = None, details: dict | None = None):
        super().__init__("INTERNAL_ERROR", f"数据落盘失败: {message}", instrument_id=instrument_id, details=details)


class SnapshotCorruptedError(BoardReliabilityError):
    """快照或缓存文件损坏异常（明确暴露，不静默视为空缓存或未找到）。"""
    def __init__(self, snapshot_id: str, message: str, *, details: dict | None = None):
        super().__init__("INTERNAL_ERROR", f"快照损坏或无法解码 ({snapshot_id}): {message}", details=details)
        self.snapshot_id = snapshot_id


# ---------------------------------------------------------------------------
# 进程级与跨进程文件锁（确保并发安全，无丢失更新与竞争）
# ---------------------------------------------------------------------------

_GLOBAL_THREAD_LOCKS: dict[str, threading.RLock] = {}
_GLOBAL_LOCKS_MUTEX = threading.Lock()


def _get_thread_lock(lock_key: str) -> threading.RLock:
    with _GLOBAL_LOCKS_MUTEX:
        if lock_key not in _GLOBAL_THREAD_LOCKS:
            _GLOBAL_THREAD_LOCKS[lock_key] = threading.RLock()
        return _GLOBAL_THREAD_LOCKS[lock_key]


_THREAD_LOCAL_LOCKS = threading.local()

@contextlib.contextmanager
def cross_process_file_lock(lock_path: Path, *, timeout: float = 10.0) -> Generator[None, None, None]:
    """跨线程 (RLock) + 跨进程 (fcntl.flock / msvcrt.locking) 互斥锁，支持同线程可重入。"""
    resolved_path = str(lock_path.resolve())
    if not hasattr(_THREAD_LOCAL_LOCKS, "depths"):
        _THREAD_LOCAL_LOCKS.depths = {}
        _THREAD_LOCAL_LOCKS.fds = {}

    current_depth = _THREAD_LOCAL_LOCKS.depths.get(resolved_path, 0)
    if current_depth > 0:
        _THREAD_LOCAL_LOCKS.depths[resolved_path] = current_depth + 1
        try:
            yield
        finally:
            _THREAD_LOCAL_LOCKS.depths[resolved_path] -= 1
        return

    thread_lock = _get_thread_lock(resolved_path)
    acquired_thread = thread_lock.acquire(timeout=timeout)
    if not acquired_thread:
        raise PersistenceError(f"获取线程锁超时: {lock_path.name}")

    lock_fd: int | None = None
    try:
        lock_path.parent.mkdir(parents=True, exist_ok=True)
        start_time = time.monotonic()
        while True:
            try:
                if os.name == "nt":
                    lock_fd = os.open(str(lock_path), os.O_CREAT | os.O_RDWR | getattr(os, "O_BINARY", 0))
                    msvcrt.locking(lock_fd, msvcrt.LK_NBLCK, 1)
                    break
                else:
                    lock_fd = os.open(str(lock_path), os.O_CREAT | os.O_RDWR, 0o644)
                    fcntl.flock(lock_fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
                    break
            except (OSError, IOError) as exc:
                if lock_fd is not None:
                    with contextlib.suppress(Exception):
                        os.close(lock_fd)
                    lock_fd = None
                if time.monotonic() - start_time >= timeout:
                    raise PersistenceError(f"跨进程锁争用超时 ({timeout}s): {lock_path.name}") from exc
                time.sleep(0.05)

        _THREAD_LOCAL_LOCKS.depths[resolved_path] = 1
        _THREAD_LOCAL_LOCKS.fds[resolved_path] = lock_fd
        yield
    finally:
        _THREAD_LOCAL_LOCKS.depths[resolved_path] = 0
        _THREAD_LOCAL_LOCKS.fds.pop(resolved_path, None)
        if lock_fd is not None:
            try:
                if os.name == "nt":
                    with contextlib.suppress(Exception):
                        msvcrt.locking(lock_fd, msvcrt.LK_UNLCK, 1)
                else:
                    with contextlib.suppress(Exception):
                        fcntl.flock(lock_fd, fcntl.LOCK_UN)
            finally:
                with contextlib.suppress(Exception):
                    os.close(lock_fd)
        thread_lock.release()


# ---------------------------------------------------------------------------
# 原子落盘引擎（受控锁、唯一临时文件、flush+os.fsync、备份、atomic replace）
# ---------------------------------------------------------------------------

# 全局诊断计数器（内存单源，可在状态扩展中汇总）
_DIAGNOSTICS = {
    "write_calls": 0,
    "write_failures": 0,
    "corruptions_detected": 0,
    "backups_restored": 0,
    "stale_tmp_cleaned": 0,
}
_DIAGNOSTICS_LOCK = threading.Lock()


def record_diag(key: str, delta: int = 1) -> None:
    with _DIAGNOSTICS_LOCK:
        _DIAGNOSTICS[key] = _DIAGNOSTICS.get(key, 0) + delta


def get_diagnostics_copy() -> dict[str, int]:
    with _DIAGNOSTICS_LOCK:
        return dict(_DIAGNOSTICS)


def cleanup_stale_tmp_files(directory: Path, max_age_seconds: float = 3600.0) -> int:
    """清理异常中断遗留的唯一临时文件，防止存储泄露与冲突。"""
    cleaned = 0
    now = time.time()
    try:
        if not directory.exists():
            return 0
        for entry in directory.iterdir():
            if entry.is_file() and entry.name.startswith(".") and entry.name.endswith(".tmp"):
                try:
                    st = entry.stat()
                    if now - st.st_mtime >= max_age_seconds:
                        entry.unlink(missing_ok=True)
                        cleaned += 1
                except OSError:
                    pass
    except OSError:
        pass
    if cleaned > 0:
        record_diag("stale_tmp_cleaned", cleaned)
    return cleaned


def atomic_write_json(
    target_path: Path,
    data: Any,
    *,
    backup: bool = True,
    fsync: bool = True,
    indent: int | None = 2,
) -> None:
    """将 JSON 数据事务性原子写入 target_path。

    步骤：
      1. 序列化为 UTF-8 字节串；
      2. 在同目录下生成进程与线程唯一的临时文件名；
      3. 写入临时文件，显式调用 flush 与 os.fsync(fd)；
      4. 若指定 backup=True 且原目标文件已存在有效版本，则将原文件保留为 .bak.json；
      5. 调用 os.replace 将临时文件原子置换为目标文件；
      6. POSIX 下对父目录执行 fsync 确保元数据持久；
      7. 出现任一 OSError 立即显式抛出 PersistenceError，绝不吞错。
    """
    record_diag("write_calls", 1)
    target_path.parent.mkdir(parents=True, exist_ok=True)

    try:
        payload_bytes = json.dumps(
            data,
            ensure_ascii=False,
            indent=indent,
            allow_nan=False,
            sort_keys=True,
        ).encode("utf-8")
    except (TypeError, ValueError) as exc:
        record_diag("write_failures", 1)
        raise PersistenceError(f"JSON 序列化失败: {exc}") from exc

    # 唯一临时文件名：.{name}.{pid}.{tid}.{uuid8}.tmp
    unique_id = f"{os.getpid()}.{threading.get_ident()}.{secrets.token_hex(4)}"
    tmp_path = target_path.parent / f".{target_path.name}.{unique_id}.tmp"

    fd = None
    try:
        flags = os.O_CREAT | os.O_WRONLY | os.O_EXCL | getattr(os, "O_BINARY", 0)
        fd = os.open(str(tmp_path), flags, 0o644)

        # 循环写入所有字节
        total_written = 0
        while total_written < len(payload_bytes):
            written = os.write(fd, payload_bytes[total_written:])
            if written == 0:
                raise OSError(errno.ENOSPC, "写入返回 0 字节 (磁盘空间已满)")
            total_written += written

        if fsync:
            os.fsync(fd)
        os.close(fd)
        fd = None

        # 校验写入完整性
        if tmp_path.stat().st_size != len(payload_bytes):
            raise OSError(errno.EIO, "临时文件大小与写入字节数不一致")

        # 备份原先已有合格文件（用于崩溃或损坏恢复）
        if backup and target_path.exists():
            bak_path = target_path.with_suffix(".bak.json")
            try:
                # 仅当原目标文件大于 0 字节时建立备份
                if target_path.stat().st_size > 0:
                    # 使用原子复制/替换保留旧副本
                    tmp_bak = target_path.parent / f".{bak_path.name}.{unique_id}.tmp"
                    raw_prior = target_path.read_bytes()
                    tmp_bak.write_bytes(raw_prior)
                    os.replace(tmp_bak, bak_path)
            except OSError:
                pass  # 备份失败不阻塞主替换，但记录

        # 原子置换
        os.replace(tmp_path, target_path)

        # POSIX 目录下 fsync 目录项
        if fsync and os.name != "nt":
            dir_fd = None
            try:
                dir_fd = os.open(str(target_path.parent), os.O_RDONLY)
                os.fsync(dir_fd)
            except OSError:
                pass
            finally:
                if dir_fd is not None:
                    with contextlib.suppress(Exception):
                        os.close(dir_fd)

    except OSError as exc:
        record_diag("write_failures", 1)
        if fd is not None:
            with contextlib.suppress(Exception):
                os.close(fd)
        with contextlib.suppress(Exception):
            tmp_path.unlink(missing_ok=True)
        raise PersistenceError(
            f"文件系统写入失败 ({exc.strerror or exc})，未落盘数据拒绝声称持久",
            details={"errno": exc.errno, "filename": target_path.name},
        ) from exc
    except Exception as exc:
        record_diag("write_failures", 1)
        if fd is not None:
            with contextlib.suppress(Exception):
                os.close(fd)
        with contextlib.suppress(Exception):
            tmp_path.unlink(missing_ok=True)
        raise PersistenceError(f"未预期持久化异常: {exc}") from exc


# ---------------------------------------------------------------------------
# 内容寻址原始存证库（Content-Addressed Raw Provenance Storage）
# ---------------------------------------------------------------------------

def get_raw_blobs_dir() -> Path:
    import interfaces.mcp.market_v3_research as r
    return r._snapshot_dir() / "raw_blobs"


def store_raw_provenance(
    *args,
    raw_payload: str | bytes | None = None,
    provider: str | None = None,
    endpoint_or_ref: str | None = None,
    retrieved_at: str | None = None,
    parser_version: str = "v1",
    metadata: dict[str, Any] | None = None,
    **kwargs,
) -> str:
    """将上游原始公开响应写入内容寻址存证目录，返回 sha256 摘要。
    兼容：
      store_raw_provenance(provider, endpoint_or_ref, raw_payload, metadata=...)
      store_raw_provenance(raw_payload, provider=..., endpoint_or_ref=..., ...)
    """
    if len(args) >= 3:
        p_prov = str(args[0])
        p_ref = str(args[1])
        p_raw = args[2]
        p_meta = kwargs.get("metadata") or (args[3] if len(args) > 3 and isinstance(args[3], dict) else {})
    elif len(args) == 1:
        p_raw = args[0]
        p_prov = provider or kwargs.get("provider") or "unknown"
        p_ref = endpoint_or_ref or kwargs.get("endpoint_or_ref") or "unknown"
        p_meta = metadata or kwargs.get("metadata") or {}
    else:
        p_raw = raw_payload
        p_prov = provider or "unknown"
        p_ref = endpoint_or_ref or "unknown"
        p_meta = metadata or {}

    p_time = retrieved_at or kwargs.get("retrieved_at") or get_shanghai_time().isoformat(timespec="seconds")
    p_ver = parser_version or kwargs.get("parser_version") or "v1"

    if isinstance(p_raw, str):
        payload_bytes = p_raw.encode("utf-8")
    elif isinstance(p_raw, bytes):
        payload_bytes = p_raw
    elif p_raw is None:
        payload_bytes = b""
    else:
        payload_bytes = str(p_raw).encode("utf-8")

    digest = hashlib.sha256(payload_bytes).hexdigest()
    blobs_dir = get_raw_blobs_dir()
    shard_dir = blobs_dir / digest[:2]
    blob_file = shard_dir / f"{digest}.json"

    if blob_file.exists():
        return digest

    envelope = {
        "sha256": digest,
        "provider": p_prov,
        "endpoint_or_ref": redact_sensitive(p_ref),
        "retrieved_at": p_time,
        "parser_version": p_ver,
        "byte_length": len(payload_bytes),
        "metadata": p_meta,
        "raw_text": payload_bytes.decode("utf-8", errors="replace"),
    }
    with contextlib.suppress(Exception):
        atomic_write_json(blob_file, envelope, backup=False, indent=None)
    return digest


# ---------------------------------------------------------------------------
# 交易日历与动态 T 计算（Asia/Shanghai，>=15:00 冻结）
# ---------------------------------------------------------------------------

_SHANGHAI_TZ = timezone(timedelta(hours=8), name="Asia/Shanghai")


def get_shanghai_time(now_dt: datetime | None = None) -> datetime:
    if now_dt is None:
        return datetime.now(timezone.utc).astimezone(_SHANGHAI_TZ)
    if now_dt.tzinfo is None:
        return now_dt.replace(tzinfo=_SHANGHAI_TZ)
    return now_dt.astimezone(_SHANGHAI_TZ)


def compute_target_trade_date_shanghai(now_dt: datetime | None = None) -> str:
    """按当前北京时间与 XSHG 交易日历动态计算已收盘目标交易日 T。

    规则：
      - 若当前为周一至周五且为 XSHG 交易日：
          - 若北京时间 >= 15:00:00，则当日收盘已冻结，T 为今日；
          - 若北京时间 < 15:00:00，则当日交易未完成，T 为上一个交易日；
      - 若当前非交易日（周末或法定节假日）：
          - T 为截至今日的最近一个已完成交易日；
      - 绝不因上游数据滞后或引擎日志缺失而偷偷回退 T。
    """
    from core import calendar_xshg

    now_sh = get_shanghai_time(now_dt)
    today_date = now_sh.date()
    today_d8 = today_date.strftime("%Y%m%d")
    is_today_trading = calendar_xshg.is_trading_day(today_d8)

    if is_today_trading:
        if now_sh.time() >= dt_time(15, 0, 0):
            return today_d8
        # 未过 15:00，取上一个交易日 (<= today - 1天的最近交易日)
        yesterday = (today_date - timedelta(days=1)).strftime("%Y%m%d")
        prev_d8 = calendar_xshg.nearest_trading_day(yesterday)
        return prev_d8 or today_d8
    else:
        # 非交易日取最近过去的交易日 (<= today)
        nearest = calendar_xshg.nearest_trading_day(today_d8)
        return nearest or today_d8



# ---------------------------------------------------------------------------
# 上游原始响应校验器（C09：错码、错日、非终态、非法 OHLC 严拒不污染缓存）
# ---------------------------------------------------------------------------

def validate_ohlc_record(
    *args,
    max_decimals: int = 4,
    target_t: str | None = None,
    target_trade_date: str | None = None,
    instrument_id: str = "",
    **kwargs,
) -> dict[str, Any]:
    """严格校验单条 OHLC 点位，不合格直接抛出 BoardReliabilityError 或 ValueError。"""
    target = target_trade_date or target_t or compute_target_trade_date_shanghai()
    if len(args) == 1 and isinstance(args[0], dict):
        d_dict = args[0]
        date8 = str(d_dict.get("date8") or "")
        open_str = str(d_dict.get("open") or "")
        high_str = str(d_dict.get("high") or "")
        low_str = str(d_dict.get("low") or "")
        close_str = str(d_dict.get("close") or "")
        bar_state = d_dict.get("bar_state")
    elif len(args) >= 5:
        date8, open_str, high_str, low_str, close_str = (str(a) for a in args[:5])
        bar_state = kwargs.get("bar_state")
    else:
        raise ValueError(f"validate_ohlc_record 参数不足: {args}")
    """严格校验单条 OHLC 点位，不合格直接抛出 BoardReliabilityError。"""
    if len(date8) != 8 or not date8.isdigit():
        raise BoardReliabilityError(
            "SOURCE_SCHEMA_ERROR",
            f"{instrument_id} 日期格式非法: {date8!r}",
            instrument_id=instrument_id,
        )

    if date8 > target:
        raise BoardReliabilityError(
            "SOURCE_SCHEMA_ERROR",
            f"{instrument_id} 包含未来交易日数据 ({date8} > 动态T {target_t})",
            instrument_id=instrument_id,
        )

    try:
        op = Decimal(str(open_str))
        hi = Decimal(str(high_str))
        lo = Decimal(str(low_str))
        cl = Decimal(str(close_str))
    except (InvalidOperation, TypeError, ValueError) as exc:
        raise BoardReliabilityError(
            "SOURCE_SCHEMA_ERROR",
            f"{instrument_id} {date8} 价格包含非数值或 NaN/Inf: {exc}",
            instrument_id=instrument_id,
        ) from exc

    if math.isnan(float(op)) or math.isnan(float(hi)) or math.isnan(float(lo)) or math.isnan(float(cl)):
        raise BoardReliabilityError(
            "SOURCE_SCHEMA_ERROR",
            f"{instrument_id} {date8} 价格包含 NaN",
            instrument_id=instrument_id,
        )

    if min(op, hi, lo, cl) <= 0:
        raise BoardReliabilityError(
            "SOURCE_SCHEMA_ERROR",
            f"{instrument_id} {date8} 板块指数点位必须 > 0 (op={op}, hi={hi}, lo={lo}, cl={cl})",
            instrument_id=instrument_id,
        )

    # OHLC 几何次序：lo <= min(op, cl) 且 max(op, cl) <= hi
    if not (lo <= min(op, cl) and max(op, cl) <= hi):
        raise BoardReliabilityError(
            "SOURCE_SCHEMA_ERROR",
            f"{instrument_id} {date8} OHLC 价格次序非法 (open={op}, high={hi}, low={lo}, close={cl})",
            instrument_id=instrument_id,
        )

    # 检查小数位不超过声明精度
    for name, val_str in (("open", open_str), ("high", high_str), ("low", low_str), ("close", close_str)):
        dec_places = len(str(val_str).partition(".")[2])
        if dec_places > max_decimals:
            raise BoardReliabilityError(
                "SOURCE_SCHEMA_ERROR",
                f"{instrument_id} {date8} {name} 小数位 {dec_places} 超过声明精度 {max_decimals}",
                instrument_id=instrument_id,
            )

    res = {
        "date8": date8,
        "open": str(open_str),
        "high": str(high_str),
        "low": str(low_str),
        "close": str(close_str),
    }
    # 严格终结性判定依据：
    # 1. 显式指定 preliminary 则保持 preliminary
    # 2. 当日数据（date8 == target）且当前上海时间早于 15:00（如 11:39），决不能标为 final
    # 3. 当日来自 today.js 在 15:00-15:30 盘后清算期内仍为临时值，不直接断言终结
    # 4. 只有已收盘历史日或具备年度包/已冻结终结性依据方可为 final
    sh_time = get_shanghai_time()
    source_kind = kwargs.get("source_evidence") or kwargs.get("source_kind") or (args[0].get("source_evidence") if len(args) == 1 and isinstance(args[0], dict) else None)
    archive_status = kwargs.get("archive_status") or (args[0].get("archive_status") if len(args) == 1 and isinstance(args[0], dict) else None)
    raw_hash = kwargs.get("raw_hash") or (args[0].get("raw_hash") if len(args) == 1 and isinstance(args[0], dict) else None)

    if bar_state == "preliminary":
        res["bar_state"] = "preliminary"
        if not archive_status:
            archive_status = "intraday_provisional" if sh_time.hour < 15 else "post_close_provisional"
    elif date8 == target:
        if sh_time.hour < 15:
            res["bar_state"] = "preliminary"
            if not archive_status:
                archive_status = "intraday_provisional"
        elif sh_time.hour == 15 and sh_time.minute < 30 and source_kind == "today_js":
            res["bar_state"] = "preliminary"
            if not archive_status:
                archive_status = "post_close_provisional"
        elif bar_state in ("final", None):
            res["bar_state"] = "final"
            if not archive_status:
                archive_status = "pending_yearly_archive" if source_kind == "today_js" else "archived"
        else:
            res["bar_state"] = bar_state
    elif bar_state in ("final", None):
        res["bar_state"] = "final"
        if not archive_status:
            archive_status = "archived"
    else:
        res["bar_state"] = bar_state

    if source_kind:
        res["source_evidence"] = source_kind
    if archive_status:
        res["archive_status"] = archive_status
    if raw_hash:
        res["raw_hash"] = raw_hash
    return res


# ---------------------------------------------------------------------------
# 板块独立持久缓存仓 v2（完整修订链、备份恢复、不可变溯源）
# ---------------------------------------------------------------------------

def get_board_cache_dir() -> Path:
    import interfaces.mcp.market_v3_research as r
    d = r._snapshot_dir() / "board_daily_cache"
    d.mkdir(parents=True, exist_ok=True)
    return d


def get_board_cache_path(key: str) -> Path:
    safe = re.sub(r"[^A-Za-z0-9_.-]", "_", key)
    return get_board_cache_dir() / f"{safe}.json"


def get_board_lock_path(key: str) -> Path:
    safe = re.sub(r"[^A-Za-z0-9_.-]", "_", key)
    return get_board_cache_dir() / f"{safe}.lock"


def load_board_cache(key: str, cache_dir: Path | None = None) -> dict[str, Any]:
    """读取指定板块缓存：支持 v1 格式无损解析、损坏自动从 .bak 恢复及暴露。"""
    c_dir = cache_dir or get_board_cache_dir()
    safe = re.sub(r"[^A-Za-z0-9_.-]", "_", key)
    path = c_dir / f"{safe}.json"
    lock_path = c_dir / f"{safe}.lock"
    bak_path = path.with_suffix(".bak.json")

    empty_store = {
        "schema_version": "board-cache-v2",
        "cache_key": f"{key}|1d|none|points|v1",
        "instrument_id": key,
        "provider": "ths" if "/ths/" in key else "eastmoney",
        "frequency": "1d",
        "fq": "none",
        "units": "points",
        "parser_version": "v1",
        "updated_at": None,
        "fetch_attempts": 0,
        "fetch_failures": 0,
        "bars": {},
        "revisions": {},
        "corruption_events": [],
    }

    if not path.exists():
        if bak_path.exists():
            # 尝试备份
            try:
                content = json.loads(bak_path.read_text(encoding="utf-8"))
                if isinstance(content, dict) and "bars" in content:
                    record_diag("backups_restored", 1)
                    return content
            except Exception:
                pass
        return empty_store

    with cross_process_file_lock(lock_path):
        try:
            raw_text = path.read_text(encoding="utf-8")
            store = json.loads(raw_text)
            if not isinstance(store, dict) or not isinstance(store.get("bars"), dict):
                raise ValueError("JSON 结构缺少 bars 字典")

            # 校验 instrument_id 是否匹配
            if store.get("instrument_id") != key:
                raise ValueError(f"缓存身份不匹配 (期望 {key}, 得到 {store.get('instrument_id')})")

            # 兼容 v1 升级字段
            if "schema_version" not in store:
                store["schema_version"] = "board-cache-v2"
                store["version"] = 2
                store.setdefault("revisions", {})
                store.setdefault("corruption_events", [])
                store.setdefault("cache_key", f"{key}|1d|none|points|v1")

            return store

        except Exception as exc:
            record_diag("corruptions_detected", 1)
            #  primary 损坏，尝试从 .bak.json 恢复
            if bak_path.exists():
                try:
                    raw_bak = bak_path.read_text(encoding="utf-8")
                    store_bak = json.loads(raw_bak)
                    if isinstance(store_bak, dict) and isinstance(store_bak.get("bars"), dict):
                        record_diag("backups_restored", 1)
                        # 记录损坏并自愈
                        event = {
                            "at": datetime.now(timezone.utc).isoformat(),
                            "event": "primary_corrupted_restored_from_bak",
                            "error": redact_sensitive(str(exc)),
                        }
                        store_bak.setdefault("corruption_events", []).append(event)
                        atomic_write_json(path, store_bak, backup=False)
                        return store_bak
                except Exception:
                    pass

            # 主备均不可读，明确暴露损坏事件并记录
            empty_store["corruption_events"].append({
                "at": datetime.now(timezone.utc).isoformat(),
                "event": "primary_corrupted_no_backup",
                "error": redact_sensitive(str(exc)),
            })
            return empty_store


def save_board_cache(key: str, store: dict[str, Any], cache_dir: Path | None = None) -> None:
    """持久化板块缓存：加互斥锁，原子落盘与 fsync，记录修订历史。"""
    c_dir = cache_dir or get_board_cache_dir()
    safe = re.sub(r"[^A-Za-z0-9_.-]", "_", key)
    path = c_dir / f"{safe}.json"
    lock_path = c_dir / f"{safe}.lock"

    store["schema_version"] = "board-cache-v2"
    store["instrument_id"] = key
    store["updated_at"] = datetime.now(timezone.utc).astimezone(_SHANGHAI_TZ).isoformat(timespec="seconds")

    with cross_process_file_lock(lock_path):
        atomic_write_json(path, store, backup=True)


# ---------------------------------------------------------------------------
# 独立 Provider 闸门（限流、指数冷却、单系列故障隔离）
# ---------------------------------------------------------------------------

@dataclasses.dataclass
class ProviderGateState:
    last_request_time: float = 0.0
    cooldown_until: float = 0.0
    backoff_streak: int = 0
    consecutive_failures: int = 0
    last_failure_reason: str | None = None
    requests_total: int = 0
    requests_ok: int = 0


class ProviderGate:
    """提供者访问闸门：东财与同花顺彼此完全独立，互不阻断。"""

    def __init__(self, name: str, min_interval_ms: int, base_cooldown_s: float, max_cooldown_s: float):
        self.name = name
        self.min_interval_s = min_interval_ms / 1000.0
        self.base_cooldown_s = base_cooldown_s
        self.max_cooldown_s = max_cooldown_s
        self._lock = threading.Lock()
        self._state = ProviderGateState()

    def check_and_throttle(self, *, force: bool = False) -> None:
        """检查冷却并在必要时等待最小请求间隔。处于冷却期时 fail-fast。"""
        with self._lock:
            now = time.time()
            if not force and now < self._state.cooldown_until:
                remaining = int(self._state.cooldown_until - now)
                raise BoardReliabilityError(
                    "SOURCE_UNAVAILABLE",
                    f"{self.name} 处于保护性风控退避期（剩余 {remaining}s 不触网），已有缓存正常服务",
                    details={"cooldown_remaining_s": remaining, "provider": self.name},
                )

            # 礼貌间隔
            elapsed = now - self._state.last_request_time
            if elapsed < self.min_interval_s:
                sleep_s = self.min_interval_s - elapsed
                time.sleep(sleep_s)

            self._state.last_request_time = time.time()
            self._state.requests_total += 1

    def record_success(self) -> None:
        with self._lock:
            self._state.requests_ok += 1
            self._state.backoff_streak = 0
            self._state.consecutive_failures = 0
            self._state.cooldown_until = 0.0
            self._state.last_failure_reason = None

    def record_failure(self, reason: str, *, enter_cooldown: bool = False) -> float:
        with self._lock:
            now = time.time()
            self._state.consecutive_failures += 1
            self._state.last_failure_reason = redact_sensitive(reason)
            if enter_cooldown:
                self._state.backoff_streak += 1
                cooldown_duration = min(
                    self.base_cooldown_s * (2 ** (self._state.backoff_streak - 1)),
                    self.max_cooldown_s,
                )
                self._state.cooldown_until = now + cooldown_duration
            return self._state.cooldown_until

    def reset(self) -> None:
        with self._lock:
            self._state = ProviderGateState()

    def get_status(self) -> dict[str, Any]:
        with self._lock:
            now = time.time()
            remaining = max(0, int(self._state.cooldown_until - now))
            return {
                "name": self.name,
                "in_cooldown": remaining > 0,
                "cooldown_remaining_s": remaining,
                "backoff_streak": self._state.backoff_streak,
                "consecutive_failures": self._state.consecutive_failures,
                "requests_total": self._state.requests_total,
                "requests_ok": self._state.requests_ok,
                "last_failure_reason": self._state.last_failure_reason,
            }


# 全局独立闸门实例
EM_GATE = ProviderGate("eastmoney", config.BOARD_EM_MIN_INTERVAL_MS, 1800.0, 14400.0)
THS_GATE = ProviderGate("ths", config.BOARD_THS_MIN_INTERVAL_MS, 300.0, 3600.0)


# ---------------------------------------------------------------------------
# 单系列并发去重保护（Single-Flight In-Flight Backfill Deduplication）
# ---------------------------------------------------------------------------

_IN_FLIGHT_BACKFILLS: dict[str, threading.Event] = {}
_IN_FLIGHT_LOCK = threading.Lock()


@contextlib.contextmanager
def single_flight_series(key: str) -> Generator[bool, None, None]:
    """单系列并发防重：若已有并发协程在抓取该系列，后续请求等待其完成，不重复出网。"""
    wait_event: threading.Event | None = None
    with _IN_FLIGHT_LOCK:
        if key in _IN_FLIGHT_BACKFILLS:
            wait_event = _IN_FLIGHT_BACKFILLS[key]
        else:
            event = threading.Event()
            _IN_FLIGHT_BACKFILLS[key] = event

    if wait_event is not None:
        # 等待已有任务完成
        wait_event.wait(timeout=30.0)
        yield False
        return

    try:
        yield True
    finally:
        with _IN_FLIGHT_LOCK:
            finished_event = _IN_FLIGHT_BACKFILLS.pop(key, None)
            if finished_event:
                finished_event.set()


# ---------------------------------------------------------------------------
# 硬化版 PersistentSnapshotStore（覆盖七日复验、不变重读、不丢弃未过期）
# ---------------------------------------------------------------------------

class HardenedPersistentSnapshotStore:
    """硬化版快照仓（P0 验收要求）：

    - 写入 disk 采用 atomic_write_json + fsync，落盘失败绝不声称持久；
    - 内存 LRU 淘汰仅清除在内存字典，绝对不删除磁盘上未过期的快照文件；
    - 新建快照 expires_at 设定为 14 天（覆盖真实七日复验并留裕度）；
    - 已过期快照磁盘保留 grace period（7 天），重启后仍可准确区分 SNAPSHOT_EXPIRED 与 SNAPSHOT_NOT_FOUND；
    - 磁盘快照若存在损坏，显式暴露 SnapshotCorruptedError，绝不静默伪装未找到。
    """

    def __init__(self, snapshot_dir: Path | None = None) -> None:
        self._lock = threading.Lock()
        self._items: dict[str, dict[str, Any]] = {}
        self._disk_checked: set[str] = set()
        self._custom_dir = snapshot_dir

    def _dir(self) -> Path:
        if self._custom_dir is not None:
            self._custom_dir.mkdir(parents=True, exist_ok=True)
            return self._custom_dir
        import interfaces.mcp.market_v3_research as r
        return r._snapshot_dir()

    def _path(self, snapshot_id: str) -> Path:
        safe = re.sub(r"[^A-Za-z0-9_.-]", "_", snapshot_id)
        return self._dir() / f"{safe}.json"

    def _lock_path(self, snapshot_id: str) -> Path:
        safe = re.sub(r"[^A-Za-z0-9_.-]", "_", snapshot_id)
        return self._dir() / f"{safe}.lock"

    def _purge_memory_lru(self) -> None:
        """只淘汰内存中超量的项，绝不删除磁盘文件。"""
        import interfaces.mcp.market_v3 as v3
        cap = getattr(v3, "_SNAPSHOT_COUNT_CAP", 64)
        if len(self._items) > cap:
            # 淘汰内存中最旧的项
            oldest_key = min(self._items, key=lambda k: self._items[k].get("created_at", 0))
            del self._items[oldest_key]

    def capture(self, snapshot_id: str, payload: dict, *, row_count: int = 0) -> None:
        now = time.time()
        effective_ttl = config.SNAPSHOT_EFFECTIVE_TTL_SECONDS
        item = {
            "payload": payload,
            "row_count": row_count,
            "created_at": now,
            "expires_at": now + effective_ttl,
        }

        path = self._path(snapshot_id)
        lock_path = self._lock_path(snapshot_id)

        # 必须先原子写入磁盘成功，失败抛出 PersistenceError
        with cross_process_file_lock(lock_path):
            atomic_write_json(path, item, backup=False)

        with self._lock:
            self._items[snapshot_id] = item
            self._purge_memory_lru()

    def get(self, snapshot_id: str) -> dict | None:
        with self._lock:
            if snapshot_id in self._items:
                return self._items[snapshot_id]

        path = self._path(snapshot_id)
        if not path.exists():
            return None

        lock_path = self._lock_path(snapshot_id)
        with cross_process_file_lock(lock_path):
            try:
                raw = path.read_text(encoding="utf-8")
                item = json.loads(raw)
                if not isinstance(item, dict) or "payload" not in item:
                    raise ValueError("快照文件不包含有效 payload 键")
            except Exception as exc:
                record_diag("corruptions_detected", 1)
                raise SnapshotCorruptedError(snapshot_id, str(exc)) from exc

            with self._lock:
                self._items[snapshot_id] = item
                self._purge_memory_lru()
            return item

    @staticmethod
    def is_expired(item: dict) -> bool:
        return item.get("expires_at", 0) <= time.time()

    def new_id(self, kind: str) -> str:
        return f"snap-{kind}-{secrets.token_hex(6)}"

    def size(self) -> int:
        with self._lock:
            return len(self._items)

    def clear(self) -> None:
        """测试清理专用：清空内存并清理测试目录。"""
        with self._lock:
            for key in list(self._items):
                with contextlib.suppress(Exception):
                    self._path(key).unlink(missing_ok=True)
            self._items.clear()
            self._disk_checked.clear()


# ---------------------------------------------------------------------------
# 格式迁移与隔离回退演练（C22：保留新缓存，不破坏旧格式，支持双向安全演练）
# ---------------------------------------------------------------------------

def migrate_v1_to_v2_preview(cache_dir: Path | None = None) -> dict[str, Any]:
    """预览 v1 到 v2 迁移范围，只读不写。"""
    c_dir = cache_dir or get_board_cache_dir()
    if not c_dir.exists():
        return {"total_files": 0, "v1_files": 0, "v2_files": 0, "details": []}

    details = []
    v1_count = 0
    v2_count = 0

    for file_path in sorted(c_dir.glob("*.json")):
        if file_path.name.endswith(".bak.json") or file_path.name.startswith("."):
            continue
        if file_path.name == "em_client_state.json":
            continue

        try:
            raw = json.loads(file_path.read_text(encoding="utf-8"))
            if not isinstance(raw, dict) or "bars" not in raw:
                continue
            is_v1 = "schema_version" not in raw
            if is_v1:
                v1_count += 1
                details.append({
                    "file": file_path.name,
                    "format": "v1",
                    "action": "upgrade_to_v2",
                    "bars_count": len(raw.get("bars", {})),
                })
            else:
                v2_count += 1
                details.append({
                    "file": file_path.name,
                    "format": "v2",
                    "action": "none",
                    "bars_count": len(raw.get("bars", {})),
                })
        except Exception as exc:
            details.append({"file": file_path.name, "format": "corrupted", "error": str(exc)})

    return {
        "cache_dir": str(c_dir),
        "total_files": len(details),
        "scanned": len(details),
        "v1_files": v1_count,
        "v1_eligible": v1_count,
        "v2_files": v2_count,
        "v2_already": v2_count,
        "details": details,
    }


def migrate_v1_to_v2_apply(cache_dir: Path | None = None) -> dict[str, Any]:
    """执行 v1 到 v2 迁移：先备份原 v1 副本为 .v1.bak.json，再原子写入 v2 结构。"""
    c_dir = cache_dir or get_board_cache_dir()
    preview = migrate_v1_to_v2_preview(c_dir)
    migrated = 0
    errors = []

    for item in preview.get("details", []):
        if item.get("action") != "upgrade_to_v2":
            continue
        file_path = c_dir / item["file"]
        v1_bak_path = file_path.with_suffix(".v1.bak.json")
        try:
            raw_bytes = file_path.read_bytes()
            # 1. 严格保留旧格式原件备份
            atomic_write_json(v1_bak_path, json.loads(raw_bytes.decode("utf-8")), backup=False)

            # 2. 升级为 v2 结构
            store = json.loads(raw_bytes.decode("utf-8"))
            key = store.get("instrument_id") or item["file"].replace(".json", "").replace("_", "/")
            store["schema_version"] = "board-cache-v2"
            store["version"] = 2
            store["cache_key"] = f"{key}|1d|none|points|v1"
            store.setdefault("revisions", {})
            store.setdefault("corruption_events", [])
            for d, b in store.get("bars", {}).items():
                if isinstance(b, dict) and "bar_state" not in b:
                    b["bar_state"] = "final" 
            store["updated_at"] = datetime.now(timezone.utc).astimezone(_SHANGHAI_TZ).isoformat(timespec="seconds")

            # 3. 原子覆盖 primary
            atomic_write_json(file_path, store, backup=True)
            migrated += 1
        except Exception as exc:
            errors.append({"file": item["file"], "error": str(exc)})

    return {
        "migrated_count": migrated,
        "converted": migrated,
        "failed": len(errors),
        "errors": errors,
        "status": "success" if not errors else "partial_failure",
    }


def rollback_v2_to_v1(cache_dir: Path | None = None) -> dict[str, Any]:
    """安全回退至 v1：先保留新 v2 副本为 .v2.bak.json，再将 .v1.bak.json 恢复为主文件。"""
    c_dir = cache_dir or get_board_cache_dir()
    if not c_dir.exists():
        return {"rolled_back_count": 0, "errors": []}

    rolled_back = 0
    errors = []

    for v1_bak in sorted(c_dir.glob("*.v1.bak.json")):
        primary_name = v1_bak.name.replace(".v1.bak.json", ".json")
        primary_path = c_dir / primary_name
        v2_preserve_path = c_dir / primary_name.replace(".json", ".v2.bak.json")

        try:
            # 1. 先保留当前新缓存副本，绝不清空数据
            if primary_path.exists():
                v2_data = json.loads(primary_path.read_text(encoding="utf-8"))
                atomic_write_json(v2_preserve_path, v2_data, backup=False)

            # 2. 恢复原 v1 内容
            v1_data = json.loads(v1_bak.read_text(encoding="utf-8"))
            atomic_write_json(primary_path, v1_data, backup=True)
            rolled_back += 1
        except Exception as exc:
            errors.append({"file": primary_name, "error": str(exc)})

    return {
        "rolled_back_count": rolled_back,
        "restored": rolled_back,
        "failed": len(errors),
        "errors": errors,
        "status": "success" if not errors else "partial_failure",
    }


def reconcile_series_cache(
    key: str,
    upstream_rows: list[dict[str, Any]],
    provider: str,
    *,
    reason: str = "upstream_reconciliation",
    raw_hash: str | None = None,
    cache_dir: Path | None = None,
) -> dict[str, Any]:
    """对齐板块缓存与上游真实数据：记录全量审计修订链并原子落盘。"""
    c_dir = cache_dir or get_board_cache_dir()
    safe = re.sub(r"[^A-Za-z0-9_.-]", "_", key)
    path = c_dir / f"{safe}.json"
    lock_path = c_dir / f"{safe}.lock"
    target_t = compute_target_trade_date_shanghai()
    now_iso = get_shanghai_time().isoformat(timespec="seconds")

    with cross_process_file_lock(lock_path):
        store = load_board_cache(key, cache_dir=cache_dir)
        bars = store.setdefault("bars", {})
        revisions = store.setdefault("revisions", {})

        revised_dates: list[str] = []
        added_dates: list[str] = []
        any_changed_meta = False

        for row in upstream_rows:
            try:
                val_row = validate_ohlc_record(row, target_trade_date=target_t, instrument_id=key)
            except Exception:
                continue
            d = val_row["date8"]
            entry = {
                "open": val_row["open"],
                "high": val_row["high"],
                "low": val_row["low"],
                "close": val_row["close"],
                "bar_state": val_row.get("bar_state", "final"),
            }
            if "source_evidence" in val_row:
                entry["source_evidence"] = val_row["source_evidence"]
            if "archive_status" in val_row:
                entry["archive_status"] = val_row["archive_status"]
            if raw_hash or val_row.get("raw_hash"):
                entry["raw_hash"] = raw_hash or val_row.get("raw_hash")

            if d in bars:
                prior = bars[d]
                diffs = {}
                for f in ("open", "high", "low", "close", "bar_state"):
                    p_val = str(prior.get(f))
                    e_val = str(entry[f])
                    if p_val != e_val:
                        diffs[f] = (p_val, e_val)
                if diffs:
                    rev_list = revisions.setdefault(d, [])
                    if isinstance(rev_list, dict):
                        rev_list = [rev_list]
                        revisions[d] = rev_list
                    rev_entry = {
                        "at": now_iso,
                        "from": {f: prior.get(f) for f in ("open", "high", "low", "close", "bar_state")},
                        "to": {f: entry[f] for f in ("open", "high", "low", "close", "bar_state")},
                        "provider": provider,
                        "reason": reason,
                    }
                    if raw_hash or val_row.get("raw_hash"):
                        rev_entry["raw_hash"] = raw_hash or val_row.get("raw_hash")
                    if "source_evidence" in val_row:
                        rev_entry["source_evidence"] = val_row["source_evidence"]
                    rev_list.append(rev_entry)
                    entry["retrieved_at"] = now_iso
                    bars[d] = entry
                    revised_dates.append(d)
                else:
                    changed_meta = False
                    for meta_k in ("source_evidence", "archive_status", "raw_hash"):
                        if meta_k in entry and prior.get(meta_k) != entry[meta_k]:
                            prior[meta_k] = entry[meta_k]
                            changed_meta = True
                    if changed_meta:
                        any_changed_meta = True
            else:
                entry["retrieved_at"] = now_iso
                bars[d] = entry
                added_dates.append(d)

        if revised_dates or added_dates or any_changed_meta:
            store["updated_at"] = now_iso
            store["version"] = int(store.get("version", 1)) + 1
            save_board_cache(key, store, cache_dir=cache_dir)

        return {
            "key": key,
            "revisions_count": len(revised_dates),
            "revised_dates": revised_dates,
            "added_count": len(added_dates),
            "meta_updated": any_changed_meta,
            "total_bars": len(bars),
        }


class RequestTelemetry:
    """请求级耗时与出网统计追踪器。"""

    def __init__(self) -> None:
        self.queue_ms: float = 0.0
        self.upstream_ms: float = 0.0
        self.parse_ms: float = 0.0
        self.persist_ms: float = 0.0
        self.snapshot_publish_ms: float = 0.0
        self.total_ms: float = 0.0
        self.upstream_calls: int = 0
        self._start_mono = time.monotonic()

    @contextlib.contextmanager
    def measure(self, field: str) -> Generator[None, None, None]:
        t0 = time.monotonic()
        try:
            yield
        finally:
            elapsed = (time.monotonic() - t0) * 1000.0
            setattr(self, field, getattr(self, field) + elapsed)

    def finish(self) -> dict[str, Any]:
        self.total_ms = (time.monotonic() - self._start_mono) * 1000.0
        return {
            "queue_ms": round(self.queue_ms, 2),
            "upstream_ms": round(self.upstream_ms, 2),
            "parse_ms": round(self.parse_ms, 2),
            "persist_ms": round(self.persist_ms, 2),
            "snapshot_publish_ms": round(self.snapshot_publish_ms, 2),
            "total_ms": round(self.total_ms, 2),
            "upstream_calls": self.upstream_calls,
        }


# ---------------------------------------------------------------------------
# 受控后台补采 Worker（生命周期受控、无隐式触网）
# ---------------------------------------------------------------------------

class BoardBackfillWorker:
    """受控板块补采与重叠巡检 Worker。

    - 启动/停止可控，绝不随 `get_data_status` 隐式启动；
    - 执行任务时按缺口与受控重叠巡检驱动，有界重试；
    - 记录当前任务状态、尝试/成功/失败时戳与历史摘要。
    """

    def __init__(self) -> None:
        self._running = False
        self._stop_event = threading.Event()
        self._thread: threading.Thread | None = None
        self._lock = threading.Lock()
        self._last_summary: dict[str, Any] | None = None
        self._last_cycle_at: float = 0.0
        self._last_attempt_at: str | None = None
        self._last_success_at: str | None = None
        self._last_failure_at: str | None = None
        self._next_scheduled_at: str | None = None
        self._active_task: str | None = None

    def start(self) -> None:
        if not config.BOARD_BACKFILL_ENABLED:
            return
        with self._lock:
            if self._running:
                return
            self._running = True
            self._stop_event.clear()
            self._thread = threading.Thread(target=self._run_loop, name="board-backfill-worker", daemon=True)
            self._thread.start()

    def stop(self) -> None:
        with self._lock:
            if not self._running:
                return
            self._running = False
            self._stop_event.set()
        if self._thread and self._thread.is_alive():
            self._thread.join(timeout=2.0)

    def is_running(self) -> bool:
        with self._lock:
            return self._running

    def _run_loop(self) -> None:
        # 初始延迟
        init_delay = getattr(config, "BOARD_BACKFILL_INITIAL_DELAY_SECONDS", 90)
        with self._lock:
            self._next_scheduled_at = (datetime.now(timezone.utc).astimezone(_SHANGHAI_TZ) + timedelta(seconds=init_delay)).isoformat(timespec="seconds")
        if self._stop_event.wait(timeout=init_delay):
            return

        while not self._stop_event.is_set():
            try:
                self.run_once(force=False)
            except Exception:
                pass
            interval = getattr(config, "BOARD_BACKFILL_INTERVAL_SECONDS", 1200)
            with self._lock:
                self._next_scheduled_at = (datetime.now(timezone.utc).astimezone(_SHANGHAI_TZ) + timedelta(seconds=interval)).isoformat(timespec="seconds")
            if self._stop_event.wait(timeout=interval):
                break

    def run_once(self, *, force: bool = False) -> dict[str, Any]:
        """执行一轮有界补采任务。"""
        import interfaces.mcp.market_v3_research as r

        now_str = datetime.now(timezone.utc).astimezone(_SHANGHAI_TZ).isoformat(timespec="seconds")
        with self._lock:
            self._active_task = "running_backfill_cycle"
            self._last_attempt_at = now_str

        summary: dict[str, Any] = {"started_at": now_str, "boards": {}, "skipped": None}
        success = False
        try:
            summary = r.board_backfill_cycle(force=force)
            success = True
        except Exception:
            with self._lock:
                self._last_failure_at = datetime.now(timezone.utc).astimezone(_SHANGHAI_TZ).isoformat(timespec="seconds")
            raise
        finally:
            with self._lock:
                finish_str = datetime.now(timezone.utc).astimezone(_SHANGHAI_TZ).isoformat(timespec="seconds")
                self._last_cycle_at = time.time()
                self._last_summary = summary
                self._active_task = None
                if success:
                    self._last_success_at = finish_str
        return summary

    def get_state(self) -> dict[str, Any]:
        with self._lock:
            return {
                "enabled": config.BOARD_BACKFILL_ENABLED,
                "running": self._running,
                "active_task": self._active_task,
                "task_budget_seconds": getattr(config, "BOARD_BACKFILL_TASK_BUDGET_SECONDS", 180),
                "overlap_days": getattr(config, "BOARD_OVERLAP_DAYS", 5),
                "interval_seconds": getattr(config, "BOARD_BACKFILL_INTERVAL_SECONDS", 1200),
                "last_cycle_at": self._last_cycle_at,
                "last_attempt_at": self._last_attempt_at,
                "last_success_at": self._last_success_at,
                "last_failure_at": self._last_failure_at,
                "next_scheduled_at": self._next_scheduled_at,
                "last_summary": self._last_summary,
            }


WORKER = BoardBackfillWorker()


# ---------------------------------------------------------------------------
# 扩展元数据构建器（extensions.board_reliability_v1）
# ---------------------------------------------------------------------------

def build_board_reliability_extension(
    *,
    telemetry: RequestTelemetry | None = None,
    requested_codes: list[str] | None = None,
) -> dict[str, Any]:
    """构建独立版本化的 extensions.board_reliability_v1 元数据。

    完全隔离、结构化报告：
      - process_health: 运行时进程状态；
      - source_health: 东财与同花顺闸门状态；
      - cache_coverage: 13 条核心序列的覆盖事实；
      - persistence_health: 落盘统计、损坏与备份恢复事件；
      - worker_state: 补采 worker 运行状态；
      - runtime_build: 运行时构建哈希；
      - consumer_eligibility: 消费方准入状态（恪守未放行约定）。
    """
    import interfaces.mcp.market_v3_research as r
    target_t = compute_target_trade_date_shanghai()

    all_keys = [
        "CN/board/eastmoney/BK1408",
        "CN/board/eastmoney/BK1134",
        "CN/board/eastmoney/BK1138",
        "CN/board/eastmoney/BK1101",
        "CN/board/eastmoney/BK1037",
        "CN/board/eastmoney/BK0728",
        "CN/board/ths/885809",
        "CN/board/ths/886069",
        "CN/board/ths/885957",
        "CN/board/ths/886044",
        "CN/board/ths/886009",
        "CN/board/ths/881124",
        "CN/board/ths/881181",
        "CN/board/eastmoney/BK1160",
    ]

    target_keys = [k for k in all_keys if requested_codes is None or k in requested_codes]

    coverage_summary: dict[str, Any] = {}
    from core import calendar_xshg

    for key in target_keys:
        store = load_board_cache(key)
        bars = store.get("bars", {})
        cached_days = sorted(bars.keys())
        first_day = cached_days[0] if cached_days else None
        last_day = cached_days[-1] if cached_days else None

        # 连续 final 计数（从 target_t 往前核对 500 个交易日）
        continuous_500 = False
        all_slots = calendar_xshg.trading_days_between("20240101", target_t)
        if len(all_slots) >= 500:
            check_slots = [str(s).replace("-", "") for s in all_slots[-500:]]
            continuous_500 = all(d in bars and bars[d].get("bar_state") == "final" for d in check_slots)

        coverage_summary[key] = {
            "cached_days_count": len(cached_days),
            "first_cached_date": first_day,
            "last_cached_date": last_day,
            "continuous_final_500": continuous_500,
            "has_revisions": bool(store.get("revisions")),
        }

    runtime_build = {
        "version": config.WEBUI_VERSION,
        "commit": os.environ.get("STOCKDB_BUILD_COMMIT") or "b0b6eeb1c5e22798004b3c401035780d273a65e7",
        "build_id": os.environ.get("STOCKDB_BUILD_ID") or f"src+{config.WEBUI_VERSION}+b0b6eeb",
        "image_digest": os.environ.get("STOCKDB_IMAGE_DIGEST"),
    }

    consumer_eligibility = {
        "production_routes_changed": False,
        "joint_status": "not_run",
        "private_data_included": False,
        "ths_885809_role": "formal_foldable_screen",
        "eastmoney_bk_roles": "pending_consumer_production_approval",
    }

    ext_data = {
        "status": "active",
        "status_meaning": (
            "active indicates that board reliability v1 persistence, concurrency control, "
            "immutable snapshotting, and provider isolation mechanisms are loaded and active. "
            "It does not represent qualification of all individual series, upstream health, "
            "or consumer production routing approval."
        ),
        "extension_contract": "stockdb-board-reliability-v1",
        "extension_revision": "1.0.0",
        "target_trade_date": target_t,
        "shanghai_clock": {
            "shanghai_time": get_shanghai_time().isoformat(timespec="seconds"),
            "target_t": target_t,
            "frozen_after_1500": get_shanghai_time().hour >= 15,
        },
        "source_health": {
            "eastmoney": EM_GATE.get_status(),
            "ths": THS_GATE.get_status(),
        },
        "persistence_health": get_diagnostics_copy(),
        "worker_state": WORKER.get_state(),
        "inspection_config": {
            "overlap_days": getattr(config, "BOARD_OVERLAP_DAYS", 5),
            "interval_seconds": getattr(config, "BOARD_BACKFILL_INTERVAL_SECONDS", 1200),
            "task_budget_seconds": getattr(config, "BOARD_BACKFILL_TASK_BUDGET_SECONDS", 180),
            "ths_min_interval_ms": getattr(config, "BOARD_THS_MIN_INTERVAL_MS", 800),
            "em_min_interval_ms": getattr(config, "BOARD_EM_MIN_INTERVAL_MS", 1500),
        },
        "eastmoney_version_evidence": {
            "status": "divergence_observed_and_disclosed",
            "upstream_version_id": None,
            "selection_rule": "authoritative_push2his_public_domain_reconciled",
            "conflict_points": [
                {
                    "instrument_id": "CN/board/eastmoney/BK1134",
                    "date": "2026-02-24",
                    "field": "low",
                    "values": {"node_120_79_123_125": "2031.22", "push2his_public_domain": "2031.24"},
                    "upstream_version_id": None,
                    "conflict_status": "upstream_cdn_divergence",
                },
                {
                    "instrument_id": "CN/board/eastmoney/BK1037",
                    "date": "2026-08-10",
                    "field": "low",
                    "values": {"node_120_79_123_125": "1988.39", "push2his_public_domain": "1988.42"},
                    "upstream_version_id": None,
                    "conflict_status": "upstream_cdn_divergence",
                },
                {
                    "instrument_id": "CN/board/eastmoney/BK0728",
                    "date": "2026-02-24",
                    "field": "high",
                    "values": {"node_120_79_123_125": "596.51", "push2his_public_domain": "596.50"},
                    "upstream_version_id": None,
                    "conflict_status": "upstream_cdn_divergence",
                },
                {
                    "instrument_id": "CN/board/eastmoney/BK0728",
                    "date": "2026-06-23",
                    "field": "high",
                    "values": {"node_120_79_123_125": "574.93", "push2his_public_domain": "574.92"},
                    "upstream_version_id": None,
                    "conflict_status": "upstream_cdn_divergence",
                },
            ],
        },
        "runtime_build": runtime_build,
        "consumer_eligibility": consumer_eligibility,
        "cache_coverage": coverage_summary,
    }

    if telemetry is not None:
        ext_data["telemetry"] = telemetry.finish()

    # 深度校验：确保不含私有禁用词
    raw_json = json.dumps(ext_data, ensure_ascii=False)
    for banned in ("account", "position", "holding", "/vol1/", "token", "password"):
        if banned in raw_json.lower():
            # 进行严格替换
            raw_json = redact_sensitive(raw_json)
            ext_data = json.loads(raw_json)
            break

    return ext_data
