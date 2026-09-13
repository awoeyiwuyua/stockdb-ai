#!/usr/bin/env python3
"""free-stockdb webui — 同步管理 + 运维面板（纯 Python 标准库，零第三方依赖）

功能：
  - 同步管理：网页一键完成「停 stockdb 进程 → 运行数据更新（增量同步）→ 重启」
  - 行情查询：代理 stockdb 7899 HTTP API（日K/分钟K/复权/股票代码）
  - 私有存储：mydb 写入（pybao 客户端）+ 港股日K 拉取（东财/腾讯）
  - 日志查看：同步过程实时写入 /data/sync.log，页面轮询展示

0.5.0 单镜像架构：stockdb 与 webui 同容器，进程级控制（pidfile + SIGTERM），
不再依赖 docker socket 挂载。

安全边界：
  - 进程操控仅限固定的 stockdb 服务（pidfile 停止/启动），不开放任意命令；
    写操作（同步/定时）面向内网信任环境，无令牌鉴权。
  - 只读查询不鉴权；局域网部署即可，如需公网暴露请自行加反向代理鉴权。
  - 同步串行化（互斥锁），防止并发点击
"""

from __future__ import annotations

import collections
import http.client
import json
import math
import os
import re
import signal
import sqlite3
import subprocess
import sys
import threading
import time
import urllib.request
from datetime import date, datetime, timedelta
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import urlparse, parse_qs, unquote

# 只读 MCP（stockdb-native）dispatch：HTTP POST /mcp 复用（纯标准库，随 webui 同目录
# interfaces/mcp/ 分发；0.9.8 严格分层：MCP 归接口层）。
# 缺失/加载失败时 webui 其余功能不受影响，/mcp 路由返回 500。
try:
    from interfaces.mcp.stockdb_mcp_server import dispatch as mcp_dispatch
except Exception as _mcp_import_exc:  # noqa: BLE001 - MCP 模块缺失时优雅降级
    mcp_dispatch = None
    # 0.9.11：导入失败留痕（此前静默 None，/mcp 永久 500 且无法区分缺依赖/代码损坏）
    print(f"webui: MCP 模块加载失败（/mcp 将不可用）: {_mcp_import_exc}", file=sys.stderr)

# /mcp SSE 流式进度推送依赖 pybao_tools 的线程级 progress hook
# （set_progress_hook/clear_progress_hook，见 pybao_tools 模块）；
# 注意：必须用顶层 `import pybao_tools`（而非 `from interfaces.mcp import pybao_tools`），
# 与 interfaces.mcp.stockdb_mcp_server 内的 `import pybao_tools` 共用同一模块实例——
# 否则 threading.local 进度钩子分属两个模块对象，SSE 进度帧永不触发
# （mcp_dispatch 已在上方导入，server 已将 interfaces/mcp/ 插入 sys.path，身份一致）。
# 缺失/加载失败时 webui 其余功能不受影响，SSE 流式请求退化为现有 JSON 响应。
try:
    import pybao_tools
except ImportError:  # noqa: BLE001 - pybao_tools 缺失时优雅降级
    pybao_tools = None
    print("webui: 未加载 pybao_tools，/mcp SSE 流式退化为 JSON 响应", file=sys.stderr)

# ---- 0.9.1 四层架构：配置单一入口（config.py）----
# 运行配置全部收敛于 config 模块（引擎地址/端口/数据目录/调度触发点/版本号/并发闸门），
# 本文件不再直接读环境变量定义这些配置（0.9.2 各层迁移后从 config 引用）。
from config import (  # noqa: E402 - 配置为纯 stdlib，无循环依赖
    AUCTION_CLOSE_TIME,
    AUCTION_COLLECT_TIME,
    DATA_DIR,
    LISTEN_PORT,
    STOCKDB_HOST,
    STOCKDB_LOG_FILE,
    STOCKDB_MAX_CONCURRENCY,
    STOCKDB_PAUSE,
    STOCKDB_PIDFILE,
    STOCKDB_PORT,
    WAREHOUSE_ENABLED,
    WEBUI_VERSION,
)

# ---- 0.9.2 批次 3：mydb/引擎访问迁 storage/providers（本文件保留同名引用） ----
from storage.providers.free_stockdb import (  # noqa: E402
    _breaker as _stockdb_breaker,
    _breaker_open as _stockdb_breaker_open,
    _gate as _stockdb_gate,
    fetch as stockdb_fetch,
)
from storage.providers.mydb_store import (  # noqa: E402
    _mydb_rd,
    _mydb_rd_reset,
    _rd_lock,
    _rd_to_py,
    auction_series_read as _auction_series_read,
    auction_series_write as _auction_series_write,
    mydb_read,
    mydb_tables,
    mydb_write,
    validate_custom_table,
)

# ---- 0.9.2 批次 4：打板用例迁 services/auction_tasks.py（组合根装配） ----
# app 保留同名暴露（HTTP/调度引用不变）；注入点绑定见模块末尾（_wire_auction_tasks）。
import services.auction_tasks as _auction_tasks  # noqa: E402
from services.auction_tasks import (  # noqa: E402
    AUCTION_IMPORT_ERROR,
    AUCTION_METRICS,
    AUCTION_MODULES_AVAILABLE,
    _auction_apply_reference,
    _auction_backfill_state,
    _auction_fired,
    _auction_lag_close,
    _auction_load_codes,
    _auction_load_series,
    _auction_prev_trade_date,
    _auction_points_for_codes,
    auction_run_backfill,
    auction_run_backfill_async,
    auction_run_close,
    auction_run_collect,
    auction_scheduler_loop,
)

# 0.10.0 W4：仓库沉淀编排（注入点绑定见 _wire_warehouse_tasks；handlers 经 app 取用）
import services.warehouse_tasks as _warehouse_tasks  # noqa: E402
from services.warehouse_tasks import (  # noqa: E402
    warehouse_run_async,
    warehouse_scheduler_loop,
    warehouse_status,
)

# ---- 0.9.2 批次 6：HTTP 路由表外置（interfaces/web/routes.py，0.9.8 收拢接口层） ----
from interfaces.web.routes import (  # noqa: E402
    GET_ROUTES as _WEB_GET_ROUTES,
    POST_ROUTES as _WEB_POST_ROUTES,
)

SYNC_LOG = DATA_DIR / "sync.log"

# 同步线程状态
_sync_lock = threading.Lock()
_sync_state = {"running": False, "exit_code": None, "last_start": None, "last_end": None,
               "phase": "idle"}  # phase: idle/stopping/syncing/verifying/restarting/done
_last_sync_stdout: str = ""          # 最近一次同步的 stdout（供解析下载/删除数）
_last_verify_result: str | None = None  # 最近一次完整性验证结果（pass/fail/跳过）
_scheduler_alive = False             # 定时线程心跳（每次循环更新时间戳）
_scheduler_heartbeat = 0.0           # 定时线程最近一次心跳时间戳（unix）
_webui_started = time.time()         # webui 进程启动时间戳

HISTORY_FILE = DATA_DIR / "sync_history.json"
SCHEDULE_FILE = DATA_DIR / "sync_schedule.json"
HISTORY_MAX = 30


# ==================== 同步历史 / 定时配置（落 /data 卷，重建容器不丢） ====================
def load_history() -> list[dict]:
    if not HISTORY_FILE.exists():
        return []
    try:
        data = json.loads(HISTORY_FILE.read_text(encoding="utf-8"))
        return data if isinstance(data, list) else []
    except Exception:
        return []


def append_history(entry: dict) -> None:
    history = load_history()
    history.append(entry)
    history = history[-HISTORY_MAX:]
    try:
        HISTORY_FILE.write_text(
            json.dumps(history, ensure_ascii=False, indent=1), encoding="utf-8"
        )
    except Exception as exc:
        log(f"  ⚠️ 同步历史写入失败: {exc}")


def load_timeline(days: int = 7) -> list[dict]:
    """驾驶舱时间线载荷（W1 批 2，docs/design/webui-cockpit-redesign.md §2.3）。

    最近 days 个交易日逐日聚合四路事件（全部子块静默降级——读失败该日该块为空，
    不阻塞整体；定义书 §4 唯一后端增量，纯读聚合）：
      沉淀 = records/YYYYMMDD.jsonl 中 task=warehouse_sediment（取末条 + 对账 ok）
      同步 = sync_history.json 按 ts 前缀日分组（trigger/exit_code/verified/时长）
      备份 = warehouse/backups/warehouse-YYYYMMDD-*.db 按文件名日期计数
      告警 = alerts 按 ts 日期计数（分 error/warn）
    返回按日期倒序（新 → 旧）。路径全部运行期取 config（patchable，测试友好）。
    """
    import config as _config  # 函数内引用：测试 patch config.DATA_DIR/WAREHOUSE_DIR 生效
    days = max(1, min(31, int(days)))

    # 交易日序列：今天往回收集 days 个交易日（日历不可用时退化为跳过周末）
    probes: list = []
    probe = datetime.now().date()
    guard = 0
    while len(probes) < days and guard < days * 5 + 14:
        guard += 1
        try:
            if not is_trading_day(probe):
                probe -= timedelta(days=1)
                continue
        except Exception:
            if probe.weekday() >= 5:
                probe -= timedelta(days=1)
                continue
        probes.append(probe)
        probe -= timedelta(days=1)

    # —— 沉淀：records/YYYYMMDD.jsonl（task=warehouse_sediment，末条为准）——
    sediment: dict[str, dict] = {}
    try:
        records_dir = Path(_config.DATA_DIR) / "records"
        for d in probes:
            p = records_dir / f"{d.strftime('%Y%m%d')}.jsonl"
            if not p.exists():
                continue
            last = None
            for line in p.read_text(encoding="utf-8").splitlines():
                try:
                    rec = json.loads(line)
                except ValueError:
                    continue
                if rec.get("task") == "warehouse_sediment":
                    last = rec
            if last:
                sediment[d.strftime("%Y%m%d")] = {
                    "rows": last.get("rows"),
                    "ok": bool(last.get("ok")),
                }
    except Exception:
        pass  # 降级：沉淀块整列缺席

    # —— 同步：sync_history 按 ts 前缀日分组 ——
    sync_by_day: dict[str, list] = {}
    try:
        for h in load_history():
            day = str(h.get("ts") or "")[:10].replace("-", "")
            if day:
                sync_by_day.setdefault(day, []).append({
                    "ts": h.get("ts"),
                    "trigger": h.get("trigger"),
                    "exit_code": h.get("exit_code"),
                    "verified": h.get("verified"),
                    "duration_sec": h.get("duration_sec"),
                    "data_latest": h.get("data_latest"),
                })
    except Exception:
        pass

    # —— 备份：warehouse/backups/warehouse-YYYYMMDD-*.db 按文件名日期 ——
    backups: dict[str, dict] = {}
    try:
        bdir = Path(_config.WAREHOUSE_DIR) / "backups"
        for f in sorted(bdir.glob("warehouse-*.db")):
            parts = f.name.split("-")
            if len(parts) >= 2 and len(parts[1]) == 8 and parts[1].isdigit():
                b = backups.setdefault(parts[1], {"count": 0, "last": f.name})
                b["count"] += 1
                b["last"] = f.name
    except Exception:
        pass

    # —— 告警：按 ts 日期计数（分 error/warn）——
    alert_by_day: dict[str, dict] = {}
    try:
        for a in _get_alerts().list(500):
            day = str(a.get("ts") or "")[:10].replace("-", "")
            if not day:
                continue
            slot = alert_by_day.setdefault(day, {"count": 0, "err": 0, "warn": 0})
            slot["count"] += 1
            if a.get("level") == "error":
                slot["err"] += 1
            elif a.get("level") == "warning":
                slot["warn"] += 1
    except Exception:
        pass

    out = []
    for d in sorted(probes, reverse=True):
        d8 = d.strftime("%Y%m%d")
        out.append({
            "date": d8,
            "sediment": sediment.get(d8),
            "sync": sync_by_day.get(d8, []),
            "backups": backups.get(d8),
            "alerts": alert_by_day.get(d8, {"count": 0, "err": 0, "warn": 0}),
        })
    return out


# warehouse_totals TTL 缓存（0.10.27）：facts glob 每次扫数万 parquet 文件名，
# snapshot 进驾驶舱 15s 轮询后不能每拍全量扫盘——60s 缓存对资产卡足够新鲜。
# _monotonic 抽出模块级便于测试替换；force=True（备份落盘后）绕过缓存。
_WH_TOTALS_TTL = 60.0
_monotonic = time.monotonic
_wh_totals_cache: tuple[float, dict] = (0.0, {})


def warehouse_totals(force: bool = False) -> dict:
    """仓库资产总量（W1 v0.4 数据资产卡）：交易日数 / 周K / 月K / 备份。纯读、静默降级。

    交易日数与周/月K 数 = facts 下 parquet 文件名去重计数（date=YYYYMMDD）；
    备份 = backups 目录文件数 + 最近一次落盘时间戳（前端换算年龄）。
    0.10.27：60s TTL 缓存（驱动 15s 轮询聚合快照；glob 数万文件不能每拍扫盘）。
    """
    global _wh_totals_cache
    now = _monotonic()
    ts, cached = _wh_totals_cache
    if not force and ts and now - ts < _WH_TOTALS_TTL:
        return cached
    import config as _config
    facts = Path(_config.WAREHOUSE_DIR) / "facts"

    def _count(sub: str) -> int:
        try:
            return len({p.name[5:13] for p in facts.glob(f"{sub}/*/*/date=*.parquet")
                        if len(p.name) >= 13})
        except Exception:
            return 0

    count, last_mtime = 0, 0.0
    try:
        bks = list((Path(_config.WAREHOUSE_DIR) / "backups").glob("warehouse-*.db"))
        count = len(bks)
        last_mtime = max((p.stat().st_mtime for p in bks), default=0.0)
    except Exception:
        pass
    result = {"sediment_days": _count("daily"), "weeks": _count("week"),
              "months": _count("month"),
              "backups": {"count": count, "last_mtime": last_mtime}}
    _wh_totals_cache = (now, result)
    return result


