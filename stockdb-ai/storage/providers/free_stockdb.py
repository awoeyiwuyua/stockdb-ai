"""storage.providers.free_stockdb — 上游引擎访问闸口（0.9.2 批次 3 从 app.py 搬迁）。

统一封装对上游引擎（127.0.0.1:7899）的 HTTP 访问：信号量限并发 + 熔断器（探针路径）。
架构定位（总纲 D3）：上游 free-stockdb 是数据层 provider 之一——应用层只经本模块
访问引擎，将来引擎被替换/镜像时改本模块即可。
"""
from __future__ import annotations

import os
import re
import threading
import time
from datetime import datetime

import config  # 模块引用（config.STOCKDB_HOST/PORT/STOCKDB_MAX_CONCURRENCY 动态读取）

# 上游访问治理（并发卫生：熔断 + 信号量）。
# 事故复盘（0.6.x 多标签切页打瘫后端）：stockdb 是全部路径的共享依赖，慢/挂时
# 每次探针都等满超时（扇出 3~4 路 × 10s），并发线程无界堆积。
# 治理原则：探针路径（breaker=True）记仇快败；全部路径过信号量限并发；
# 控制路径（启动等待/同步校验）只过信号量、不受熔断牵连。
_gate = threading.Semaphore(config.STOCKDB_MAX_CONCURRENCY)
_breaker: dict = {"fails": 0, "open_until": 0.0,
                  "threshold": 3, "cooldown": 300.0}
_breaker_lock = threading.Lock()  # 0.9.11：熔断计数读改写加锁（并发失败丢失更新）


def _breaker_open() -> bool:
    with _breaker_lock:
        return time.time() < _breaker["open_until"]


def fetch(path: str, timeout: float = 10.0, breaker: bool = False,
          base: str | None = None, block: bool = False) -> str:
    """打 stockdb HTTP 的统一闸口。

    - 信号量（全路径）：并发最多 STOCKDB_MAX_CONCURRENCY（默认 8）。默认非阻塞：
      满则立即抛 RuntimeError 由调用方降级（不等待——等待会堆积线程）；
      block=True（MCP 查询路径，0.10.0 C1）：满则排队等待槽位——查询正确性
      优先于快败，与收敛前直连行为一致。
    - 熔断器（breaker=True 的探针路径）：连续 threshold 次失败后 open_until 内
      快速失败，调用方降级取缓存，避免 stockdb 挂/忙时每路都等满超时。
    - base（0.10.0 C1）：完整 host:port 覆盖（默认 None = config 单一配置源）。
      唯一使用方是 MCP 接口层——其独立运行模式默认 host 与 config 不同
      （100.66.1.1 vs 127.0.0.1），双轨收敛时经此参数保持既有行为不漂移。
    """
    if breaker and _breaker_open():
        with _breaker_lock:
            fails = _breaker["fails"]
            open_until = _breaker["open_until"]
        raise RuntimeError(
            f"stockdb 熔断中（连续 {fails} 次失败，"
            f"降级至 {datetime.fromtimestamp(open_until).strftime('%H:%M:%S')}）")
    if not _gate.acquire(blocking=block):
        raise RuntimeError("stockdb 并发已满（信号量限流），本次降级")
    try:
        import json as _json
        import urllib.request
        from . import msgpack_lite
        host_port = base if base else f"{config.STOCKDB_HOST}:{config.STOCKDB_PORT}"
        with urllib.request.urlopen(
                f"http://{host_port}{path}", timeout=timeout) as resp:
            body = resp.read()
        # 0.10.29：引擎 0.3.5 起 HTTP 响应改为 MsgPack（旧版为 JSON）。
        # 按 Content-Type 嗅探 → 解包后回序列化为 JSON 文本，调用方 json.loads
        # 契约不变（webui + MCP 全链路一处修复）；其余 Content-Type 走原文本路径。
        content_type = ""
        try:
            headers = getattr(resp, "headers", None)
            if headers is not None and hasattr(headers, "get"):
                content_type = str(headers.get("Content-Type") or "")
        except Exception:
            content_type = ""
        if "msgpack" in content_type:
            try:
                decoded = _json.dumps(msgpack_lite.unpack(body),
                                      ensure_ascii=False, allow_nan=True)
            except Exception as exc:  # 解码失败不静默：让调用方降级路径生效
                raise RuntimeError(f"msgpack 解码失败（引擎协议异常）: {exc}") from exc
        else:
            decoded = body.decode("utf-8", "replace")
        # 0.9.11：成功复位仅限探针路径（breaker=True）——控制路径成功不干扰
        # 探针失败计数（此前任何成功都复位，探针计数被控制路径冲刷）
        if breaker:
            with _breaker_lock:
                _breaker["fails"] = 0
        return decoded
    except Exception:
        if breaker:
            with _breaker_lock:  # 0.9.11：读改写原子化（并发失败不再丢失更新）
                _breaker["fails"] += 1
                if _breaker["fails"] >= _breaker["threshold"]:
                    _breaker["open_until"] = time.time() + _breaker["cooldown"]
        raise
    finally:
        _gate.release()