def _default_schedule() -> dict:
    return {"enabled": False, "times": ["15:30"], "trading_only": True,
            "fired": {}, "retried": {}, "retry_pending": None,
            "stale_retried": {}, "stale_retry_pending": None,
            "last_trigger": None, "next_trigger": None,
            "auction_fired": {}}  # 0.10.35：打板采集/收口日级触发守卫（持久化）


# ==================== A 股交易日历（休市日表，数据截至 2026 年） ====================
# 来源：exchange_calendars 的 XSHG 日历（https://github.com/gerrymanoim/exchange_calendars）
# 取值规则：每个年份「周一~周五但非交易日」的日期（官方调休安排：春节/国庆/元旦/清明/五一/端午/中秋，
# 以及部分周六周日调休补班的 0 个或 1 个非交易日，均已折算进工作日的缺失）。
# 提取脚本：stockdb-ai/scripts/extract_xshg_holidays.py（仅维护期使用，不随 webui 运行）。
# 注意：XSHG 日历发布滞后（2027 官方安排通常 2026 年底公布），未收录年份按"工作日=交易日"处理，
# 数据截至年份后请在日志提示更新。
XSHG_HOLIDAYS: dict[str, set[str]] = {
    "2024": {"01-01", "02-09", "02-12", "02-13", "02-14", "02-15", "02-16",
             "04-04", "04-05", "05-01", "05-02", "05-03", "06-10", "09-16", "09-17",
             "10-01", "10-02", "10-03", "10-04", "10-07"},
    "2025": {"01-01", "01-28", "01-29", "01-30", "01-31", "02-03", "02-04",
             "04-04", "05-01", "05-02", "05-05", "06-02",
             "10-01", "10-02", "10-03", "10-06", "10-07", "10-08"},
    "2026": {"01-01", "01-02", "02-16", "02-17", "02-18", "02-19", "02-20", "02-23",
             "04-06", "05-01", "05-04", "05-05", "06-19", "09-25",
             "10-01", "10-02", "10-05", "10-06", "10-07"},
}
XSHG_HOLIDAYS_THROUGH = "2026-12-31"  # 休市表覆盖到的最后日期（用于到期提示）


_calendar_warned: set[int] = set()  # 休市表未收录年份的日志限频（每年只警告一次）


def is_trading_day(d=None) -> bool:
    """A 股交易日判定：工作日 且 非休市表内日期。

    未收录年份（休市表覆盖后）按"工作日=交易日"处理，并在日志提示更新（每年限频一次，
    避免 4s 轮询触发日志风暴）。供定时同步跳过周末/法定节假日触发用。
    """
    from datetime import datetime as _dt
    d = d or _dt.now().date()
    if d.weekday() >= 5:  # 周六/周日
        return False
    holidays = XSHG_HOLIDAYS.get(str(d.year))
    if holidays is None:
        if d.year not in _calendar_warned:
            _calendar_warned.add(d.year)
            log(f"⚠️ A股休市表未收录 {d.year} 年（数据截至 {XSHG_HOLIDAYS_THROUGH}），请更新 XSHG_HOLIDAYS")
        return True  # 未知年份：工作日即视为交易日
    return d.strftime("%m-%d") not in holidays


def _normalize_times(times) -> list[str]:
    """校验并规范化时间点列表（HH:MM，去重、排序、只留合法值）。"""
    result = []
    seen = set()
    for t in times or []:
        t = str(t).strip()
        try:
            datetime.strptime(t, "%H:%M")
        except ValueError:
            continue
        if t not in seen:
            seen.add(t)
            result.append(t)
    return sorted(result)


_schedule_lock = threading.Lock()  # sync_schedule.json 读改写互斥（调度线程/同步回填/Web 请求并发）


def _write_schedule(cfg: dict) -> None:
    """原子写定时配置：临时文件 + os.replace（避免读者看到写一半的内容）。"""
    tmp = SCHEDULE_FILE.with_suffix(".json.tmp")
    tmp.write_text(json.dumps(cfg, ensure_ascii=False, indent=1), encoding="utf-8")
    os.replace(tmp, SCHEDULE_FILE)


def load_schedule() -> dict:
    """读定时配置；兼容旧格式 {enabled, time} → 迁移为 {enabled, times:[time], trading_only}。

    fired: {日期: [已触发时间点...]}——当天每个时间点只触发一次（多时间点防循环重复触发）。
    retried: {日期: [已安排过自动重试的时间点...]}；retry_pending: 计划执行重试的时间（字符串）。
    纯读不加锁（_write_schedule 原子替换保证读到完整文件）。
    """
    if not SCHEDULE_FILE.exists():
        return _default_schedule()
    try:
        data = json.loads(SCHEDULE_FILE.read_text(encoding="utf-8"))
    except Exception:
        return _default_schedule()
    times = data.get("times")
    if not isinstance(times, list):  # 旧格式迁移：单 time 字段
        times = [str(data.get("time") or "15:30")]
    last = data.get("last_trigger")
    if not isinstance(last, dict):
        last = None
    fired = data.get("fired")
    if not isinstance(fired, dict):
        fired = {}
    retried = data.get("retried")
    if not isinstance(retried, dict):
        retried = {}
    rp = data.get("retry_pending")
    stale_retried = data.get("stale_retried")
    if not isinstance(stale_retried, dict):
        stale_retried = {}
    srp = data.get("stale_retry_pending")
    auction_fired = data.get("auction_fired")
    if not isinstance(auction_fired, dict):
        auction_fired = {}
    norm_times = _normalize_times(times)
    return {
        "enabled": bool(data.get("enabled")),
        "times": norm_times,
        "trading_only": bool(data.get("trading_only", True)),
        "fired": fired,
        "retried": retried,
        "retry_pending": rp if isinstance(rp, str) else None,
        "stale_retried": stale_retried,
        "stale_retry_pending": srp if isinstance(srp, str) else None,
        "last_trigger": last,
        "auction_fired": auction_fired,
        "next_trigger": compute_next_trigger(norm_times, trading_only=bool(data.get("trading_only", True))),
    }


def save_schedule(enabled: bool, times, trading_only: bool = True) -> dict:
    """保存定时配置（保留 last_trigger / fired / retried / retry_pending，不因改配置清空）。

    读改写整体持锁，防止与调度线程/同步回填并发写丢状态。
    """
    with _schedule_lock:
        cfg = {"enabled": bool(enabled), "times": _normalize_times(times),
               "trading_only": bool(trading_only)}
        if not cfg["times"]:
            raise RuntimeError("至少需要一个合法时间点（HH:MM）")
        try:
            old = {}
            if SCHEDULE_FILE.exists():
                try:
                    old = json.loads(SCHEDULE_FILE.read_text(encoding="utf-8"))
                except Exception:
                    old = {}
            # 触发/重试记录：保留有效值，缺失时用空默认（配置结构保持完整）
            cfg["fired"] = old.get("fired") if isinstance(old.get("fired"), dict) else {}
            cfg["retried"] = old.get("retried") if isinstance(old.get("retried"), dict) else {}
            cfg["retry_pending"] = old.get("retry_pending") if isinstance(old.get("retry_pending"), str) else None
            cfg["stale_retried"] = old.get("stale_retried") if isinstance(old.get("stale_retried"), dict) else {}
            cfg["stale_retry_pending"] = (old.get("stale_retry_pending")
                                          if isinstance(old.get("stale_retry_pending"), str) else None)
            cfg["auction_fired"] = (old.get("auction_fired")
                                    if isinstance(old.get("auction_fired"), dict) else {})
            cfg["last_trigger"] = old.get("last_trigger") if isinstance(old.get("last_trigger"), dict) else None
            cfg["next_trigger"] = compute_next_trigger(cfg["times"], trading_only=trading_only)
            _write_schedule(cfg)
        except Exception as exc:
            raise RuntimeError(f"定时配置写入失败: {exc}")
        return cfg


def _today_key() -> str:
    return datetime.now().strftime("%Y-%m-%d")


def _prune_fired(cfg: dict) -> dict:
    """清理 fired/retried/stale_retried 里早于今天的日期（只保留最近记录，防累积）。"""
    today = _today_key()
    for k in ("fired", "retried", "stale_retried"):
        d = cfg.get(k) or {}
        stale = [day for day in d if day < today]
        for day in stale:
            d.pop(day, None)
        cfg[k] = d
    return cfg


def _mark_fired(t: str) -> None:
    """记录某时间点今天已触发（防同一天重复触发），落盘。"""
    with _schedule_lock:
        try:
            cfg = load_schedule()
            today = _today_key()
            fired = dict(cfg.get("fired") or {})
            fired.setdefault(today, [])
            if t not in fired[today]:
                fired[today].append(t)
            cfg["fired"] = fired
            cfg["next_trigger"] = compute_next_trigger(cfg["times"], trading_only=cfg["trading_only"])
            cfg = _prune_fired(cfg)
            _write_schedule(cfg)
        except Exception as exc:
            log(f"⏰ 定时触发标记失败: {exc}")


def _auction_fired_dates(kind: str) -> set:
    """打板触发守卫：kind ∈ {"collect","close"} → 已触发日期集合（0.10.35）。

    纯读；解析失败返回空集（退化为内存守卫语义，不误触发由调度侧兜底）。
    """
    try:
        cfg = load_schedule()
        v = (cfg.get("auction_fired") or {}).get(kind)
        return set(v) if isinstance(v, list) else set()
    except Exception:
        return set()


def _mark_auction_fired(kind: str, date: str) -> None:
    """置位打板触发守卫并落盘（0.10.35）：auction_fired[kind] += [date]。

    保留最近 14 天（防累积）；读写整体持 _schedule_lock，避免与调度线程/配置保存并发丢更新。
    """
    with _schedule_lock:
        try:
            cfg = load_schedule()
            af = dict(cfg.get("auction_fired") or {})
            dates = af.get(kind)
            if not isinstance(dates, list):
                dates = []
            if date not in dates:
                dates.append(date)
            af[kind] = dates[-14:]  # 仅留最近 14 个日期
            cfg["auction_fired"] = af
            _write_schedule(cfg)
        except Exception as exc:
            log(f"⏰ 打板触发守卫落盘失败（{kind} {date}）: {exc}")


def _mark_last_trigger(key: str, t: str | None = None, retry: bool = False) -> None:
    """记录最近一次定时触发（key=日期 时间点；t=该时间点 HH:MM），供界面展示与重试判定。"""
    with _schedule_lock:
        try:
            cfg = load_schedule()
            cfg["last_trigger"] = {"key": key, "t": t, "ts": datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
                                   "exit": None, "retry": bool(retry)}
            cfg["next_trigger"] = compute_next_trigger(cfg["times"], trading_only=cfg["trading_only"])
            _write_schedule(cfg)
        except Exception as exc:
            log(f"⏰ 定时触发标记失败: {exc}")


def _mark_retried(t: str) -> None:
    """记录某时间点今天已安排过自动重试，并登记 10 分钟后的执行计划（retry_pending）。"""
    with _schedule_lock:
        try:
            cfg = load_schedule()
            today = _today_key()
            retried = dict(cfg.get("retried") or {})
            retried.setdefault(today, [])
            if t not in retried[today]:
                retried[today].append(t)
            cfg["retried"] = retried
            cfg["retry_pending"] = (datetime.now() + timedelta(minutes=10)).strftime("%Y-%m-%d %H:%M:%S")
            cfg = _prune_fired(cfg)
            _write_schedule(cfg)
        except Exception as exc:
            log(f"↻ 重试登记失败: {exc}")


def _clear_retry_pending() -> None:
    with _schedule_lock:
        try:
            cfg = load_schedule()
            cfg["retry_pending"] = None
            _write_schedule(cfg)
        except Exception:
            pass


def _arm_stale_retry(latest: str | None, expected: str) -> bool:
    """登记滞后自检重试（0.10.13）：同步 exit 0 但数据未到应至交易日。

    复用调度线程的到点执行机制（stale_retry_pending），与失败重试（retry_pending）
    相互独立：exit!=0 走失败重试，exit=0 但数据未前进走本重试。当日上限
    STALE_RETRY_MAX 次、截止 STALE_RETRY_UNTIL——双保险防深夜空转；窗口用尽
    静默收口（晚间兜底告警会推给人）。返回是否成功登记（测试用）。
    """
    with _schedule_lock:
        try:
            cfg = load_schedule()
            today = _today_key()
            stale_retried = dict(cfg.get("stale_retried") or {})
            n = int(stale_retried.get(today, 0)) + 1
            if n > STALE_RETRY_MAX or datetime.now().strftime("%H:%M") >= STALE_RETRY_UNTIL:
                if cfg.get("stale_retry_pending"):
                    cfg["stale_retry_pending"] = None
                    _write_schedule(cfg)
                return False
            stale_retried[today] = n
            cfg["stale_retried"] = stale_retried
            cfg["stale_retry_pending"] = (datetime.now()
                                          + timedelta(minutes=STALE_RETRY_INTERVAL_MIN)
                                          ).strftime("%Y-%m-%d %H:%M:%S")
            cfg = _prune_fired(cfg)
            _write_schedule(cfg)
            log(f"↻ 滞后自检重试已登记（{n}/{STALE_RETRY_MAX}）：数据最新 {latest} < 应至 {expected}，"
                f"{STALE_RETRY_INTERVAL_MIN} 分钟后再试")
            return True
        except Exception as exc:
            log(f"↻ 滞后重试登记失败: {exc}")
            return False


def _clear_stale_retry_pending() -> None:
    with _schedule_lock:
        try:
            cfg = load_schedule()
            if cfg.get("stale_retry_pending"):
                cfg["stale_retry_pending"] = None
                _write_schedule(cfg)
        except Exception:
            pass


def _update_schedule_trigger_exit(exit_code, retry: bool = False) -> None:
    """run_sync 结束后回填最近一次定时触发的 exit 码（retry 记录随 last_trigger 保留）。"""
    with _schedule_lock:
        try:
            cfg = load_schedule()
            if cfg["last_trigger"] and cfg["last_trigger"].get("exit") is None:
                cfg["last_trigger"]["exit"] = exit_code
                if retry:
                    cfg["last_trigger"]["retry"] = True
                _write_schedule(cfg)
        except Exception:
            pass


def compute_next_trigger(times: list[str], now=None, trading_only: bool = True) -> str | None:
    """最近的下一次触发时间，返回如 '今天 16:05' / '明天 15:30' / '08-13 15:30'。

    与调度器使用同一套交易日判定（trading_only 时跳过周末/A股法定休市），
    避免界面显示"明天"而调度器实际跳过（周五/节假日前）的不一致。
    8 天内无交易日时退回不跳过的兜底计算（极端长假边界）。
    """
    if not times:
        return None
    now = now or datetime.now()
    today = now.date()
    for offset in range(8):
        d = today + timedelta(days=offset)
        if trading_only and not is_trading_day(d):
            continue
        hm = now.strftime("%H:%M") if offset == 0 else "00:00"
        for t in times:
            if t > hm:
                label = "今天" if offset == 0 else ("明天" if offset == 1 else d.strftime("%m-%d"))
                return f"{label} {t}"
    # 兜底：8 天内无交易日，退回不跳过的原始逻辑
    today_hm = now.strftime("%H:%M")
    for t in times:
        if t > today_hm:
            return f"今天 {t}"
    return f"明天 {times[0]}"


def parse_sync_counts(stdout: str) -> dict:
    """从数据更新输出中提取下载/删除数量。"""
    d, r = None, None
    for line in (stdout or "").splitlines():
        if "待下载资源数" in line:
            try:
                d = int(line.split("待下载资源数")[1].strip().split(":")[1].split("个")[0].strip())
            except Exception:
                pass
        if "待删除资源数" in line:
            try:
                r = int(line.split("待删除资源数")[1].strip().split(":")[1].split("个")[0].strip())
            except Exception:
                pass
    return {"downloads": d, "deletes": r}


def _sync_failure_reason(stdout: str) -> str | None:
    """从同步器输出识别数据源失败（0.8.17）。

    同步器（数据更新）对认证/连接失败也返回退出码 0——若不识别，会把
    "auth failed" 当成成功进入验证，并因未打印下载数量触发 None>0 崩溃
    （2026-08-16 事故：认证失败被掩盖成"同步异常：'>' not supported..."）。
    返回失败原因文本；无失败迹象 → None。
    """
    text = stdout or ""
    if "auth failed" in text:
        return "认证失败（auth failed），请检查数据源授权"
    if "状态:连接失败" in text or "连接失败" in text:
        return "数据源连接失败，请检查网络/数据源可用性"
    return None


def _sync_effective(before_date, after_date, counts) -> bool:
    """判定一次同步是否真正生效（纯函数，可单测）。

    同步器（数据更新）退出码 0 不代表有增量：镜像清单协议升级/清单损坏等
    情况下，更新器可能 0 下载且不改数据（2026-08-12 事故：manifest 解析失败
    零下载却退出码 0）。以「待下载数>0」或「本地数据日期确实前进」为准——
    两者都无则视为未生效，避免 webui 误报同步成功。
    """
    if counts and counts.get("downloads") not in (None, 0):
        return True
    return bool(before_date) and bool(after_date) and after_date > before_date


def data_latest_date(force: bool = False) -> str | None:
    """全市场行情最新交易日（8位）：取平安银行 000001 日K 最大日期。

    000001 每交易日都有行情，是可靠的"数据同步到哪天"探针；数据源本身
    不含指数表，这里不代表上证指数点位（概览页大盘指数另行处理）。
    近 3 月前缀通配，跨年安全。

    4s 心跳轮询会频繁调用：8 秒 TTL 缓存，避免每 4s 打 3 次 stockdb HTTP。
    同步验证/重启检测等需要实时的路径传 force=True 绕过缓存。
    """
    now = time.time()
    if not force and now - _latest_date_cache["at"] < 8:
        return _latest_date_cache["val"]  # 失败结果（None）同样缓存：stockdb 忙/挂时不重复打探
    if _stockdb_breaker_open():
        return _latest_date_cache["val"]  # 熔断中快速降级（冷却期后自动恢复探测）
    if not _latest_date_probe_lock.acquire(blocking=False):
        # 已有并发探测在跑（单飞）：立即返回缓存旧值（可能为 None），
        # 下一轮轮询自然拿到新值——避免多标签切换时 N 路同时打 4 个 10s 慢请求
        return _latest_date_cache["val"]
    try:
        import urllib.parse
        from datetime import datetime as dt, timedelta
        today = dt.now()
        start = (today - timedelta(days=95)).strftime("%Y%m%d")
        end = today.strftime("%Y%m%d")
        dates = []
        for prefix in _month_prefixes(start, end):
            try:
                path = f"/?cmd=vals&t={urllib.parse.quote(f'日k:000001:{prefix}*')}"
                rows = json.loads(stockdb_fetch(path, timeout=10, breaker=True))
                for row in rows if isinstance(rows, list) else []:
                    if isinstance(row, dict) and row.get("date"):
                        dates.append(str(row["date"]))
            except Exception:
                continue
        result = max(dates) if dates else None
        _latest_date_cache.update(at=now, val=result)
        return result
    finally:
        _latest_date_probe_lock.release()


def _classify_code(code: str) -> str:
    """按代码段归类：hk / etf / stock / other。

    港股：5 位数字或带 hk 前缀（如 00700 / hk00700）。
    ETF：沪市 51x/52x/56x/58x（510-518 宽基、520-529 新宽基、560-563 行业、588/589 科创），
          深市 159 开头。
    股票：0/3/6 开头（深主板 00x、创业板 300/301、沪主板 60x、科创板 688/689），
          4/8 开头与 92x（北交所 43x/83x/87x/88x/920x）。
    其他：LOF(16x/50x)、REITs(18x)、B股(200/900) 等场内非股票非 ETF 品种。
    """
    c = str(code).strip().lower()
    if c.startswith("hk"):
        c = c[2:]
    if c.isdigit() and len(c) == 5:
        return "hk"
    if c[:2] in ("51", "52", "56", "58") or c[:3] == "159":
        return "etf"
    if c[:2] == "92" or c[:3] == "430" or c[0] in ("0", "3", "4", "6", "8"):
        return "stock"
    return "other"




# ==================== 港股数据（东财 + 腾讯，写入 hk日k: 表） ====================
_HK_TABLE = "hk日k"  # 港股日K自定义表（与上游命名空间隔离）
_HK_EM = "https://push2his.eastmoney.com/api/qt/stock/kline/get"
_HK_QT = "https://web.ifzq.gtimg.cn/appstock/app/fqkline/get"


def _hk_fetch_daily_em(code: str) -> list[dict]:
    """东财港股日K（含成交额）。返回升序 [{date,open,high,low,close,volume,amount}]。"""
    import urllib.request, urllib.parse, re
    secid = "116." + code  # 116 = 港股市场
    url = (_HK_EM + "?" + urllib.parse.urlencode({
        "secid": secid, "fields1": "f1,f2,f3,f4,f5,f6",
        "fields2": "f51,f52,f53,f54,f55,f56,f57,f58",
        "klt": "101", "fqt": "1", "beg": "0", "end": "20500101"}))
    req = urllib.request.Request(url, headers={"User-Agent": "Mozilla/5.0"})
    with urllib.request.urlopen(req, timeout=15) as resp:
        data = json.loads(resp.read().decode("utf-8", "replace"))
    klines = ((data.get("data") or {}).get("klines") or [])
    rows = []
    for line in klines:
        parts = str(line).split(",")
        if len(parts) < 6:
            continue
        date = int(parts[0].replace("-", ""))
        rows.append({
            "date": date,
            "open": float(parts[1]), "close": float(parts[2]),
            "high": float(parts[3]), "low": float(parts[4]),
            "volume": float(parts[5]),
            "amount": float(parts[6]) if len(parts) > 6 else None,
        })
    return rows


def _is_hk_code(code: str) -> bool:
    """港股代码识别：5 位数字，或带 hk 前缀（如 00700 / hk00700）。"""
    c = str(code).strip().lower()
    if c.startswith("hk"):
        c = c[2:]
    return c.isdigit() and len(c) == 5


def _normalize_hk_code(code: str) -> str:
    """规范化为 5 位港股代码（00700）。"""
    c = str(code).strip().lower()
    if c.startswith("hk"):
        c = c[2:]
    return c.zfill(5)


def _hk_fetch_daily_qt(code: str) -> list[dict]:
    """腾讯港股日K（降级源）。格式 [date, open, close, high, low, volume]。"""
    import urllib.request, urllib.parse
    url = (_HK_QT + "?" + urllib.parse.urlencode({
        "param": f"hk{code},day,,,320,qfq"}))
    req = urllib.request.Request(url, headers={"User-Agent": "Mozilla/5.0",
                                               "Referer": "https://gu.qq.com/"})
    with urllib.request.urlopen(req, timeout=15) as resp:
        data = json.loads(resp.read().decode("utf-8", "replace"))
    days = ((data.get("data") or {}).get(f"hk{code}", {}) or {}).get("day") or []
    rows = []
    for d in days:
        if len(d) < 6:
            continue
        rows.append({
            "date": int(d[0].replace("-", "")),
            "open": float(d[1]), "close": float(d[2]),
            "high": float(d[3]), "low": float(d[4]),
            "volume": float(d[5]), "amount": None,
        })
    return rows


def _hk_fetch_daily(code: str) -> list[dict]:
    """拉取港股日K（东财优先，腾讯降级）。"""
    code = _normalize_hk_code(code)
    try:
        rows = _hk_fetch_daily_em(code)
        if rows:
            return rows
    except Exception:
        pass
    return _hk_fetch_daily_qt(code)


def hk_sync(codes: list[str], years: int = 2) -> dict:
    """港股同步：拉取日K写入 mydb hk日k:{code}:{date}（三层 key，代码隔离）。

    years：保留最近 N 年日K（默认 2，约 520 根），避免全量写爆 mydb。
    value 内嵌 date（读取用 vals 无需 key 解析）；keys 通配查询有缓存问题，
    统一用 vals/get 读写（实测可靠）。
    """
    years = max(1, min(int(years or 2), 10))
    cutoff = int((datetime.now().replace(year=datetime.now().year - years)).strftime("%Y%m%d"))
    results = {}
    for raw in codes:
        code = _normalize_hk_code(raw)
        try:
            rows = _hk_fetch_daily(code)
            if not rows:
                results[code] = {"ok": False, "error": "无数据"}
                continue
            items = []
            for r in rows:
                val = {k: v for k, v in r.items() if k != "date"}
                val["date"] = r["date"]  # 内嵌 date，读取无需解析 key
                if r["date"] >= cutoff:
                    items.append((str(r["date"]), val))
            items = items[-520 * years:]  # 兜底截断
            with _rd_lock:  # 0.8.10：rd 单连接串行化 + 失败自愈
                try:
                    rd = _mydb_rd()
                    for key, value in items:
                        rd.set(_HK_TABLE, code, key, value).do()  # .do() 真正发送写入
                except Exception:
                    _mydb_rd_reset()
                    raise
            results[code] = {"ok": True, "bars": len(items),
                             "latest": max(r["date"] for r in rows)}
        except Exception as exc:
            results[code] = {"ok": False, "error": str(exc)[:200]}
    return results


def hk_klines(code: str) -> list[dict]:
    """读取 mydb hk日k: 表（升序）。value 内嵌 date，用 vals 全量读取。
    0.8.10：rd 读取持锁 + 失败自愈。"""
    code = _normalize_hk_code(code)
    with _rd_lock:
        try:
            rd = _mydb_rd()
            vals = rd.vals(_HK_TABLE, code, "*") or []
        except Exception:
            _mydb_rd_reset()
            raise
    rows = []
    for v in vals:
        v = _rd_to_py(v)
        if isinstance(v, dict) and v.get("date"):
            rows.append(v)
    rows.sort(key=lambda r: int(r["date"]))
    return rows