# ==================== 运行中引擎版本探测（0.10.37 B） ====================
# 为什么需要两来源：引擎**没有**版本接口（/api/version 等一律 400），版本只在启动
# 横幅 `stockdb-server 0.3.5-stockdb` 露出，而该横幅走 stdout（docker logs）——
# STOCKDB_LOG_FILE 只落 ERROR 级（NAS 实测：日志里全是 leveldb 错误行，无横幅）。
# 于是：① 先扫启动日志（本地/原生模式常有横幅）；② 再扫引擎二进制里的版本字面量
# （发行包二进制内含 `0.3.5-stockdb`；按 mtime/size 缓存，读 3MB 一次可忽略）。
# 该值 = 实际在跑的二进制版本，是与上游 release tag 比较的**同类**版本线
# （对比此前拿面板 WEBUI_VERSION 去比上游引擎 tag —— 两条互不相干的版本线）。
_ENGINE_VERSION_RE = re.compile(r"stockdb-server\s+([0-9][0-9A-Za-z.\-]*)")
_BINARY_VERSION_RE = re.compile(rb"(\d+\.\d+\.\d+(?:-[A-Za-z][A-Za-z0-9]*)?)")


def _standalone_version(token: str) -> bool:
    """版本字面量独立性判定（防 IP 误取）。

    `120.53.53` 这类 IP 能从错位处匹配出 `20.53.53`（内部还有「数字紧邻数字」
    的形态）→ 拒绝；`0.3.5` / `0.3.5-stockdb` / `1.12.12` 通过。
    规则：token 内部不得出现两个连续数字（点分数字段必然被点分隔）。
    """
    return not any(token[i].isdigit() and token[i - 1].isdigit()
                   for i in range(1, len(token)))
_ENGINE_BINARY = "/opt/stockdb/stockdb"
_engine_cache: dict = {"at": 0.0, "sig_log": None, "sig_bin": None, "val": None}
_ENGINE_CACHE_TTL = 60.0


def _parse_log_version(path: str) -> dict | None:
    """启动日志最后一条 `stockdb-server <ver>` → info（无则 None）。"""
    last_line = None
    try:
        with open(path, "r", encoding="utf-8", errors="replace") as fh:
            for line in fh:
                if _ENGINE_VERSION_RE.search(line):
                    last_line = line.strip()
    except OSError:
        return None
    if not last_line:
        return None
    raw = _ENGINE_VERSION_RE.search(last_line).group(1)
    return {"version": raw, "base": raw.split("-", 1)[0],
            "source": "log", "detail": last_line[:160]}


def _parse_binary_version(path: str) -> dict | None:
    """引擎二进制内的版本字面量 → info（无则 None）。

    取「最长且出现次数最多」的候选：发行包二进制含 `0.3.5` 与 `0.3.5-stockdb`
    （后者更长、语义更全）；纯数字噪声（1.12.12 等依赖版本）通常只出现一次。
    """
    try:
        with open(path, "rb") as fh:
            data = fh.read()
    except OSError:
        return None
    counts: dict[str, int] = {}
    for m in _BINARY_VERSION_RE.finditer(data):
        token = m.group(1).decode("ascii", "replace")
        if not _standalone_version(token):
            continue
        counts[token] = counts.get(token, 0) + 1
    if not counts:
        return None
    token = max(counts, key=lambda t: (("-" in t), len(t), counts[t]))
    return {"version": token, "base": token.split("-", 1)[0],
            "source": "binary", "detail": f"{path} 内版本字面量（出现 {counts[token]} 次）"}


def engine_version_info(*, ttl: float = _ENGINE_CACHE_TTL, force: bool = False) -> dict | None:
    """运行中引擎版本：启动日志优先，其次引擎二进制字面量。

    返回 {"version": "0.3.5-stockdb", "base": "0.3.5", "source": "log|binary",
    "detail": 依据, "log": 日志路径}；两来源都拿不到 → None（不抛）。
    缓存 60s；日志或二进制的 mtime/size 变化即失效（换引擎后立刻反映）。
    """
    log_path = str(config.STOCKDB_LOG_FILE)
    try:
        st = os.stat(log_path)
        sig_log = (st.st_mtime, st.st_size)
    except OSError:
        sig_log = None
    try:
        st = os.stat(_ENGINE_BINARY)
        sig_bin = (st.st_mtime, st.st_size)
    except OSError:
        sig_bin = None
    now = time.time()
    if (not force and _engine_cache["val"] is not None
            and now - _engine_cache["at"] < ttl
            and _engine_cache["sig_log"] == sig_log
            and _engine_cache["sig_bin"] == sig_bin):
        return _engine_cache["val"]
    val = _parse_log_version(log_path) or _parse_binary_version(_ENGINE_BINARY)
    if val is not None:
        val["log"] = log_path
    _engine_cache.update(at=now, sig_log=sig_log, sig_bin=sig_bin, val=val)
    return val