def code_stats() -> dict:
    """全市场标的数量，股票 / ETF 分开统计（其余归 other），并返回查询延迟 ms。

    延迟 = 拉取全市场代码列表耗时，供系统页「行情服务」健康卡显示。
    全市场代码列表 GET 较贵且 4s 心跳反复调用：15 秒缓存（仅缓存成功结果）。
    """
    now = time.time()
    if _code_stats_cache["val"] is not None and now - _code_stats_cache["at"] < 15:
        return _code_stats_cache["val"]
    if _stockdb_breaker_open():
        return {"stock": None, "etf": None, "other": None, "latency_ms": None}  # 熔断快速降级
    import urllib.parse
    t0 = time.time()
    try:
        path = f"/?cmd=get&t={urllib.parse.quote('股票代码')}"
        data = json.loads(stockdb_fetch(path, timeout=15, breaker=True))
        latency_ms = round((time.time() - t0) * 1000)
        codes: list[str] = []
        if isinstance(data, dict):
            for group in data.values():
                if isinstance(group, list):
                    codes.extend(str(c) for c in group)
        stats = {"stock": 0, "etf": 0, "other": 0, "hk": 0}  # 0.9.11：补 hk 键（_classify_code 输出域）
        for c in set(codes):
            stats[_classify_code(c)] += 1
        stats["latency_ms"] = latency_ms
        _code_stats_cache.update(at=now, val=stats)
        return stats
    except Exception:
        return {"stock": None, "etf": None, "other": None, "latency_ms": None}


_coverage_cache: dict = {"at": 0.0, "data": None}  # 15 分钟缓存，避免 4s 轮询重复全历史扫描
_coverage_scan_lock = threading.Lock()  # 0.9.11：全历史扫描单飞锁（并发缓存失效只跑一路）
_latest_date_cache: dict = {"at": 0.0, "val": None}  # 8 秒缓存：/api/status 4s 心跳不重复打 stockdb
_latest_date_probe_lock = threading.Lock()  # 单飞锁：并发缓存失效时只跑一路探测（其余立即取缓存）
_code_stats_cache: dict = {"at": 0.0, "val": None}   # 15 秒缓存：全市场代码列表 GET 较贵
_container_state_cache: dict = {"at": 0.0, "val": None}  # 5 秒缓存：stockdb 进程探测



def data_coverage() -> dict | None:
    """行情数据覆盖范围（最早 ~ 最新交易日），基于 000001 逐年前缀扫描。

    每 4s 轮询会反复请求 /api/status，全历史扫描较重，故缓存 15 分钟。
    返回 {"earliest": int, "latest": int} 或 None（无数据）。
    """
    now = time.time()
    if _coverage_cache["data"] is not None and now - _coverage_cache["at"] < 900:
        return _coverage_cache["data"]
    if _stockdb_breaker_open():
        return _coverage_cache["data"]  # 熔断快速降级（可能为 None）
    # 0.9.11：单飞锁（与 _latest_date_probe_lock 同款）——缓存过期时顺序扫 37 个
    # 年份 × timeout=8 最坏约 5 分钟，多标签页并发命中 /api/status 会各跑一份
    # 全量扫描（线程堆积、心跳拖到分钟级）；失败返回旧缓存
    if not _coverage_scan_lock.acquire(blocking=False):
        return _coverage_cache["data"]
    try:
        import urllib.parse
        from datetime import datetime as _dt
        this_year = _dt.now().year
        earliest = latest = None
        for y in range(1990, this_year + 1):
            try:
                path = f"/?cmd=vals&t={urllib.parse.quote(f'日k:000001:{y}*')}"
                rows = json.loads(stockdb_fetch(path, timeout=8, breaker=True))
                ds = [int(r["date"]) for r in rows if isinstance(r, dict) and r.get("date")]
                if ds:
                    earliest = min(ds) if earliest is None else earliest
                    latest = max(ds)
            except Exception:
                continue
        data = {"earliest": earliest, "latest": latest} if earliest is not None else None
        _coverage_cache.update(at=now, data=data)
        return data
    finally:
        _coverage_scan_lock.release()


_mirror_cache: dict = {"at": 0.0, "val": None}  # 镜像日期抓取缓存（10 分钟），避免 4s 轮询重复访问外网
_mirror_refresh_lock = threading.Lock()  # 防重入：镜像刷新后台线程同一时间只跑一个


def mirror_latest_date() -> str | None:
    """镜像源（a.123128.xyz 网页）标注的最新数据日期（10 分钟缓存）。

    镜像源是 LevelDB 文件镜像（HTTP + manifest），无行情 API，但它首页明文标注
    「数据更新至:YYYY-MM-DD」。抓该日期可判断「本地落后是同步未跑 vs 镜像未发布」。
    可配置 MIRROR_PAGE_URL 覆盖（内网映射/镜像源变更时）。

    镜像抓取是公网请求且可能很慢（最多 12s）：/api/status 4s 心跳里不能同步等它。
    缓存过期时在后台线程刷新，本请求立即返回旧值（无则 None），下次轮询即有新值。
    """
    # 0.9.11：缓存命中不再要求 val 非 None（失败结果也缓存 600s）——此前镜像不可达
    # 时每次心跳都 spawn 线程抓公网页面（7×24 无意义外呼），仅缓存过期才刷新
    if time.time() - _mirror_cache["at"] < 600:
        return _mirror_cache["val"]

    def _refresh():
        try:
            import urllib.request, re
            url = os.environ.get("MIRROR_PAGE_URL", "https://a.123128.xyz/")
            result = None
            try:
                req = urllib.request.Request(url, headers={"User-Agent": "Mozilla/5.0"})
                with urllib.request.urlopen(req, timeout=12) as resp:
                    html = resp.read().decode("utf-8", "replace")
                m = re.search(r"数据更新至:(\d{4}-\d{2}-\d{2})", html)
                result = m.group(1) if m else None
            except Exception:
                result = None
            # 0.9.11：失败结果同样入缓存（val=None 也缓存 600s）——此前缓存命中
            # 条件要求 val 非 None，镜像不可达时每次心跳都 spawn 线程抓公网页面，
            # 7×24 无意义外呼；失败也标记"已尝试"，仅缓存过期才刷新
            _mirror_cache.update(at=time.time(), val=result)
        finally:
            _mirror_refresh_lock.release()

    if _mirror_refresh_lock.acquire(blocking=False):
        threading.Thread(target=_refresh, daemon=True).start()
    # 过期瞬间：立即返回旧值（无则 None），避免阻塞心跳
    return _mirror_cache["val"]


def _workday_lag(today, latest_dt) -> int:
    """latest 距 today 之间的工作日数（跳过周六日）。法定节假日按近似处理。"""
    lag = 0
    d = latest_dt + timedelta(days=1)
    while d <= today:
        if d.weekday() < 5:
            lag += 1
        d += timedelta(days=1)
    return lag


def health_status() -> dict:
    """数据健康度：用工作日计数判定落后（跨周末不误报），盘前宽容。

    联动镜像源日期：mirror = 镜像网页标注的最新数据日期。
    - 本地落后但镜像已更新 → 提示"可同步"
    - 本地落后且镜像未更新 → 提示"镜像尚未发布"，避免误判同步坏了
    """
    latest = data_latest_date()
    mirror = mirror_latest_date()
    if not latest:
        return {"latest": None, "lag_days": None, "mirror": mirror,
                "status": "unknown", "note": "无法获取数据最新日期"}
    try:
        from datetime import datetime as dt
        latest_dt = dt.strptime(latest, "%Y%m%d").date()
        today = dt.now().date()
        lag = _workday_lag(today, latest_dt)
        if lag == 0:
            return {"latest": latest, "lag_days": 0, "mirror": mirror, "status": "ok",
                    "note": f"数据最新 {latest}（已是最新交易日）"}
        if lag == 1:
            # 交易日盘中/盘前：今日数据要等收盘后同步，属正常
            if today.weekday() < 5 and dt.now().hour < 16:
                return {"latest": latest, "lag_days": 0, "mirror": mirror, "status": "ok",
                        "note": f"数据至 {latest}（今日待收盘后同步）"}
        # 落后 1+ 交易日：看镜像是否已更新
        if mirror:
            mirror_norm = mirror.replace("-", "")
            if mirror_norm > latest:
                note = f"本地 {latest}，镜像已至 {mirror}——可同步"
            else:
                note = f"本地 {latest}，镜像尚未发布新数据（{mirror}）"
        else:
            note = f"数据落后 {lag} 个交易日（{latest}），建议立即同步"
        return {"latest": latest, "lag_days": lag, "mirror": mirror,
                "status": "stale", "note": note}
    except Exception as exc:
        return {"latest": latest, "lag_days": None, "mirror": mirror,
                "status": "unknown", "note": f"健康度计算失败: {exc}"}


# ==================== stockdb 进程级控制（0.5.0 单镜像，不再依赖 docker socket） ====================
# entrypoint 后台监督 stockdb 进程存活（读 pidfile，/data/.stockdb-paused 存在时不拉起）；
# webui 通过 pidfile + SIGTERM 停进程、删除暂停标记让监督器重新拉起。
# 本地开发模式（无 pidfile/进程）优雅降级为 unknown。


def _stockdb_pid() -> int | None:
    try:
        text = STOCKDB_PIDFILE.read_text(encoding="utf-8", errors="replace").strip()
        return int(text) if text.isdigit() else None
    except Exception:
        return None


def stockdb_proc_alive() -> bool:
    """pidfile 中的进程是否存活（pid 存在且可 SIG 0 探测）。"""
    pid = _stockdb_pid()
    if not pid:
        return False
    try:
        os.kill(pid, 0)  # 探测存在性，不发信号
        return True
    except OSError:
        return False


def _send_term(pid: int, timeout: float = 30.0) -> None:
    """SIGTERM 并等待退出；超时升级 SIGKILL。"""
    try:
        os.kill(pid, signal.SIGTERM)
    except OSError:
        return  # 已退出
    deadline = time.time() + timeout
    while time.time() < deadline:
        if not stockdb_proc_alive():
            return
        time.sleep(0.3)
    try:
        os.kill(pid, signal.SIGKILL)
    except OSError:
        pass


def container_state(force: bool = False) -> dict:
    """返回 stockdb 运行状态详情（进程级）。

    {ok, status, note}：ok=False 表示本机无法控制 stockdb（本地开发/无 pidfile）。
    started 为进程启动时间（epoch 秒，供运行时长展示）；查询失败时保持 None。
    进程探测在 4s 心跳中频繁触发：5 秒缓存；同步流程里停/启后的状态校验传
    force=True 绕过缓存（否则可能读到停服前的 running 漏掉补启）。
    """
    now = time.time()
    if not force and _container_state_cache["val"] is not None and now - _container_state_cache["at"] < 5:
        return _container_state_cache["val"]
    alive = stockdb_proc_alive()
    started = None
    if alive:
        try:
            started = int(Path(f"/proc/{_stockdb_pid()}/stat").stat().st_ctime)
        except Exception:
            started = None
    result = {"ok": alive, "status": "running" if alive else "stopped",
              "note": "" if alive else "stockdb 进程未运行",
              "started": started}
    _container_state_cache.update(at=now, val=result)
    return result


def container_start() -> None:
    """删除暂停标记，由 entrypoint 监督器拉起 stockdb（进程不存在时立即拉起）。"""
    STOCKDB_PAUSE.unlink(missing_ok=True)


def container_stop() -> None:
    """写暂停标记（监督器不再拉起）+ SIGTERM 停进程（同 PID 双保险）。"""
    STOCKDB_PAUSE.touch(exist_ok=True)
    pid = _stockdb_pid()
    if pid:
        _send_term(pid)


def container_restart() -> None:
    """重启 stockdb 进程（热更新失败止损/加载新快照用）。"""
    container_stop()
    time.sleep(1)
    container_start()


def wait_stockdb_ready(timeout: float = 60.0) -> bool:
    """轮询等待 stockdb HTTP 服务就绪（重启后验证/查询前调用）。

    进程 restart 返回不代表服务已可查询，直接连可能连接拒绝导致误判；
    轮询 7899 的 /?cmd=get&t=股票代码 直到响应。超时返回 False。
    """
    import urllib.request, urllib.parse
    deadline = time.time() + timeout
    while time.time() < deadline:
        try:
            url = f"http://{STOCKDB_HOST}:{STOCKDB_PORT}/?cmd=get&t={urllib.parse.quote('股票代码')}"
            with urllib.request.urlopen(url, timeout=3) as resp:
                if resp.status == 200:
                    return True
        except Exception:
            pass
        time.sleep(2)
    return False


def reload_stockdb() -> list[str]:
    """向运行中的 stockdb 发送 reload 命令，热重载 dataN 快照（零中断）。

    上游同步器设计为同步后向本地 127.0.0.1:7899 发 GET /?cmd=reload&t=<remote>
    让运行中的服务重载 LevelDB。单镜像下 STOCKDB_HOST=127.0.0.1 即自身。
    返回成功重载的 remote 列表（如 ["0","1"]）。
    """
    import urllib.request, urllib.parse
    ok: list[str] = []
    for remote in ("0", "1"):
        try:
            url = f"http://{STOCKDB_HOST}:{STOCKDB_PORT}/?cmd=reload&t={remote}"
            with urllib.request.urlopen(url, timeout=10) as resp:
                body = resp.read().decode("utf-8", "replace")
            if '"ok":true' in body or '"ok": true' in body:
                ok.append(remote)
        except Exception:
            continue
    return ok


def container_logs(tail: int = 150) -> str:
    """读取 stockdb 日志尾部（conf 的 logger.output=/data/log.txt）。"""
    if not STOCKDB_LOG_FILE.exists():
        return ""
    try:
        lines = STOCKDB_LOG_FILE.read_text(encoding="utf-8", errors="replace").splitlines()
        return "\n".join(lines[-tail:])
    except Exception as exc:
        raise RuntimeError(f"读取 stockdb 日志失败: {exc}") from exc


# ==================== 同步任务（后台线程） ====================
def _verify_data(expected_date: str | None = None) -> list[str]:
    """同步后完整性验证。

    expected_date：同步前抓取的全市场最新交易日（8位）。同步后抽样股票的
    最新 bar 日期须 ≥ expected_date 才算同步生效——镜像停在旧日期时不会误报，
    也不会随时间推移退化成"验证某一天有没有数据"。
    返回异常列表（空=通过）。
    """
    problems = []
    try:
        import urllib.request, urllib.parse
        from datetime import datetime as _dt, timedelta as _td
        today = _dt.now()
        months = _month_prefixes(
            (today - _td(days=95)).strftime("%Y%m%d"), today.strftime("%Y%m%d")
        )
        def q(table: str) -> str:
            path = f"/?cmd=get&t={urllib.parse.quote(table)}"
            return stockdb_fetch(path, timeout=15)  # 控制路径：只过信号量，不受熔断牵连
        def vals(table: str) -> list:
            path = f"/?cmd=vals&t={urllib.parse.quote(table)}"
            return json.loads(stockdb_fetch(path, timeout=15))
        codes_raw = q("股票代码")
        try:
            codes = json.loads(codes_raw)
            total = len(codes.get("0", codes)) if isinstance(codes, dict) else len(codes)
            if total == 0:
                problems.append(f"股票代码为空（total=0）")
        except Exception:
            problems.append("股票代码解析失败")
        # 抽样 3 只不同板块：沪市 600633 / 深市 000001 / 创业板 300750
        exp = int(expected_date or 0)
        for code in ("600633", "000001", "300750"):
            try:
                dates = []
                for prefix in months:
                    for row in vals(f"日k:{code}:{prefix}*"):
                        if isinstance(row, dict) and row.get("date"):
                            dates.append(int(row["date"]))
                if not dates:
                    problems.append(f"{code} 日K 无数据")
                elif exp and max(dates) < exp:
                    problems.append(f"{code} 最新 {max(dates)}，早于同步前基准 {exp}（同步未生效）")
            except Exception as exc:
                problems.append(f"{code} 日K 查询失败: {exc}")
    except Exception as exc:
        problems.append(f"验证接口异常: {exc}")
    return problems


def run_sync(hot: bool = True, trigger: str = "manual", retry: bool = False) -> None:
    """同步数据。串行执行，防并发点击。

    hot=True（默认，热更新）：不停服务直接增量同步——同步器下载到 .part 临时文件、
    SHA256 校验后原子 rename 替换（Unix rename 对读进程无影响），服务端持续可查。
    官方 data-source.md 保守要求"同步期间停止服务"，但实测与机制均支持热更新；
    上游多点数据源架构下增量下载进 data1/LevelDB，运行中的服务进程持旧快照，
    故检测到新数据文件（下载数>0）时同步完成后自动重启 stockdb 加载新快照，
    再自动做完整性验证，失败则再次重启止损。

    hot=False（严格模式）：按官方要求先停服务 → 同步 → 重启，作为兜底。

    trigger=manual|scheduled|scheduled-retry：记录触发来源（手动按钮 / 定时线程 /
    定时失败后的自动重试），写入同步历史，便于回看"某次同步是不是定时自动跑的"。
    """
    if not _sync_lock.acquire(blocking=False):
        return  # 已在同步中
    _sync_state.update(running=True, exit_code=None, last_start=time.time(), last_end=None,
                       trigger=trigger, phase="stopping" if not hot else "syncing",
                       fail_reason=None)
    global _last_sync_stdout, _last_verify_result
    _last_sync_stdout = ""
    _last_verify_result = None
    try:
        log(f"=== 同步开始 {now()}（{'热更新' if hot else '严格模式(停服)'}｜{'定时' if trigger.startswith('scheduled') else '手动'}{'·重试' if retry else ''}）===")

        # 0. 同步前抓全市场最新交易日（作为同步后验证基准；绕过 TTL 缓存）
        before_date = None
        try:
            before_date = data_latest_date(force=True)
            if before_date:
                log(f"→ 同步前数据最新交易日：{before_date}")
        except Exception:
            pass

        # 1.（严格模式）停 stockdb；热更新模式不停
        if not hot:
            _sync_state["phase"] = "stopping"
            log("→ 停止 stockdb 进程 ...")
            try:
                if container_state(force=True).get("status") == "running":
                    container_stop()
                else:
                    log("  （stockdb 已处于停止状态）")
            except Exception as exc:
                log(f"  ⚠️ 停止失败，继续同步（风险：数据卷并发写）：{exc}")
        else:
            st = container_state(force=True)
            if st["status"] == "running":
                log("→ 热更新：stockdb 保持运行，直接增量同步 ...")
            elif st["status"] in ("exited", "not-found"):
                log(f"→ 热更新：stockdb 当前 {st['status']}，同步后尝试启动 ...")
            else:
                log(f"→ 热更新：stockdb 状态 {st['status']}（{st['note']}），仍继续同步 ...")

        # 2. 同步数据（同步器读当前目录 sync_url.txt / stockdb.conf）
        _sync_state["phase"] = "syncing"
        log("→ 运行 数据更新（增量同步，断点续传）...")
        cfg = DATA_DIR / "sync_url.txt"
        if not cfg.exists():
            log("  ⚠️ /data/sync_url.txt 不存在，使用镜像模板")
        else:
            sources = [ln for ln in cfg.read_text(encoding="utf-8").splitlines()
                       if ln.strip() and not ln.strip().startswith("#")]
            log(f"  数据源: {sources[0] if sources else '（空，无法同步）'}")
        proc = subprocess.run(
            ["/opt/stockdb/数据更新"],
            cwd=str(DATA_DIR),
            capture_output=True, text=True, timeout=3600 * 6,
        )
        for line in (proc.stdout or "").splitlines():
            log(f"  {line}")
        for line in (proc.stderr or "").splitlines():
            log(f"  [err] {line}")
        _last_sync_stdout = proc.stdout or ""
        _sync_state["exit_code"] = proc.returncode
        if proc.returncode != 0:
            _sync_state["fail_reason"] = f"同步器异常退出（code {proc.returncode}）"
        log(f"→ 数据更新退出码 {proc.returncode}")

        # 3. 热更新模式：验证数据完整性（此时 stockdb 仍在运行）
        if hot:
            if proc.returncode == 0:
                _sync_state["phase"] = "verifying"
                # 0.8.17：同步器对认证/连接失败也返回 0——先识别失败输出，
                # 避免把"auth failed"当成成功进入验证（并触发 None>0 崩溃）
                failure = _sync_failure_reason(_last_sync_stdout)
                if failure:
                    _sync_state["fail_reason"] = f"数据源失败：{failure}"
                    _last_verify_result = "skipped"
                    log(f"  ⚠️ 数据源失败（{failure}），跳过完整性验证")
                    log("  → 同步未执行，数据保持原状（恢复认证后重试）")
                else:
                    log("→ 热更新完成，验证数据完整性 ...")
                # 上游新架构（多点数据源）下增量下载进 data1/LevelDB，
                # 运行中的 stockdb 进程仍持旧快照。优先用上游 reload 命令热重载
                # （零中断）；reload 不可用（老版本/连接失败）则降级重启。
                # 无新文件（downloads=0）时数据未变，跳过加载。
                counts = parse_sync_counts(_last_sync_stdout)
                downloads = counts.get("downloads")
                # 0.8.17 修复：downloads=None（同步器未打印数量）时禁止比较——
                # 旧写法 counts.get("downloads", 0) > 0 在 key 存在值为 None 时
                # 抛 "'>' not supported between instances of 'NoneType' and 'int'"
                # （2026-08-16 auth failed 事故：认证失败被掩盖成同步异常）
                if failure:
                    pass  # 数据源失败：不重启不验证（fail_reason 已置，_last_verify_result=skipped）
                elif downloads is None or downloads > 0:
                    _sync_state["phase"] = "restarting"
                    reloaded = reload_stockdb()
                    if reloaded:
                        _sync_state["phase"] = "verifying"
                        log(f"  ✅ stockdb 热重载成功（remote {','.join(reloaded)}），零中断")
                    else:
                        log("→ reload 不可用（老版本/命令失败），降级重启 stockdb 加载新快照 ...")
                        try:
                            container_restart()
                            log("  ✅ stockdb 已重启")
                            if not wait_stockdb_ready():
                                log("  ⚠️ 重启后服务未在 60s 内就绪，验证可能失败")
                        except Exception as exc:
                            log(f"  ❌ 重启失败：{exc}")
                        _sync_state["phase"] = "verifying"
                if failure:
                    pass  # 数据源失败：跳过完整性验证
                else:
                    problems = _verify_data(before_date)
                    if problems:
                        _last_verify_result = "fail"
                        _sync_state["fail_reason"] = "数据完整性验证未通过"
                        log(f"  ⚠️ 完整性验证未通过：{problems}")
                        log("  → 数据异常，自动重启 stockdb 止损 ...")
                        try:
                            container_restart()
                            log("  ✅ stockdb 已重启")
                        except Exception as exc:
                            log(f"  ❌ 重启失败：{exc}")
                    else:
                        _last_verify_result = "pass"
                        log("  ✅ 数据完整性验证通过（股票代码 + 抽样日K/复权/分钟K）")
            else:
                _last_verify_result = "skipped"
                log("  ⚠️ 同步退出码非 0，跳过完整性验证")
                # 同步失败时确保服务仍在（可能中途被手动停过）；force 绕过缓存
                if container_state(force=True).get("status") != "running":
                    try:
                        container_start()
                        log("  → 已尝试重新启动 stockdb")
                    except Exception as exc:
                        log(f"  ❌ 启动失败：{exc}")

        # 4.（严格模式）重启服务；热更新若中途发现服务没跑也补启
        if not hot:
            _sync_state["phase"] = "restarting"
            log("→ 启动 stockdb 进程 ...")
            try:
                container_start()
                log("  ✅ stockdb 已启动")
            except Exception as exc:
                log(f"  ❌ 启动失败：{exc}")
        elif container_state(force=True).get("status") in ("exited", "not-found"):
            _sync_state["phase"] = "restarting"
            log("→ 热更新收尾：stockdb 未在运行，尝试启动 ...")
            try:
                container_start()
                log("  ✅ stockdb 已启动")
            except Exception as exc:
                log(f"  ❌ 启动失败：{exc}")

        log(f"=== 同步结束 {now()} ===")
        _sync_state["last_end"] = time.time()
    except Exception as exc:
        log(f"❌ 同步异常：{exc}")
        _sync_state["exit_code"] = -1
        _sync_state["fail_reason"] = f"同步异常：{exc}"
    finally:
        # 记录同步历史（时间/触发来源/模式/结果/耗时/下载删除数/数据最新日期/失败原因）
        try:
            counts = parse_sync_counts(_last_sync_stdout)
            after_date = data_latest_date(force=True)
            effective = _sync_effective(before_date, after_date, counts)
            warn = None
            if _sync_state.get("exit_code") == 0 and not effective:
                warn = "同步未生效：下载 0 文件且数据未更新（镜像清单可能已变更）"
            append_history({
                "ts": datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
                "trigger": _sync_state.get("trigger", "manual"),
                "mode": "hot" if hot else "strict",
                "exit_code": _sync_state.get("exit_code"),
                "reason": _sync_state.get("fail_reason"),
                "downloads": counts.get("downloads"),
                "deletes": counts.get("deletes"),
                "verified": _last_verify_result,
                "duration_sec": round(time.time() - _sync_state["last_start"], 1)
                if _sync_state.get("last_start") else None,
                "data_latest": after_date,
                "warn": warn,
            })
            if _sync_state.get("trigger", "").startswith("scheduled"):
                _update_schedule_trigger_exit(_sync_state.get("exit_code"), retry=retry)
        except Exception:
            pass
        # 0.10.13 数据晚到自愈（两钩子，各自容错，不影响同步收尾）：
        # ① exit 0 但数据未到应至交易日（镜像晚发布）→ 登记滞后自检重试；
        # ② 数据前进 → 仓库补沉淀（水印落后时触发，幂等单飞）
        if _sync_state.get("exit_code") == 0:
            try:
                expected = _expected_latest_date()
                latest_now = data_latest_date(force=True)
                if expected and (latest_now or "") < expected:
                    _arm_stale_retry(latest_now, expected)
            except Exception:
                pass
            try:
                from services import warehouse_tasks as _wt
                _wt.maybe_catchup_sediment()
            except Exception:
                pass
        _sync_state["running"] = False
        _sync_state["last_end"] = time.time()
        _sync_state["phase"] = "done"
        _sync_lock.release()


def log(line: str) -> None:
    """同步日志（0.9.2 批次 2：实现迁 ops/logging.py，本处仅保留转发兼容）。"""
    _ops_log(line)


def tail_log(n: int = 200) -> str:
    """同步日志尾部（0.9.2 批次 2：实现迁 ops/logging.py）。"""
    return _ops_tail_log(n)


def now() -> str:
    """当前时间戳（0.9.2 批次 2：实现迁 ops/logging.py）。"""
    return _ops_now()


# ==================== 定时自动同步 ====================
def _pending_times(now_hm: str, times: list[str], fired: set) -> list[str]:
    """当前已到且今天尚未触发过的时间点（多时间点防循环触发核心判定）。

    now_hm: HH:MM；times: 配置的时间点（已排序）；fired: 今天已触发时间点集合。
    返回应触发的时间点（通常 0 或 1 个）。
    """
    return [t for t in times if now_hm >= t and t not in fired]


def scheduler_loop() -> None:
    """后台定时线程：每 30s 检查定时配置，到点触发热更新同步。

    触发判定（多时间点防循环）：
      维护 fired={日期:[已触发时间点]}；对每个配置时间点 t，仅当
      now>=t 且 t 不在「今天已触发集合」内才触发，并立即加入集合。
      多时间点不再交替重复触发；容器重启后集合落盘，当天不会重复触发。

    trading_only（默认开）：非交易日不触发（is_trading_day：工作日排除 A 股法定休市）。

    失败自动重试（持久化，重启不丢）：
      最近一次定时触发 exit!=0 且该时间点今天未安排过重试 → 登记 retry_pending=10 分钟后，
      retried={日期:[已安排重试的时间点]} 防重复安排。到点执行 run_sync(trigger=scheduled-retry)；
      执行前若最后一次触发已成功则取消。重试仍失败不再重试。
    """
    global _scheduler_alive, _scheduler_heartbeat
    while True:
        _scheduler_alive = True
        _scheduler_heartbeat = time.time()
        try:
            cfg = load_schedule()
            now = datetime.now()
            today_key = now.strftime("%Y-%m-%d")
            now_hm = now.strftime("%H:%M")
            if cfg["enabled"] and cfg["times"] and not _sync_state["running"]:
                if cfg["trading_only"] and not is_trading_day(now.date()):
                    # 0.10.18：非交易日必须先睡再 continue——此前直接 continue 跳过
                    # 循环底部的 sleep(30)，周末/节假日整日满核忙转（load_schedule
                    # 读盘 + 日历计算每秒上千次，实测烧满 1 核、NAS 升温）
                    time.sleep(30)
                    continue  # 非交易日：不触发，也不安排重试
                # 正常触发优先；每轮只启动一个任务（if/else 隔离），
                # 避免同轮「正常触发 + 到期重试」并发启动两个线程，导致
                # run_sync 锁静默拒绝其中一个而 fired 已标记（计划未执行却视为已执行）。
                # 跨轮由外层 not _sync_state["running"] 守卫，不存在并发。
                fired = set((cfg.get("fired") or {}).get(today_key, []))
                due = _pending_times(now_hm, cfg["times"], fired)
                if due:
                    t = due[0]
                    _mark_fired(t)
                    _mark_last_trigger(f"{today_key} {t}", t)
                    log(f"⏰ 定时同步触发（{t}）——stockdb 保持运行，热更新")
                    threading.Thread(
                        target=run_sync,
                        kwargs={"hot": True, "trigger": "scheduled"},
                        daemon=True,
                    ).start()
                else:
                    # 本轮无正常触发 → 检查失败自动重试（登记或到期执行）
                    rp = cfg.get("retry_pending")
                    if rp and now.strftime("%Y-%m-%d %H:%M:%S") >= rp:
                        _clear_retry_pending()
                        lt = cfg.get("last_trigger") or {}
                        if lt.get("exit") == 0:
                            log("↻ 重试前同步已成功，取消重试")
                        else:
                            log("↻ 定时重试执行（上次失败）——stockdb 保持运行，热更新")
                            threading.Thread(
                                target=run_sync,
                                kwargs={"hot": True, "trigger": "scheduled-retry", "retry": True},
                                daemon=True,
                            ).start()
                    else:
                        lt = cfg.get("last_trigger") or {}
                        retried = set((cfg.get("retried") or {}).get(today_key, []))
                        lt_t = lt.get("t")
                        if (lt.get("exit") not in (None, 0) and lt.get("key", "").startswith(today_key)
                                and lt_t and lt_t not in retried):
                            _mark_retried(lt_t)
                            log(f"↻ 定时同步上次失败（exit={lt.get('exit')}），安排 10 分钟后自动重试 ...")
                # 0.10.13：滞后自检重试（exit 0 但数据未前进——镜像晚发布场景）。
                # 到点先验证是否仍滞后（追平则取消）；窗口/上限用尽由登记侧收口。
                srp = cfg.get("stale_retry_pending")
                if (srp and now.strftime("%Y-%m-%d %H:%M:%S") >= srp
                        and not _sync_state["running"]):
                    _clear_stale_retry_pending()
                    try:
                        expected = _expected_latest_date(now)
                        latest_now = data_latest_date(force=True)
                        if not expected or (latest_now or "") >= expected:
                            log(f"↻ 滞后重试取消：数据已到位（最新 {latest_now}）")
                        elif now.strftime("%H:%M") >= STALE_RETRY_UNTIL:
                            log(f"↻ 滞后重试窗口已过（{STALE_RETRY_UNTIL}），交由晚间兜底告警收口")
                        else:
                            log("↻ 滞后重试执行（上次成功但数据未前进）——stockdb 保持运行，热更新")
                            threading.Thread(
                                target=run_sync,
                                kwargs={"hot": True, "trigger": "scheduled-stale-retry"},
                                daemon=True,
                            ).start()
                    except Exception as exc:
                        log(f"↻ 滞后重试评估异常: {exc}")
        except Exception as exc:
            log(f"⏰ 定时线程异常: {exc}")
        time.sleep(30)


_cap_cache: dict = {"at": 0.0, "val": None}  # 同步能力检查缓存（60s），避免 4s 轮询反复探测磁盘


def sync_capability() -> dict:
    """同步能力检查：更新程序 / 数据源 / 数据卷可写 / 待重试（warn 级，不参与可用性）。

    比只看 Docker 更能解释"为什么同步失败"。本地开发模式（无 /opt/stockdb/数据更新）
    返回 ok=False，前端展示为不可用。
    待重试任务属于"正在进行中的重试计划"，是 warn/info 而非能力缺失，不参与 ok 计算。
    数据卷探测用唯一临时文件（多浏览器并发不互相删除探针），结果缓存 60s。
    """
    if _cap_cache["val"] is not None and time.time() - _cap_cache["at"] < 60:
        return _cap_cache["val"]
    import tempfile
    checks = {}
    # 1. 更新程序（容器内发行版同步器）
    updater = Path("/opt/stockdb/数据更新")
    if updater.is_file() and os.access(str(updater), os.X_OK):
        checks["updater"] = {"ok": True, "detail": "更新程序存在"}
    elif updater.exists():
        checks["updater"] = {"ok": False, "detail": "更新程序存在但不可执行"}
    else:
        checks["updater"] = {"ok": False, "detail": "未找到更新程序 /opt/stockdb/数据更新"}
    # 2. 数据源配置
    src = ""
    cfg = DATA_DIR / "sync_url.txt"
    if cfg.exists():
        lines = [ln.strip() for ln in cfg.read_text(encoding="utf-8", errors="replace").splitlines()
                 if ln.strip() and not ln.strip().startswith("#")]
        src = lines[0] if lines else ""
    checks["source"] = {"ok": bool(src), "detail": src if src else "sync_url.txt 无有效数据源"}
    # 3. 数据卷可写（唯一临时文件名，避免多浏览器同时删除彼此的探针）
    try:
        probe = Path(tempfile.mktemp(prefix=".write_probe_", dir=str(DATA_DIR)))
        probe.write_text("ok", encoding="utf-8")
        probe.unlink()
        checks["writable"] = {"ok": True, "detail": "数据卷可写"}
    except Exception as exc:
        checks["writable"] = {"ok": False, "detail": f"数据卷不可写: {exc}"}
    # 4. 待重试任务（warn/info 级，不参与 ok）
    rp = load_schedule().get("retry_pending")
    checks["retry_pending"] = {"warn": bool(rp),
                               "detail": f"等待重试：{rp}" if rp else "无待重试任务"}
    cap_ok = all(c.get("ok") for c in checks.values() if "ok" in c)
    result = {"ok": cap_ok, "warn": bool(rp), "checks": checks}
    _cap_cache.update(at=time.time(), val=result)
    return result


def last_sync_summary() -> dict | None:
    """最近一次同步摘要（历史数组末尾=最新记录）。"""
    h = load_history()
    return h[-1] if h else None


def disk_usage() -> dict:
    """数据卷磁盘用量（/data 挂载点）。"""
    try:
        import shutil
        total, used, free = shutil.disk_usage(str(DATA_DIR))
        return {"total_gb": round(total / 2 ** 30, 1),
                "used_gb": round(used / 2 ** 30, 1),
                "free_gb": round(free / 2 ** 30, 1)}
    except Exception:
        return {"total_gb": None, "used_gb": None, "free_gb": None}


# ==================== K 线区间查询（复刻本地 MCP 的 vals 前缀通配） ====================
def _month_prefixes(start: str, end: str) -> list[str]:
    try:
        y0, m0 = int(start[:4]), int(start[4:6])
        y1, m1 = int(end[:4]), int(end[4:6])
    except ValueError:
        return []
    prefixes, y, m = [], y0, m0
    while (y, m) <= (y1, m1):
        prefixes.append(f"{y:04d}{m:02d}")
        m += 1
        if m > 12:
            m, y = 1, y + 1
    return prefixes


def stockdb_get(table: str) -> str:
    import urllib.parse
    return stockdb_fetch(f"/?cmd=get&t={urllib.parse.quote(table)}", timeout=15)


# ==================== 运营支撑（Phase 4.5：告警中心 / MCP 调用捕获 / 上游版本探针） ====================
# 告警中心（Alerts/notify_alert/_get_alerts/常量）0.9.2 批次 2 已迁 ops/alerts.py，
# 此处仅保留 import 暴露（app.Alerts 等名字不变，测试与 HTTP 处理器引用无感）。
from ops.alerts import (  # noqa: E402 - 横切关注点（ops 层）随模块顶部统一装配
    ALERT_LEVELS,
    ALERT_LEVEL_ALIASES,
    MAX_ALERTS,
    Alerts,
    _get_alerts,
    notify_alert,
)
from ops.logging import (  # noqa: E402
    log as _ops_log,
    now as _ops_now,
    tail_log as _ops_tail_log,
)

MCP_CALLS_FILE_MAX_LINES = 2000       # mcp_calls.jsonl 行数上限（超出截断保留尾部）
MCP_CALLS_DEQUE_MAX = 500             # 内存调用 deque 上限（最新 500 条）
MCP_CALLS_LIST_DEFAULT = 100          # list_mcp_calls 默认条数

GITHUB_RELEASE_URL = "https://api.github.com/repos/hello245m/free-stockdb/releases/latest"
RELEASE_TTL_SECONDS = 3600            # 上游版本探针 TTL 缓存（成功与失败均缓存）


def _now_iso() -> str:
    """当前本地时间 ISO（秒级）：2026-08-14T21:52:30。"""
    return datetime.now().isoformat(timespec="seconds")




# ---- 数据新鲜度告警（迁移自 test_ops.py 可执行规格，行为基线一致） ----
FRESHNESS_LAG_THRESHOLD = 2   # 数据新鲜度滞后阈值（交易日滞后 > 2 天告警）

# ---- 数据晚到自愈（0.10.13：0.10.6 试运行 08-28 实证——镜像晚于 15:50 发布当日日K，
#      定时同步 exit 0 但数据未前进，旧重试只认 exit!=0，数据挂到次日；见 CHANGELOG） ----
STALE_RETRY_INTERVAL_MIN = 30  # 滞后自检重试间隔（分钟）
STALE_RETRY_UNTIL = "23:00"    # 当日滞后重试截止时刻（其后交由晚间兜底告警）
STALE_RETRY_MAX = 6            # 当日滞后重试上限（30 分钟 × 6，与截止时刻双保险）
EVENING_STALE_ALERT_AFTER = "21:00"  # 交易日此时刻后数据仍滞后 → 晚间兜底告警


def _expected_latest_date(now_dt: datetime | None = None) -> str | None:
    """当前时刻数据「理应」达到的最新交易日（8 位；0.10.13）。

    15:00 收盘后当日数据应到位（镜像发布延迟由滞后重试吸收）；盘前/盘中
    以前一交易日为准。回退最多 10 天找最近交易日；日历异常返回 None（调用方
    视为无法判定，不触发重试/告警——宁可漏报不误报）。
    """
    now = now_dt or datetime.now()
    probe = now.date() if now.hour >= 15 else now.date() - timedelta(days=1)
    for _ in range(10):
        if is_trading_day(probe):
            return probe.strftime("%Y%m%d")
        probe -= timedelta(days=1)
    return None


def _parse_date(s) -> date | None:
    """解析日期：支持 YYYYMMDD / YYYY-MM-DD；非法返回 None（不抛）。"""
    s = str(s).strip()
    if len(s) == 8 and s.isdigit():
        try:
            return datetime.strptime(s, "%Y%m%d").date()
        except ValueError:
            return None
    try:
        return date.fromisoformat(s[:10])
    except (ValueError, TypeError):
        return None


def data_freshness_alert(latest_date, is_trading_day, *,
                         threshold: int = FRESHNESS_LAG_THRESHOLD,
                         alerts=None) -> None:
    """行情数据新鲜度告警（两分支，均为 warning 级、来源「数据」、当日去重）。

    分支1：latest_date 为 None（探针失败，或日期格式无法解析）→
          「行情数据不可用（探针失败）」；
    分支2：滞后天数 = (今天 - latest).days > threshold（默认 2）且 is_trading_day
          （今天是交易日，数据本应更新）→ 「行情数据已滞后 N 天（最新 D）」。
          非交易日滞后不告警（休市日数据不更新属正常）；滞后为负（时钟超前）
          不告警。

    0.10.36 自愈：探针恢复 / 滞后回落到阈值内时**撤回**本函数投递过的两类告警
    （前缀匹配：「行情数据不可用」「行情数据已滞后」）。此前只投不撤，数据追平
    后旧告警仍挂面板（NAS 实证：09-11 17:50 追平，16:23 的「滞后 35 天」还在）。

    参数：
      latest_date:    最新交易日 'YYYYMMDD' / 'YYYY-MM-DD'；None 视为探针失败
      is_trading_day: 今天是否为交易日（由调用方按日历判定后传入）
      threshold:      滞后天数阈值（默认 2，仅 is_trading_day 时生效）
      alerts:         告警中心（缺省用模块单例；测试可注入隔离实例）
    """
    target = alerts if alerts is not None else _get_alerts()
    if latest_date is None:
        target.add("warning", "数据", "行情数据不可用（探针失败）")
        return
    d = _parse_date(latest_date)
    if d is None:  # 日期无法解析 → 视为不可用（保守告警）
        target.add("warning", "数据", "行情数据不可用（探针失败）")
        return
    lag = (date.today() - d).days
    if is_trading_day and lag > threshold:
        target.add("warning", "数据", f"行情数据已滞后 {lag} 天（最新 {latest_date}）")
    else:
        # 探针可用且滞后在阈值内（含非交易日、时钟超前）→ 条件已恢复，撤旧警
        target.resolve("数据", "行情数据不可用")
        target.resolve("数据", "行情数据已滞后")


def evening_stale_alert(now_dt: datetime | None = None, *, alerts=None) -> bool:
    """晚间兜底告警（0.10.13）：交易日 21:00 后数据仍未到「应至交易日」→ warning。

    与 data_freshness_alert（阈值 2 天）互补：镜像晚发布当天只滞后 1 天，
    旧阈值不报；此告警把「当天没到位」在当晚推给人（滞后重试同窗兜底，
    滞后重试全失败/未启用时这里是最后防线）。消息含最新日期，追平前
    每轮评估都是同一条消息 → 告警中心当日去重，不刷屏。
    0.10.36 自愈：数据已追平（或未到 21:00 / 非交易日）→ 撤回「晚间兜底：」告警。
    返回是否投递（测试用）。
    """
    now = now_dt or datetime.now()
    if now.strftime("%H:%M") < EVENING_STALE_ALERT_AFTER:
        target = alerts if alerts is not None else _get_alerts()
        target.resolve("数据", "晚间兜底：")
        return False
    if not is_trading_day():
        target = alerts if alerts is not None else _get_alerts()
        target.resolve("数据", "晚间兜底：")
        return False
    expected = _expected_latest_date(now)
    latest = data_latest_date()
    if expected and latest and str(latest).replace("-", "") < expected:
        target = alerts if alerts is not None else _get_alerts()
        target.add("warning", "数据",
                   f"晚间兜底：{expected} 数据截至 {EVENING_STALE_ALERT_AFTER} 仍未到位（最新 {latest}）")
        return True
    # 数据已追平（或无期望日期）→ 条件恢复，撤旧警
    target = alerts if alerts is not None else _get_alerts()
    target.resolve("数据", "晚间兜底：")
    return False


def _env_version_tag() -> str | None:
    """构建期注入的引擎版本（Dockerfile 0.10.37 起 ARG VERSION → ENV IMAGE_TAG）。

    环境变量为空串时返回 None（`A or B` 链会把 "" 当结果带出来 → 类型污染）；
    IMAGE_TAG 与 STOCKDB_VERSION 两者取先有值者（后者为历史别名）。
    """
    for name in ("IMAGE_TAG", "STOCKDB_VERSION"):
        val = (os.environ.get(name) or "").strip()
        if val:
            return val
    return None


def upstream_status() -> dict:
    """上游引擎版本状态（0.10.37 B）：单一判定源，供告警与 /api/diag 共用。

    判定 = 上游最新 release tag > 运行中引擎版本（同类版本线）：
      - 引擎版本优先读启动日志，其次扫引擎二进制版本字面量
        （storage.providers.free_stockdb.engine_version_info；引擎无版本接口、
        日志只落 ERROR 级，故双来源）；
      - 都拿不到时退回构建期注入的 IMAGE_TAG（Dockerfile ARG VERSION）；
      - 三者都拿不到 → kind="unknown"（无法比对，不等于"已最新"）。

    kind：up_to_date / update_available / probe_failed / unknown
    纯只读、不抛（探针/日志异常一律降级）。
    """
    try:
        upstream = fetch_upstream_release()
    except Exception:  # noqa: BLE001 - 探针异常按不可达处理
        upstream = None
    engine = None
    try:
        from storage.providers.free_stockdb import engine_version_info
        engine = engine_version_info()
    except Exception:  # noqa: BLE001
        engine = None
    engine_tag = (engine or {}).get("base") or _env_version_tag()
    engine_display = (engine or {}).get("version") or engine_tag
    base = {"engine_version": engine_display, "engine_tag": engine_tag,
            "upstream": upstream}
    if upstream is None or not upstream.get("tag_name"):
        return {**base, "kind": "probe_failed",
                "message": ("上游版本探测失败（GitHub 不可达或超出重试）："
                            "本次无法判断是否有新版")}
    up_tag = upstream["tag_name"]
    ut = _version_tuple(up_tag)
    ct = _version_tuple(engine_tag) if engine_tag else None
    if ut is None or ct is None:
        return {**base, "kind": "unknown",
                "message": (f"版本号无法解析（上游 {up_tag!r} / 当前引擎 "
                            f"{engine_tag!r}）——无法判断是否有新版")}
    if ut > ct:
        return {**base, "kind": "update_available",
                "message": (f"上游引擎已发布 {up_tag}（当前运行 "
                            f"{engine_display or engine_tag}），建议升级镜像"
                            f"（重新 pin ARG VERSION + SHA256 后重建）")}
    return {**base, "kind": "up_to_date",
            "message": f"引擎已是最新（上游最新 {up_tag}，当前 {engine_display or engine_tag}）"}


def upstream_release_alert(*, alerts=None, status: dict | None = None) -> str:
    """上游版本看门狗（0.10.37 D）：探针失败 / 发现新版 / 版本号不可判定 → 告警。

    此前 `stale` 判定恒 false（拿面板版本与引擎 tag 比较），且探针失败静默：
    上游发新版、同名 tag 重传资产、GitHub 不可达三种情况都不会有人被叫醒
    （NAS 实证：0.3.5 同 tag 重传是靠同步全线失败才发现的）。
    本函数把三种情况接进告警中心（source="上游"，当日去重防刷屏）；
    条件恢复（回到 up_to_date）时撤回同源告警——与数据类告警同一自愈纪律。
    返回 status["kind"]（测试用）。
    """
    target = alerts if alerts is not None else _get_alerts()
    st = status if status is not None else upstream_status()
    kind = st.get("kind")
    if kind in ("update_available", "probe_failed", "unknown"):
        target.add("warning", "上游", st["message"])
    else:
        target.resolve("上游", "上游")
    return kind

def ops_watchdog_loop(interval: float = 60.0) -> None:
    """运营支撑看门狗线程：周期投递生产告警（告警中心的生产接线点）。

    每 interval 秒评估一次（启动后预热 30s，等待首次数据探针/日历就绪，避免
    进程启动瞬间误报）：
      数据新鲜度：data_latest_date() 探针失败，或今日（交易日）滞后 > 阈值
      → data_freshness_alert 投递 warning（当日去重，不会刷屏）；
      晚间兜底（0.10.13）：交易日 21:00 后数据仍未到应至交易日 →
      evening_stale_alert 投递 warning；
      上游版本（0.10.37 D）：上游发新版 / 探针失败 / 版本号不可判定 →
      upstream_release_alert 投递 warning（探针自带 1h TTL，不额外压 GitHub）。
    看门狗自身异常绝不退出线程（stderr 提示后继续，与调度线程同级容错）。
    """
    time.sleep(30)  # 预热：等待首次数据探针/日历就绪，避免进程启动瞬间误报
    while True:
        try:
            data_freshness_alert(data_latest_date(), is_trading_day())
        except Exception:  # noqa: BLE001 - 单次评估异常不退出看门狗
            _warn("数据新鲜度看门狗评估异常（已忽略）")
        try:
            evening_stale_alert()
        except Exception:  # noqa: BLE001 - 单次评估异常不退出看门狗
            _warn("晚间兜底告警评估异常（已忽略）")
        try:
            upstream_release_alert()
        except Exception:  # noqa: BLE001 - 单次评估异常不退出看门狗
            _warn("上游版本看门狗评估异常（已忽略）")
        time.sleep(interval)


# ==================== MCP 调用捕获 / 列表 / 统计 ====================
_mcp_deque: "collections.deque" = collections.deque(maxlen=MCP_CALLS_DEQUE_MAX)
_mcp_lock = threading.Lock()
_mcp_file_lines = 0     # jsonl 当前行数（截断判断用）
_mcp_loaded = False     # 是否已从文件惰性加载过（进程重启后恢复统计）


def _mcp_ensure_loaded_locked() -> None:
    """惰性加载：进程重启后首次访问，从 jsonl 尾部恢复最近记录到 deque。

    必须在持有 _mcp_lock 时调用。文件缺失/损坏行 → 静默跳过，不影响统计。
    """
    global _mcp_loaded, _mcp_file_lines
    if _mcp_loaded:
        return
    _mcp_loaded = True
    path = str(DATA_DIR / "mcp_calls.jsonl")
    try:
        if os.path.isfile(path):
            with open(path, "r", encoding="utf-8") as f:
                lines = [ln for ln in f if ln.strip()]
            _mcp_file_lines = len(lines)
            for ln in lines[-MCP_CALLS_DEQUE_MAX:]:
                try:
                    _mcp_deque.append(json.loads(ln))
                except (ValueError, TypeError):
                    continue
    except OSError:
        pass


def _mcp_truncate_file_locked() -> None:
    """jsonl 超上限截断：保留尾部 MCP_CALLS_FILE_MAX_LINES 行（锁内调用）。

    读失败时不反复重试：把行数记到上限，下一轮再触发时重新尝试。
    """
    global _mcp_file_lines
    path = str(DATA_DIR / "mcp_calls.jsonl")
    try:
        with open(path, "r", encoding="utf-8") as f:
            lines = f.readlines()
        with open(path, "w", encoding="utf-8") as f:
            f.writelines(lines[-MCP_CALLS_FILE_MAX_LINES:])
        _mcp_file_lines = len(lines[-MCP_CALLS_FILE_MAX_LINES:])
    except OSError:
        _mcp_file_lines = MCP_CALLS_FILE_MAX_LINES


def capture_mcp_call(rec: dict) -> None:
    """捕获一次 MCP 调用。

    rec 只保留 6 键（缺省补默认，冗余键丢弃；值均为 str/int/bool，可安全序列化）：
      ts         调用时间（缺省当前本地时间 ISO）
      tool       工具名（如 get_kline）
      ok         是否成功（缺省 = not is_error）
      is_error   MCP isError 标记
      elapsed_ms 耗时毫秒
      bytes      响应体字节数
    落盘：DATA_DIR/mcp_calls.jsonl 追加一行 JSON；超 2000 行截断保留尾部。
    内存：deque（上限 500）供 list/stats 实时计算；落盘失败不影响内存统计。
    """
    global _mcp_file_lines
    ts = str(rec.get("ts") or _now_iso())
    is_error = bool(rec.get("is_error"))
    ok = rec.get("ok") if rec.get("ok") is not None else (not is_error)
    norm = {
        "ts": ts,
        "tool": str(rec.get("tool") or ""),
        "ok": bool(ok),
        "is_error": is_error,
        "elapsed_ms": int(rec.get("elapsed_ms") or 0),
        "bytes": int(rec.get("bytes") or 0),
    }
    with _mcp_lock:
        _mcp_ensure_loaded_locked()
        try:
            parent = os.path.dirname(str(DATA_DIR / "mcp_calls.jsonl"))
            if parent and not os.path.isdir(parent):
                os.makedirs(parent, exist_ok=True)
            with open(str(DATA_DIR / "mcp_calls.jsonl"), "a", encoding="utf-8") as f:
                f.write(json.dumps(norm, ensure_ascii=False) + "\n")
            _mcp_file_lines += 1
            if _mcp_file_lines >= MCP_CALLS_FILE_MAX_LINES:
                _mcp_truncate_file_locked()
        except OSError:
            pass  # 落盘失败静默降级：内存统计照常
        _mcp_deque.append(norm)


def list_mcp_calls(limit: int = MCP_CALLS_LIST_DEFAULT) -> list:
    """最近 MCP 调用（最新在前，来自内存 deque；进程重启后从 jsonl 惰性恢复）。"""
    try:
        limit = int(limit)
    except (TypeError, ValueError):
        limit = MCP_CALLS_LIST_DEFAULT
    if limit < 1:
        limit = MCP_CALLS_LIST_DEFAULT
    with _mcp_lock:
        _mcp_ensure_loaded_locked()
        items = [dict(r) for r in reversed(_mcp_deque)]
    return items[:limit]


def mcp_stats() -> dict:
    """MCP 调用统计（从内存 deque 计算，最新 500 条窗口）。

    返回 {total, ok_rate, avg_ms, p95_ms, by_tool}：
      ok_rate   成功率 0~1（空窗口为 None）
      avg_ms    平均耗时毫秒（空窗口 None）
      p95_ms    耗时 P95（最近邻排序法；空窗口 None）
      by_tool   [{tool, n, ok, avg_ms}, ...]（按调用次数降序）
    """
    with _mcp_lock:
        _mcp_ensure_loaded_locked()
        recs = list(_mcp_deque)
    total = len(recs)
    if total == 0:
        return {"total": 0, "ok_rate": None, "avg_ms": None,
                "p95_ms": None, "by_tool": []}
    ok = sum(1 for r in recs if r.get("ok"))
    ms = [float(r.get("elapsed_ms") or 0) for r in recs]
    avg_ms = sum(ms) / total
    sorted_ms = sorted(ms)
    p95 = sorted_ms[max(0, math.ceil(0.95 * total) - 1)]
    by_tool: dict[str, dict] = {}
    for r in recs:
        tool = str(r.get("tool") or "?")
        b = by_tool.setdefault(tool, {"tool": tool, "n": 0, "ok": 0, "_sum": 0.0})
        b["n"] += 1
        if r.get("ok"):
            b["ok"] += 1
        b["_sum"] += float(r.get("elapsed_ms") or 0)
    out = []
    for b in by_tool.values():
        out.append({"tool": b["tool"], "n": b["n"], "ok": b["ok"],
                    "avg_ms": round(b["_sum"] / b["n"], 1)})
    out.sort(key=lambda x: (-x["n"], x["tool"]))
    return {"total": total,
            "ok_rate": round(ok / total, 4),
            "avg_ms": round(avg_ms, 1),
            "p95_ms": round(p95, 1),
            "by_tool": out}


def _mcp_tool_name(msg: dict) -> str:
    """从 JSON-RPC 请求提取工具名：tools/call → params.name；其余 → method。"""
    if not isinstance(msg, dict):
        return ""
    method = str(msg.get("method") or "")
    if method == "tools/call":
        params = msg.get("params")
        if isinstance(params, dict) and params.get("name"):
            return str(params["name"])
    return method


# ==================== 上游最新版本探针 ====================
_RELEASE_CACHE = {"at": 0.0, "val": None}   # {at: unix 秒, val: dict|None}


def fetch_upstream_release(*, timeout: float = 10, ttl: float = RELEASE_TTL_SECONDS,
                           force: bool = False) -> dict | None:
    """上游最新版本探针：GET GitHub releases/latest（浏览器形态 UA）。

    成功 → {tag_name, html_url, published_at}；失败/网络异常/解析失败 → None
    （不抛）。结果 TTL 缓存（默认 3600s；成功与失败均缓存，避免失败时反复打
    GitHub）；force=True 绕过缓存（手动刷新用）。
    """
    now = time.time()
    if not force and now - _RELEASE_CACHE["at"] < ttl:
        return _RELEASE_CACHE["val"]
    val = None
    try:
        req = urllib.request.Request(
            GITHUB_RELEASE_URL,
            headers={
                # 浏览器形态 UA：GitHub API 对默认 urllib UA 偶发 403
                "User-Agent": ("Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 "
                               "(KHTML, like Gecko) Chrome/120.0 Safari/537.36"),
                "Accept": "application/vnd.github+json",
                "X-GitHub-Api-Version": "2022-11-28",
            },
        )
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            data = json.loads(resp.read().decode("utf-8"))
        if isinstance(data, dict) and data.get("tag_name") is not None:
            val = {"tag_name": str(data["tag_name"]),
                   "html_url": str(data.get("html_url") or ""),
                   "published_at": data.get("published_at")}
    except Exception:  # noqa: BLE001 - 探针失败返回 None，不抛
        val = None
    _RELEASE_CACHE.update(at=now, val=val)
    return val


def _version_tuple(s) -> tuple | None:
    """从字符串提取首个 X.Y[.Z] 版本三元组（'v0.3.1' / '测试版本0.3.1' → (0,3,1)）。"""
    m = re.search(r"(\d+)\.(\d+)(?:\.(\d+))?", str(s))
    if not m:
        return None
    return (int(m.group(1)), int(m.group(2)), int(m.group(3) or 0))


# ==================== HTTP 服务 ====================
# ==================== 前端静态服务（Phase 5 M0：SPA 外壳 + /legacy 逃生通道） ====================
# 前端已重构为 Vue SPA（仓库根 webui/spa/，构建产物在镜像内 /opt/webui/static/）。
# 旧面板（原 PAGE 字符串）完整保留在 webui/static/legacy/index.html，路由 /legacy 原样渲染，
# 作为逃生通道；WEBUI_UI=legacy 时根路径改用旧面板（默认 spa；SPA 未构建时自动兜底旧面板）。
# 安全：静态文件定位一律 realpath 校验必须落在 STATIC_DIR 内，防路径穿越。
# 默认路径按仓库布局解析（<repo>/webui/static）；镜像内由 Dockerfile 显式设
# WEBUI_STATIC_DIR=/opt/webui/static 锁定，不依赖此默认值的相对推导。
STATIC_DIR = Path(os.environ.get(
    "WEBUI_STATIC_DIR",
    str(Path(__file__).resolve().parent.parent / "webui" / "static"),
))
WEBUI_UI = os.environ.get("WEBUI_UI", "spa").strip().lower()  # spa | legacy

_MIME = {
    ".html": "text/html; charset=utf-8",
    ".js": "text/javascript; charset=utf-8",
    ".css": "text/css; charset=utf-8",
    ".json": "application/json; charset=utf-8",
    ".svg": "image/svg+xml",
    ".png": "image/png",
    ".ico": "image/x-icon",
    ".woff": "font/woff",
    ".woff2": "font/woff2",
    ".map": "application/json; charset=utf-8",
}
# 可长缓存的静态资源扩展名（Vite 产物文件名带内容哈希，可 immutable 缓存）
_CACHEABLE_EXT = {".js", ".css", ".svg", ".png", ".ico", ".woff", ".woff2", ".map"}


# ---- 0.9.6：HTTP Handler 迁 interfaces/web/handlers.py ----
# 0.9.11：Handler 不再在模块级导入——handlers.py 顶层 `import app`，脚本方式
# （python app.py，容器 entrypoint）执行时 sys.modules 无 'app' → 循环重载 →
# "cannot import name 'Handler' from partially initialized module"（0.9.10 部署
# 实证）。延迟到 main()（app 已完整）装配，循环消除且模块导入方式不受影响。

def _wire_auction_tasks() -> None:
    """组合根装配（0.9.2 批次 4）：接口层/探针/日历能力绑定到服务层注入点。

    在 main() 调用（模块加载完成后），避免前向引用；测试按需直接设置
    auction_tasks 的注入点（见 test_ops._AuctionBackfillTests）。
    """
    global _auction_query_snapshot
    try:
        from interfaces.mcp.stockdb_mcp_server import query_point_snapshot as _auction_query_snapshot
    except Exception:  # noqa: BLE001 - MCP 缺失时打板用例整体降级
        _auction_query_snapshot = None
    _auction_tasks.query_snapshot = _auction_query_snapshot
    _auction_tasks.data_latest = data_latest_date
    _auction_tasks.is_fq_event = (pybao_tools.is_fq_event_date
                                  if pybao_tools is not None else None)
    _auction_tasks.is_trading_day = is_trading_day
    # 0.9.5（M5）：研究成果仓储注入（SqliteResearchStore 主线 / mydb 回滚，
    # RESEARCH_STORE 环境变量切换；应用层只依赖 ResearchStore 接口）
    from storage.research_factory import get_research_store as _get_research_store
    _auction_tasks.research_store = _get_research_store()
    # 0.10.35：打板触发守卫持久化（落 sync_schedule.json）——此前纯内存，进程重启
    # 即清空，晚间重启会再次触发采集并采到收盘价冒充开盘价（2026-09-10 实证）。
    _auction_tasks.schedule_fired_provider = _auction_fired_dates
    _auction_tasks.schedule_fired_marker = _mark_auction_fired


def _wire_warehouse_tasks() -> None:
    """组合根装配（0.10.0 W4）：仓库层能力绑定到服务层注入点（C3：services 不直连
    storage.warehouse，全部经注入）。duckdb 缺失/开关关闭时注入仍完成——availability
    注入点负责运行时降级（镜像无 musllinux wheel 场景不拖垮 webui 启动）。
    """
    try:
        from interfaces.mcp.stockdb_mcp_server import query_point_snapshot as _wh_snapshot
    except Exception:  # noqa: BLE001 - MCP 缺失时沉淀任务整体降级
        _wh_snapshot = None
    _warehouse_tasks.query_snapshot = _wh_snapshot
    _warehouse_tasks.data_latest = data_latest_date
    _warehouse_tasks.is_trading_day = is_trading_day
    try:
        from storage import warehouse as _wh_pkg
        from storage.warehouse import backup as _wh_backup
        from storage.warehouse import layout as _wh_layout
        from storage.warehouse import reconcile as _wh_reconcile
        from storage.warehouse import sink as _wh_sink
        _warehouse_tasks.sink = _wh_sink
        _warehouse_tasks.reconcile_daily = _wh_reconcile.reconcile_daily
        _warehouse_tasks.warehouse_root = _wh_layout.root_dir
        _warehouse_tasks.availability = _wh_pkg.availability
        _warehouse_tasks.backup_duckdb = _wh_backup.backup_duckdb  # 0.10.8：warehouse.duckdb 日级备份

        def _wh_refresh_views():
            from storage.warehouse.engine import get_engine as _wh_get_engine
            _wh_get_engine().refresh_views()

        _warehouse_tasks.refresh_views = _wh_refresh_views
    except Exception:  # noqa: BLE001 - duckdb 缺失（ImportError）等：注入点留 None 降级
        pass


class _BoundedHTTPServer(ThreadingHTTPServer):
    """0.9.12：HTTP 并发上限 + daemon 线程。

    病灶（0.9.11 部署实证）：ThreadingHTTPServer 每请求一线程且无上限，慢操作
    （mydb 全表列取 / VACUUM 备份 / MCP 慢路径）排队时线程无限堆积，最终新请求
    挂起、全站"点击多了无法访问"。信号量限并发（超限快速断开保活，而非排队）；
    daemon_threads 避免卡死请求线程阻塞关闭。
    """

    daemon_threads = True
    request_queue_size = 64  # listen backlog（默认 5 太小，accept 风暴丢连接）

    def __init__(self, *args, max_concurrency: int = 64, **kwargs):
        super().__init__(*args, **kwargs)
        self._slots = threading.Semaphore(max_concurrency)

    def process_request(self, request, client_address):
        if not self._slots.acquire(blocking=False):
            try:  # 并发已满：快速断开（客户端可重试），不排队挂起
                request.close()
            except OSError:
                pass
            return
        super().process_request(request, client_address)

    def process_request_thread(self, request, client_address):
        try:
            super().process_request_thread(request, client_address)
        finally:
            self._slots.release()  # 信号量覆盖完整请求生命周期


def main():
    # 0.9.13：注册 SIGUSR1 → 全部线程栈转储到 stderr（docker logs 可见）。
    # 冻结/卡顿时执行 docker exec stockdb sh -c 'kill -USR1 $(pgrep -f "python /opt/webui/app.py")'
    # 即可定位卡点（如 C 扩展帧/锁等待），无需重启进程。
    try:
        import faulthandler
        faulthandler.register(signal.SIGUSR1, all_threads=True)
    except Exception:  # noqa: BLE001 - 转储能力缺失不影响主流程
        pass
    _wire_auction_tasks()  # 组合根：服务层依赖注入（0.9.2 批次 4）
    _wire_warehouse_tasks()  # 组合根：仓库层注入（0.10.0 W4）
    print(f"webui listening on 0.0.0.0:{LISTEN_PORT}", file=sys.stderr)
    print(f"stockdb: {STOCKDB_HOST}:{STOCKDB_PORT}（同容器进程）| data: {DATA_DIR}", file=sys.stderr)
    threading.Thread(target=scheduler_loop, daemon=True).start()
    threading.Thread(target=ops_watchdog_loop, daemon=True).start()     # 运营支撑看门狗（告警生产接线）
    threading.Thread(target=auction_scheduler_loop, daemon=True).start()  # 打板竞价调度（2s 轮询，独立线程）
    if WAREHOUSE_ENABLED:  # 0.10.0：仓库沉淀调度（5s 轮询；回滚演练 = WAREHOUSE_ENABLED=0）
        threading.Thread(target=warehouse_scheduler_loop, daemon=True).start()
    # 0.9.11：Handler 延迟装配（app 模块已完整）——handlers.py 顶层 import app，
    # 脚本方式执行时必须在 app 完整后导入，否则循环重载 ImportError（0.9.10 实证）
    from interfaces.web.handlers import Handler  # noqa: E402 - 组合根装配（app 已完整）
    # 0.9.12：并发有界 server（防线程风暴堆积；见 _BoundedHTTPServer）
    _BoundedHTTPServer(("0.0.0.0", LISTEN_PORT), Handler).serve_forever()


if __name__ == "__main__":
    # W1 修复（0.10.24）：单实例别名——脚本直跑时本模块叫 __main__，而
    # handlers.py `import app` 会再载入第二份实例：调度线程在 __main__ 里
    # 更新的 _scheduler_alive/_scheduler_heartbeat/_sync_state 对 handlers
    # 永不可见（实机实证 scheduler_alive 恒 False、调度运行态恒 idle，
    # 0.10.23 的 from-import 修复只解了一半）。别名后 import app → __main__，
    # 全局态单实例。
    sys.modules.setdefault("app", sys.modules["__main__"])
    main()
