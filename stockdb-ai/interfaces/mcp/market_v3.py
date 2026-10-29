#!/usr/bin/env python3
"""market_v3 — 公开行情合同 stockdb-market-v3（修订 3.0.0）的服务端分支实现。

由 stockdb_mcp_server._call_tool 经 maybe_dispatch 接入：请求显式携带
contract_version="stockdb-market-v3" 时走本模块（四个首批工具 get_kline /
get_stock_list / get_trading_days / get_data_status），不带该字段的请求保持
旧版路径与旧返回形状，互不影响。合同机器件（schema/examples/验收用例/离线
校验器）在同目录 contract/，与消费方交付包字节级一致（MANIFEST.sha256）。

设计要点（与合同 README 逐节对应）：
  - 规范证券键 <exchange>/<asset_type>/<namespace>/<native_code>；裸六位代码
    报 AMBIGUOUS_INSTRUMENT，目录外/段不符的键报 UNKNOWN_INSTRUMENT，未声明
    资产/交易所报 UNSUPPORTED_ASSET（README §5）。
  - 日K统一信封：query/request_hash/data/meta/errors/source_catalog/data_hash，
    逐证券 coverage/gaps/units/adjustment（README §6-§7）。
  - 来源/时间/哈希/快照：canonical_json_v1 哈希、不可变快照≥7天、
    retrieved_at/served_at/published_at 语义分离（README §10）。
  - 服务运行合同：max_codes=50、行情并发 4（满载 BUSY）、单请求期限 30s、
    单响应行数上限 200k、快照保留 604800s（README §13）。

数据能力（core_daily_v1 首批）：XSHG/XSHE × equity/etf × exchange × 1d × none。
沪深股票/ETF 未复权日K逐码读取引擎 HTTP（不依赖 pybao SDK，避开其批量
pipe 间歇性空结果缺陷，见 CHANGELOG 0.13.0 / 验收 L01-L02 根因）；身份经
引擎目录（股票代码表）+ 官方代码段表双重核验；bar_state 依据盘后同步日志
与后续交易日在库事实，无依据时如实 unknown，不伪标 final。
"""

from __future__ import annotations

import hashlib
import json
import os
import re
import sys
import threading
import time
from datetime import datetime
from decimal import Decimal
from pathlib import Path
from typing import Any

# 0.14.0：补充合同 stockdb-market-v3-research-extension 1.0.0（A 指数/板块日K +
# B 复权因子与 R/F/A 快照链）。research 缺失只影响扩展分支，core 不受影响。
try:  # noqa: E402
    from interfaces.mcp import market_v3_research as research  # noqa: F401
except Exception as _research_import_exc:  # noqa: BLE001
    research = None  # type: ignore[assignment]
    print(f"market_v3: market_v3_research 加载失败（扩展分支不可用）: "
          f"{_research_import_exc}", file=sys.stderr)

# ---------------------------------------------------------------------------
# 合同常量与运行限额（README §13 首批基准；环境变量可覆盖，状态工具如实申报）
# ---------------------------------------------------------------------------

CONTRACT_VERSION = "stockdb-market-v3"
CONTRACT_REVISION = "3.0.0"
FIELDS_ORDER = ("open", "high", "low", "close", "volume", "amount")
RETRYABLE_CODES = frozenset({"NOT_PUBLISHED", "SOURCE_UNAVAILABLE",
                              "SOURCE_TIMEOUT", "BUSY"})


def _env_int(name: str, default: int) -> int:
    raw = os.environ.get(name)
    if raw is None or raw == "":
        return default
    try:
        return int(raw)
    except ValueError:
        return default


MAX_CODES = 50
MAX_CONCURRENCY = _env_int("STOCKDB_V3_MAX_CONCURRENCY", 4)
REQUEST_TIMEOUT_MS = _env_int("STOCKDB_V3_REQUEST_TIMEOUT_MS", 30000)
MAX_RESPONSE_ROWS = _env_int("STOCKDB_V3_MAX_RESPONSE_ROWS", 200000)
SNAPSHOT_RETENTION_SECONDS = _env_int("STOCKDB_V3_SNAPSHOT_RETENTION_SECONDS",
                                      604800)
# 快照仓总行数/条数上限（内存防护；超限先淘汰最旧；到期的总是先淘汰）
_SNAPSHOT_ROW_CAP = _env_int("STOCKDB_V3_SNAPSHOT_ROW_CAP", 400000)
_SNAPSHOT_COUNT_CAP = _env_int("STOCKDB_V3_SNAPSHOT_COUNT_CAP", 64)
# 行情请求槽（max_concurrency）：满载且有界等待后仍拿不到 → BUSY
_KLINE_SLOT_WAIT_SECONDS = 2.0
# 引擎健康缓存刷新周期（状态工具只读缓存，绝不同步现查全部上游）
_HEALTH_REFRESH_SECONDS = 60.0
_HEALTH_PROBE_TIMEOUT = 5.0
# 目录读取失败后的短冷却（避免引擎故障期每请求都重试目录读取）
_CATALOG_FAIL_COOLDOWN = 10.0
_CATALOG_TTL = 300.0

# 日历已核实边界（core/calendar_market 的 XSHG 休市表，2024-01-01..2026-12-31，
# 与 exchange_calendars 4.13.2 逐年核对；2027 官方安排公布后由提取脚本更新）
CALENDAR_COVERAGE_START = "2024-01-01"
CALENDAR_COVERAGE_END = "2026-12-31"
CALENDAR_ID = "xshg-holiday-table"


def _calendar():
    """A 股交易日历（core 领域层；延迟导入避免测试环境路径问题）。"""
    from core import calendar_xshg
    return calendar_xshg


# ---------------------------------------------------------------------------
# canonical_json_v1 与确定性哈希（README §10.2；与 validate_contract.py 同算法）
# ---------------------------------------------------------------------------


def _inspect_canonical(value: Any) -> None:
    if type(value) is float:  # noqa: E721 - 排除浮点进入哈希
        raise ValueError("floating point is forbidden in hashed data/query")
    if type(value) is int and abs(value) > 2**53 - 1:
        raise ValueError("integer exceeds canonical safe range")
    if isinstance(value, dict):
        for key, child in value.items():
            if not isinstance(key, str):
                raise ValueError("JSON keys must be strings")
            _inspect_canonical(child)
    elif isinstance(value, list):
        for child in value:
            _inspect_canonical(child)


def canonical_json(value: Any) -> bytes:
    _inspect_canonical(value)
    return json.dumps(value, ensure_ascii=False, sort_keys=True,
                      separators=(",", ":"), allow_nan=False).encode("utf-8")


def digest(value: Any) -> str:
    return hashlib.sha256(canonical_json(value)).hexdigest()


# ---------------------------------------------------------------------------
# 十进制字符串格式化（README §6.1：原始精度、无科学计数、不预四舍五入）
# ---------------------------------------------------------------------------


def decimal_str(value: Any) -> str:
    """int/float/str → 十进制字符串（无科学计数；整数值不带小数点）。"""
    if isinstance(value, bool):
        raise ValueError("boolean is not a decimal value")
    if isinstance(value, int):
        return str(value)
    if isinstance(value, float):
        text = format(Decimal(repr(value)), "f")
    elif isinstance(value, str):
        text = value
    else:
        raise ValueError(f"unformattable numeric value: {type(value).__name__}")
    d = Decimal(text)
    if d == d.to_integral_value():
        return str(d.to_integral_value())
    return format(d.normalize(), "f")


def _price_decimals(asset_type: str) -> int:
    return 3 if asset_type == "etf" else 2


# ---------------------------------------------------------------------------
# 证券身份：官方代码段表 + 引擎目录（README §5）
# ---------------------------------------------------------------------------

# 三位前缀 → (exchange, asset_type)。资产类型依据沪深交易所公开的证券代码
# 分配规则（上交所/深交所编号计划为公开文件），并经引擎目录实测核对：
# 沪市 510-518/520/526/530/551/560-563/588/589 与深市 159 为 ETF；501/502/506
# 与 160-169 为 LOF/封闭基金，508 与 180 为 REITs——这些不是 equity 也不是
# etf，本批不声明支持（请求报 UNKNOWN_INSTRUMENT，不冒充）。
# 920 为北交所，本批未验收（UNSUPPORTED_ASSET）。
_SEGMENT_TABLE: dict[str, tuple[str, str]] = {}
for _p in ("000", "001", "002", "003", "300", "301", "302"):
    _SEGMENT_TABLE[_p] = ("XSHE", "equity")
_SEGMENT_TABLE["159"] = ("XSHE", "etf")
for _p in ("600", "601", "603", "605", "688", "689"):
    _SEGMENT_TABLE[_p] = ("XSHG", "equity")
for _p in ("510", "511", "512", "513", "515", "516", "517", "518", "520",
           "526", "530", "551", "560", "561", "562", "563", "588", "589"):
    _SEGMENT_TABLE[_p] = ("XSHG", "etf")
# 非本批资产（LOF/封闭/REIT/北交所）：显式登记，身份分类明确拒绝
_FUND_SEGMENTS = frozenset(
    ["160", "161", "162", "163", "164", "165", "166", "167", "168", "169",
     "180", "501", "502", "506", "508"])
_BJ_SEGMENTS = frozenset(["920"])

_INSTRUMENT_ID_RE = re.compile(
    r"^(XSHG|XSHE|XBSE|CN)/(equity|etf|index|board)/([a-z][a-z0-9_-]*)/([A-Za-z0-9._-]+)$")


class V3Error(Exception):
    """合同错误（README §9）：code/instrument_id/details 随信封返回。"""

    def __init__(self, code: str, message: str, *,
                 instrument_id: str | None = None,
                 details: dict | None = None):
        super().__init__(message)
        self.code = code
        self.message = message
        self.instrument_id = instrument_id
        self.retryable = code in RETRYABLE_CODES
        self.details = details or {}

    def to_error_object(self) -> dict:
        return {"code": self.code, "message": self.message,
                "instrument_id": self.instrument_id,
                "retryable": self.retryable, "details": self.details}


def parse_instrument_id(value: Any, *, argument: str = "code") -> tuple[str, str, str, str]:
    """解析规范证券键；裸代码→AMBIGUOUS_INSTRUMENT，其余格式错误→INVALID_ARGUMENT。"""
    if not isinstance(value, str) or not value:
        raise V3Error("INVALID_ARGUMENT",
                      f"{argument} 必须是非空字符串（规范证券键）",
                      details={"argument": argument, "received": repr(value)[:80]})
    match = _INSTRUMENT_ID_RE.match(value)
    if match:
        return match.group(1), match.group(2), match.group(3), match.group(4)
    if "/" not in value and re.fullmatch(r"[0-9A-Za-z._-]+", value):
        # 裸代码（含 000001 / sh000001 等未限定交易所/资产的标识）：歧义
        raise V3Error("AMBIGUOUS_INSTRUMENT",
                      "v3 需要规范证券键 <exchange>/<asset_type>/<namespace>/<code>，"
                      "裸代码无法区分同号证券（如股票与指数 000001）",
                      details={"argument": argument, "received": value})
    raise V3Error("INVALID_ARGUMENT",
                  f"{argument} 不是合法的规范证券键",
                  details={"argument": argument, "received": value[:80]})


def _segment_of(native_code: str) -> tuple[str, str] | None:
    return _SEGMENT_TABLE.get(native_code[:3])


class Identity:
    """已解析的证券身份。verified=False 时 identity_source_id=None。"""

    def __init__(self, instrument_id: str, exchange: str, asset_type: str,
                 namespace: str, code: str, *, verified: bool,
                 volume_unit: str):
        self.instrument_id = instrument_id
        self.exchange = exchange
        self.asset_type = asset_type
        self.namespace = namespace
        self.code = code
        self.verified = verified
        self.volume_unit = volume_unit


def identity_from_key(key: str, *, verified: bool = False,
                      volume_unit: str | None = None) -> Identity:
    """从规范键构造（未核验的）Identity。键此时必已通过格式解析。

    量纲是数据集语义（按资产类型），与身份是否核验无关：error meta 对
    equity/etf 仍声明 shares/fund_units（validator 对核心资产强制要求）。
    """
    exchange, asset_type, namespace, native = parse_instrument_id(key)
    if volume_unit is None:
        volume_unit = {"etf": "fund_units",
                       "equity": "shares"}.get(asset_type, "unknown")
    return Identity(key, exchange, asset_type, namespace, native,
                    verified=verified, volume_unit=volume_unit)


def resolve_identity(key: str, *, catalog: set[str] | None) -> Identity:
    """规范键 → 身份。catalog 为 None 表示目录不可用（降级 SOURCE_UNAVAILABLE）。

    核验链：代码在引擎目录（提供方清单）∧ 官方代码段表给出的交易所/资产与
    键声明一致 → verified；段表/目录/键三方不一致 → UNKNOWN_INSTRUMENT。
    指数/板块（0.14.0 A 批）路由到 research 序列目录（显式白名单 + 公开证据），
    与股票/ETF 身份链完全隔离（I02：同码不同资产不串）。
    """
    exchange, asset_type, namespace, native = parse_instrument_id(key)
    if asset_type in ("index", "board"):
        if research is None:
            raise V3Error(
                "UNSUPPORTED_ASSET",
                "指数/板块能力（index_board_daily_v1）未加载（research 模块缺失）",
                instrument_id=key,
                details={"exchange": exchange, "asset_type": asset_type})
        kind, _native, _name, calendar_exchange = (
            research.resolve_research_identity(key))
        return Identity(key, exchange, asset_type, namespace, native,
                        verified=True, volume_unit="unknown")
    if exchange in ("XBSE", "CN"):
        raise V3Error(
            "UNSUPPORTED_ASSET",
            "资产/交易所不在 core_daily_v1 首批能力内"
            "（首批仅 XSHG/XSHE × equity/etf × 1d × none；"
            "北交所/CN 按合同逐项独立验收后开放）",
            instrument_id=key,
            details={"exchange": exchange, "asset_type": asset_type})
    if namespace != "exchange":
        raise V3Error(
            "UNSUPPORTED_ASSET",
            f"命名空间 {namespace!r} 不在已声明能力内（首批仅 exchange）",
            instrument_id=key,
            details={"namespace": namespace})
    if asset_type not in ("equity", "etf") or exchange not in ("XSHG", "XSHE"):
        raise V3Error("UNSUPPORTED_ASSET",
                      "资产/交易所组合不在首批能力内",
                      instrument_id=key,
                      details={"exchange": exchange, "asset_type": asset_type})
    if native[:3] in _FUND_SEGMENTS:
        raise V3Error("UNKNOWN_INSTRUMENT",
                      f"{native} 属于 LOF/封闭基金/REITs 代码段（{native[:3]}xxx），"
                      "不是股票或 ETF，身份无法按声明的资产类型核验",
                      instrument_id=key,
                      details={"segment": native[:3],
                               "reason": "non_core_fund_segment"})
    if native[:3] in _BJ_SEGMENTS:
        raise V3Error("UNSUPPORTED_ASSET",
                      f"{native} 为北交所代码，北交所未在本批验收",
                      instrument_id=key,
                      details={"segment": native[:3]})
    if catalog is None:
        raise V3Error("SOURCE_UNAVAILABLE",
                      "身份目录（引擎股票代码表）暂不可用，无法核验证券身份",
                      instrument_id=key,
                      details={"reason": "identity_catalog_unavailable"})
    segment = _segment_of(native)
    if segment is None:
        raise V3Error("UNKNOWN_INSTRUMENT",
                      f"{native} 不在已核验的代码段表内",
                      instrument_id=key,
                      details={"reason": "segment_unknown"})
    if native not in catalog:
        raise V3Error("UNKNOWN_INSTRUMENT",
                      f"{native} 不在提供方证券目录内（目录未收录）",
                      instrument_id=key,
                      details={"reason": "not_in_catalog"})
    seg_exchange, seg_asset = segment
    if (seg_exchange, seg_asset) != (exchange, asset_type):
        raise V3Error("UNKNOWN_INSTRUMENT",
                      f"代码段表判定 {native} 为 {seg_exchange}/{seg_asset}，"
                      f"与请求键 {exchange}/{asset_type} 不一致",
                      instrument_id=key,
                      details={"reason": "segment_mismatch",
                               "segment_exchange": seg_exchange,
                               "segment_asset": seg_asset})
    volume_unit = "fund_units" if asset_type == "etf" else "shares"
    return Identity(key, exchange, asset_type, namespace, native,
                    verified=True, volume_unit=volume_unit)


# ---------------------------------------------------------------------------
# 引擎适配层（薄封装；测试通过替换这些函数注入故障/桩数据）
# ---------------------------------------------------------------------------

# 测试故障注入钩子：{"sleep_codes": {code: ms}, "fail_codes": {code: 错误码},
#                   "fail_messages": {code: 文案}, "empty_codes": {code: [dates]}}
TEST_FAULTS: dict[str, Any] = {}

_GATE_BUSY_MARKERS = ("并发已满", "熔断")
_TIMEOUT_MARKERS = ("timed out", "timeout", "TimeoutError", "超时")


class EngineFailure(Exception):
    """引擎读取失败（已分类为合同错误码）。"""

    def __init__(self, code: str, message: str):
        super().__init__(message)
        self.code = code
        self.message = message


def _classify_engine_exc(exc: Exception) -> EngineFailure:
    if isinstance(exc, EngineFailure):
        return exc
    text = f"{type(exc).__name__}: {exc}"
    if any(marker in text for marker in _TIMEOUT_MARKERS):
        return EngineFailure("SOURCE_TIMEOUT", f"引擎读取超时: {text[:200]}")
    if any(marker in text for marker in _GATE_BUSY_MARKERS):
        return EngineFailure("BUSY", "引擎访问闸门已满（并发资源占用中）")
    return EngineFailure("SOURCE_UNAVAILABLE", f"引擎不可用: {text[:200]}")


def _srv():
    """宿主 server 模块（优先复用已在运行的实例，避免 standalone 模式二次导入）。

    standalone（python stockdb_mcp_server.py）时 server 是 __main__；
    webui/包模式下是 interfaces.mcp.stockdb_mcp_server。
    """
    main_mod = sys.modules.get("__main__")
    if main_mod is not None and hasattr(main_mod, "_range_prefixes"):
        return main_mod
    mod = sys.modules.get("interfaces.mcp.stockdb_mcp_server")
    if mod is not None and hasattr(mod, "_range_prefixes"):
        return mod
    from interfaces.mcp import stockdb_mcp_server as mod  # noqa: PLC0415
    return mod


def _engine_base() -> str:
    return _srv()._base_url()


def engine_get(table: str, *, cmd: str = "get", timeout: float = 15.0) -> Any:
    """经统一闸口读取引擎表（block=False：闸门满→BUSY，不无限排队）。"""
    import urllib.parse
    from storage.providers import free_stockdb as fs
    path = f"/?cmd={cmd}&t={urllib.parse.quote(table)}"
    try:
        raw = fs.fetch(path, timeout=timeout,
                       base=_engine_base().removeprefix("http://"), block=False)
    except Exception as exc:  # noqa: BLE001 - 统一分类为合同错误码
        raise _classify_engine_exc(exc) from exc
    if not raw.strip():
        return None
    try:
        return json.loads(raw)
    except json.JSONDecodeError as exc:
        raise EngineFailure("SOURCE_SCHEMA_ERROR",
                            f"引擎响应无法解析: {exc}") from exc


def _normalize_rows(data: Any) -> list[dict]:
    if isinstance(data, list):
        return [r for r in data if isinstance(r, dict)]
    if isinstance(data, dict):
        return [data] if "date" in data else []
    return []


def _row_date_key(row: dict) -> int:
    value = row.get("date", row.get("date8"))
    try:
        return int("".join(ch for ch in str(value) if ch.isdigit())[:8])
    except (TypeError, ValueError):
        return 0


def engine_daily_rows(code: str, start: str, end: str,
                      *, timeout: float = 15.0) -> list[dict]:
    """逐码读取闭区间 [start,end] 的引擎日K行（升序、去重、客户端过滤）。

    不走 pybao SDK 批量 pipe：其 rd.pipe()+mget 间歇性返回全空（容器内实测
    10/20 空，是 legacy 批量空数据 L01/L02 的根因）；引擎 HTTP 单表读取稳定
    （实测 0/100 失败）。
    """
    fault = TEST_FAULTS.get("sleep_codes", {}).get(code)
    if fault:
        time.sleep(fault / 1000.0)
    fault_code = TEST_FAULTS.get("fail_codes", {}).get(code)
    if fault_code:
        raise EngineFailure(fault_code,
                            TEST_FAULTS.get("fail_messages", {}).get(
                                code, f"injected failure for {code}"))
    srv = _srv()
    rows: list[dict] = []
    for prefix in srv._range_prefixes(start, end):
        data = engine_get(f"日k:{code}:{prefix}*", cmd="vals", timeout=timeout)
        rows.extend(_normalize_rows(data))
    s, e = int(start), int(end)
    by_date: dict[int, dict] = {}
    for row in rows:
        key = _row_date_key(row)
        if s <= key <= e and key:
            by_date[key] = row  # 同日重复以最后一次为准（引擎不应重复）
    return [by_date[k] for k in sorted(by_date)]


_catalog_cache: dict[str, Any] = {"codes": None, "iso": None, "ok_at": 0.0,
                                  "fail_until": 0.0}
_catalog_lock = threading.Lock()


def catalog_codes(*, max_age: float = _CATALOG_TTL) -> tuple[set[str] | None, str]:
    """引擎证券目录（股票代码表），TTL 缓存。返回 (codes|None, retrieved_at)。

    读取失败短冷却内直接返回上次结果（可能为 None=不可用），不抛异常；
    调用方按「身份不可核验」降级为逐码 SOURCE_UNAVAILABLE。
    """
    now = time.time()
    with _catalog_lock:
        if (_catalog_cache["codes"] is not None
                and now - _catalog_cache["ok_at"] <= max_age):
            return set(_catalog_cache["codes"]), _catalog_cache["iso"]
        if now < _catalog_cache["fail_until"]:
            return (set(_catalog_cache["codes"])
                    if _catalog_cache["codes"] is not None else None,
                    _catalog_cache["iso"] or _now_iso())
    iso = _now_iso()
    codes: set[str] | None = None
    try:
        data = engine_get("股票代码", timeout=10.0)
        found: set[str] = set()
        if isinstance(data, dict):
            for group in data.values():
                if isinstance(group, list):
                    found.update(str(c) for c in group)
        codes = found or None
    except EngineFailure:
        codes = None
    with _catalog_lock:
        if codes is not None:
            _catalog_cache.update(codes=codes, iso=iso, ok_at=now,
                                  fail_until=0.0)
            return set(codes), iso
        _catalog_cache["fail_until"] = now + _CATALOG_FAIL_COOLDOWN
        return (set(_catalog_cache["codes"])
                if _catalog_cache["codes"] is not None else None,
                _catalog_cache["iso"] or iso)


def engine_latest_trade_date(*, timeout: float = 5.0) -> str | None:
    """引擎最新交易日（探 000001 近三个月前缀取最大 date）。"""
    now = datetime.now()
    latest = None
    for offset in (0, 1, 2):
        idx = now.year * 12 + (now.month - 1) - offset
        y, m = divmod(idx, 12)
        y, m = y, m + 1
        try:
            data = engine_get(f"日k:000001:{y:04d}{m:02d}*", cmd="vals",
                              timeout=timeout)
        except EngineFailure:
            continue
        for row in _normalize_rows(data):
            d = str(row.get("date"))[:8]
            if d.isdigit() and (latest is None or d > latest):
                latest = d
    return latest


_sync_log_cache: dict[str, Any] = {"at": 0.0, "successes": []}
_SYNC_SUCCESS_RE = re.compile(
    r"^(\d{4}-\d{2}-\d{2}) (\d{2}:\d{2}:\d{2}).*Success: 同步已 100% 完成")


def _sync_log_path() -> Path:
    try:
        import config
        return Path(config.DATA_DIR) / "sync.log"
    except Exception:  # noqa: BLE001 - 裁剪部署无 config
        return Path(os.environ.get("STOCKDB_SYNC_LOG", "/data/sync.log"))


def sync_successes() -> list[tuple[str, str]]:
    """解析同步日志中的成功同步 (date, time) 列表（缓存 60s）。

    盘后同步完成时刻是 bar_state=final 的可核验冻结依据（README §6.1：
    盘后查询时间本身不足以证明 final——依据是「盘后同步已完成」这一事件，
    而非当前时刻）。
    """
    now = time.time()
    if now - _sync_log_cache["at"] < 60.0:
        return _sync_log_cache["successes"]
    successes: list[tuple[str, str]] = []
    try:
        with open(_sync_log_path(), "r", encoding="utf-8", errors="replace") as fh:
            for line in fh:
                m = _SYNC_SUCCESS_RE.match(line.strip())
                if m:
                    successes.append((m.group(1), m.group(2)))
    except OSError:
        successes = []
    _sync_log_cache.update(at=now, successes=successes)
    return successes


def engine_version_label() -> str | None:
    """运行中引擎版本（如 '0.3.5-stockdb'；不可得返回 None）。"""
    try:
        from storage.providers import free_stockdb as fs
        info = fs.engine_version_info()
        return info.get("version") if info else None
    except Exception:  # noqa: BLE001
        return None


# ---------------------------------------------------------------------------
# 引擎健康缓存（README §8.3：状态只读有界缓存，绝不同步现查全部上游）
# ---------------------------------------------------------------------------


class _EngineHealth:
    """后台线程定期探测引擎（最新交易日），get() 只读缓存、恒快返回。"""

    def __init__(self) -> None:
        self._lock = threading.Lock()
        self._state: dict[str, Any] = {"latest": None, "checked_at": None,
                                       "ok": False, "error": None}
        self._thread: threading.Thread | None = None

    def _refresh(self) -> None:
        try:
            latest = engine_latest_trade_date(timeout=_HEALTH_PROBE_TIMEOUT)
            with self._lock:
                self._state.update(
                    latest=latest, ok=latest is not None,
                    error=None if latest else "engine returned no data",
                    checked_at=_now_iso())
        except Exception as exc:  # noqa: BLE001
            with self._lock:
                self._state.update(ok=False, error=str(exc)[:200],
                                   checked_at=_now_iso())

    def _loop(self) -> None:
        while True:
            self._refresh()
            time.sleep(_HEALTH_REFRESH_SECONDS)

    def ensure_started(self) -> None:
        with self._lock:
            if self._thread is None or not self._thread.is_alive():
                self._thread = threading.Thread(
                    target=self._loop, name="market-v3-health", daemon=True)
                self._thread.start()

    def get(self) -> dict[str, Any]:
        self.ensure_started()
        with self._lock:
            return dict(self._state)

    def inject(self, latest: str | None) -> None:
        """测试注入：直接设置缓存状态。"""
        with self._lock:
            self._state.update(latest=latest, ok=latest is not None,
                               error=None if latest else "injected",
                               checked_at=_now_iso())


HEALTH = _EngineHealth()


# ---------------------------------------------------------------------------
# 快照仓（README §10.3：不可变、≥7 天保留、重启丢失明确失败）
# ---------------------------------------------------------------------------


class SnapshotStore:
    def __init__(self) -> None:
        self._lock = threading.Lock()
        self._items: dict[str, dict] = {}

    def _purge_locked(self) -> None:
        now = time.time()
        for k in [k for k, v in self._items.items() if v["expires_at"] <= now]:
            del self._items[k]
        total_rows = sum(v.get("row_count", 0) for v in self._items.values())
        while ((len(self._items) > _SNAPSHOT_COUNT_CAP
                or total_rows > _SNAPSHOT_ROW_CAP) and self._items):
            oldest = min(self._items, key=lambda k: self._items[k]["created_at"])
            total_rows -= self._items[oldest].get("row_count", 0)
            del self._items[oldest]

    def capture(self, snapshot_id: str, payload: dict, *,
                row_count: int = 0) -> None:
        now = time.time()
        with self._lock:
            self._items[snapshot_id] = {
                "payload": payload, "row_count": row_count,
                "created_at": now,
                "expires_at": now + SNAPSHOT_RETENTION_SECONDS,
            }
            self._purge_locked()

    def get(self, snapshot_id: str) -> dict | None:
        """取快照（含已过期项——由调用方按 expires_at 区分 EXPIRED/NOT_FOUND）。"""
        with self._lock:
            return self._items.get(snapshot_id)

    @staticmethod
    def is_expired(item: dict) -> bool:
        return item["expires_at"] <= time.time()

    def new_id(self, kind: str) -> str:
        import uuid
        return f"snap-{kind}-{uuid.uuid4().hex[:12]}"

    def size(self) -> int:
        with self._lock:
            return len(self._items)

    def clear(self) -> None:
        """测试辅助：清空快照仓（模拟服务重启丢失快照）。"""
        with self._lock:
            self._items.clear()


SNAPSHOTS = SnapshotStore()

# 0.14.0（扩展 A15）：快照仓升级为「内存 + 磁盘」持久实现——正常进程/容器重启后
# 未到期快照（含 R/F/A）仍可按 id 重读；过期/丢失仍明确失败，不同 id 返回新内容。
# 基线允许重启丢快照（§10.3「服务重启若丢失快照也必须明确失败」），本实现更严。
if research is not None:
    SNAPSHOTS = research.PersistentSnapshotStore()  # type: ignore[assignment]


# ---------------------------------------------------------------------------
# 公共信封构件
# ---------------------------------------------------------------------------

_process_started_at = datetime.now().astimezone()


def _now_iso() -> str:
    return datetime.now().astimezone().isoformat(timespec="seconds")


def _server_identity() -> dict:
    srv = _srv()
    build_id = os.environ.get("STOCKDB_BUILD_ID") or ""
    commit = os.environ.get("STOCKDB_BUILD_COMMIT") or ""
    if not build_id:
        build_id = f"src+{srv.SERVER_VERSION}+{commit[:12] if commit else 'nogit'}"
    return {"name": srv.SERVER_NAME, "version": srv.SERVER_VERSION,
            "build_id": build_id, "source_commit": commit or None,
            "image_digest": os.environ.get("STOCKDB_IMAGE_DIGEST") or None,
            "started_at": _process_started_at.isoformat()}


def _new_request_id() -> str:
    import uuid
    return f"req-{uuid.uuid4().hex[:16]}"


def _base_header(status: str, *, errors: list[dict],
                 snapshot_id: str | None, snapshot_expires_at: str | None,
                 data_hash: str, truncated: bool = False) -> dict:
    srv = _srv()
    return {
        "contract_version": CONTRACT_VERSION,
        "contract_revision": CONTRACT_REVISION,
        "server_version": srv.SERVER_VERSION,
        "server_build_id": _server_identity()["build_id"],
        "request_id": _new_request_id(),
        "served_at": _now_iso(),
        "snapshot_id": snapshot_id,
        "snapshot_expires_at": snapshot_expires_at,
        "status": status,
        "is_partial": status != "complete",
        "truncated": truncated,
        "errors": errors,
        "source_catalog": {},
        "data_hash": data_hash,
    }


def tool_failure(errors: list[V3Error]) -> dict:
    """ToolFailureResponse（README §4.3：query/request_hash/snapshot 均 null）。"""
    objs = [e.to_error_object() for e in errors]
    header = _base_header("failed", errors=objs, snapshot_id=None,
                          snapshot_expires_at=None, data_hash=digest({}))
    header.update({"query": None, "request_hash": None, "data": {}, "meta": {}})
    return header


def _bars_finality_evidence() -> str:
    """bars 来源的最终性依据（README §10.1：发布机制可核验，非「现在已过收盘」）。

    引擎日K仅由「数据更新」同步工具写入；镜像源按日发布已完成收盘的数据
    （镜像页「数据更新至」声明 + 同步日志）。final 行的依据链 =
    该发布机制 + （同日行：当日 ≥15:00 的成功同步记录 / 更早行：后续交易日已在库）。
    """
    evidence = ("engine daily bars are populated exclusively by the post-close "
                "incremental sync tool (数据更新; sync log /data/sync.log; "
                "mirror publishes completed trading days only)")
    successes = sync_successes()
    if successes:
        day, ts = successes[-1]
        evidence += f"; last logged successful sync {day} {ts}"
    return evidence


IDENTITY_SOURCE_ID = "ident-engine-catalog"
SEGMENT_SOURCE_ID = "ident-code-segments"
BARS_SOURCE_ID = "engine-daily-kline"


def _source_record(*, retrieved_at: str, reference: str,
                   finality: str | None = None,
                   aggregator_extra: str | None = None) -> dict:
    version = engine_version_label()
    aggregator = "free-stockdb" + (f" {version}" if version else "")
    if aggregator_extra:
        aggregator += aggregator_extra
    record = {
        "provider": None,
        "aggregator": aggregator,
        "access_path": "http",
        "reference": reference,
        "retrieved_at": retrieved_at,
        "source_updated_at": None,
        "published_at": None,
        "finality_evidence": finality,
        "unknown_fields": ["provider", "source_updated_at", "published_at"],
    }
    if finality is None:
        record["unknown_fields"] = [*record["unknown_fields"],
                                    "finality_evidence"]
    return record


def _identity_source(retrieved_at: str) -> dict:
    return _source_record(
        retrieved_at=retrieved_at,
        reference="urn:stockdb:engine:股票代码 (provider securities catalog table)")


def _segment_source(retrieved_at: str) -> dict:
    return _source_record(
        retrieved_at=retrieved_at,
        reference=("SSE/SZSE public securities code allocation plans "
                   "(code-segment rules; mapping table shipped in "
                   "interfaces/mcp/market_v3.py _SEGMENT_TABLE)"))


def _identity_dict(identity: Identity) -> dict:
    return {"instrument_id": identity.instrument_id, "code": identity.code,
            "exchange": identity.exchange, "asset_type": identity.asset_type,
            "namespace": identity.namespace, "verified": identity.verified,
            "volume_unit": identity.volume_unit}


def _bar_state_for(max_row_date: str, latest: str | None) -> str:
    """final 需可核验冻结依据（README §6.1），无依据时 unknown，不伪标。

    依据链：引擎日K仅由盘后增量同步写入（发布机制）；行日期 < 引擎最新交易日
    （后续交易日已在库 = 该会话已完结冻结），或行日期 = 最新交易日且当日存在
    ≥15:00（收盘）的成功同步记录 → final。
    """
    if latest is not None and max_row_date < latest:
        return "final"
    for day, ts in reversed(sync_successes()):
        if day.replace("-", "") == max_row_date and ts >= "15:00:00":
            return "final"
    return "unknown"


# ---------------------------------------------------------------------------
# get_kline v3（README §6-§7）
# ---------------------------------------------------------------------------

_kline_slots = threading.BoundedSemaphore(MAX_CONCURRENCY)


def _date8(value: Any, field: str) -> str:
    if not isinstance(value, str) or not re.fullmatch(r"\d{8}", value):
        raise V3Error("INVALID_ARGUMENT",
                      f"{field} 必须是 8 位真实日期 YYYYMMDD",
                      details={"argument": field, "received": repr(value)[:80]})
    try:
        parsed = datetime.strptime(value, "%Y%m%d").date()
    except ValueError:
        raise V3Error("INVALID_ARGUMENT",
                      f"{field} 不是真实日期: {value}",
                      details={"argument": field, "received": value}) from None
    if parsed.strftime("%Y%m%d") != value:
        raise V3Error("INVALID_ARGUMENT",
                      f"{field} 不是规范日期: {value}",
                      details={"argument": field, "received": value})
    return value


def _iso(date8_str: str) -> str:
    return f"{date8_str[:4]}-{date8_str[4:6]}-{date8_str[6:8]}"


def _validate_kline_request(args: dict) -> dict:
    """正规化校验；非法抛 V3Error。返回 NormalizedQuery（请求顺序保持）。"""
    allowed = {"contract_version", "code", "codes", "frequency", "start", "end",
               "fq", "fields", "limit", "snapshot_id", "adjustment_anchor",
               "factor_snapshot_id"}
    unknown = set(args) - allowed
    if unknown:
        raise V3Error("INVALID_ARGUMENT",
                      f"未知参数: {sorted(unknown)}",
                      details={"unknown_parameters": sorted(unknown)})

    # 显式 null 与缺省不等价（合同 Request 字段均不可空）：schema 与运行时
    # 同判 INVALID_ARGUMENT，避免「schema 拒绝、运行时当缺省」的口径分叉
    for _field in ("code", "codes", "fields", "limit", "snapshot_id"):
        if _field in args and args[_field] is None:
            raise V3Error("INVALID_ARGUMENT",
                          f"{_field} 不允许显式传 null（缺省即省略该字段）",
                          details={"argument": _field, "received": None})
    has_code = args.get("code") is not None
    has_codes = args.get("codes") is not None
    if has_code and has_codes:
        raise V3Error("INVALID_ARGUMENT", "code 与 codes 不能同时提供")
    if not has_code and not has_codes:
        raise V3Error("INVALID_ARGUMENT", "code 与 codes 必须提供其一")

    codes_raw = [args["code"]] if has_code else args["codes"]
    if not isinstance(codes_raw, list) or not codes_raw:
        raise V3Error("INVALID_ARGUMENT", "codes 必须是非空数组",
                      details={"argument": "codes"})
    if len(codes_raw) > MAX_CODES:
        raise V3Error("INVALID_ARGUMENT",
                      f"codes 最多 {MAX_CODES} 个（收到 {len(codes_raw)}）",
                      details={"received": len(codes_raw)})
    seen: set[str] = set()
    parsed: list[tuple[str, str, str, str]] = []
    for item in codes_raw:
        key = item if isinstance(item, str) else repr(item)
        if key in seen:
            raise V3Error("INVALID_ARGUMENT", f"codes 存在重复项: {key}",
                          details={"duplicate": key})
        seen.add(key)
        parsed.append(parse_instrument_id(
            item, argument="code" if has_code else "codes"))
    codes = ["/".join(parts) for parts in parsed]

    frequency = args.get("frequency")
    if frequency is None:
        raise V3Error("INVALID_ARGUMENT", "frequency 必填")
    if frequency not in ("1d", "1m", "5m", "15m", "30m", "60m", "1w", "1M"):
        raise V3Error("INVALID_ARGUMENT",
                      f"不支持的 frequency 值: {frequency!r}",
                      details={"argument": "frequency"})
    if frequency != "1d":
        raise V3Error("UNSUPPORTED_FREQUENCY",
                      f"周期 {frequency} 未实现（core_daily_v1 首批仅 1d；"
                      "分钟/周/月按合同逐项独立验收后开放，不能换周期替代）",
                      details={"requested": frequency})

    start = _date8(args.get("start"), "start")
    end = _date8(args.get("end"), "end")
    if start > end:
        raise V3Error("INVALID_ARGUMENT", "start 不能大于 end",
                      details={"start": start, "end": end})

    fq = args.get("fq")
    if fq is None:
        raise V3Error("INVALID_ARGUMENT", "fq 必填（禁止缺省后按 SDK 默认前复权）")
    if fq not in ("none", "qfq", "hfq"):
        raise V3Error("INVALID_ARGUMENT", f"fq 取值非法: {fq!r}")
    if fq != "none":
        # 0.14.0（B 批）：adjusted_history_v1 启用，qfq/hfq 走完整 R/F/A 链。
        if research is None:
            raise V3Error("UNSUPPORTED_ADJUSTMENT",
                          f"复权 {fq} 分支未加载（research 模块缺失）",
                          details={"requested": fq})
        anchor = args.get("adjustment_anchor")
        factor_id = args.get("factor_snapshot_id")
        if anchor is None or factor_id is None:
            raise V3Error(
                "INVALID_ARGUMENT",
                f"fq={fq} 时 adjustment_anchor 与 factor_snapshot_id 均必填"
                "（先经 get_adjust_factors 建立因子快照 F，再以其读取复权行情）")
        anchor = _date8(anchor, "adjustment_anchor")
        if not isinstance(factor_id, str) or not factor_id:
            raise V3Error("INVALID_ARGUMENT",
                          "factor_snapshot_id 必须是非空字符串")
    else:
        if "adjustment_anchor" in args or "factor_snapshot_id" in args:
            raise V3Error("INVALID_ARGUMENT",
                          "fq=none 时不能携带 adjustment_anchor/factor_snapshot_id")

    fields_raw = args.get("fields")
    if fields_raw is None:
        fields = list(FIELDS_ORDER)
    else:
        if isinstance(fields_raw, str):
            raise V3Error("INVALID_ARGUMENT",
                          "v3 的 fields 必须是字符串数组（旧版逗号字符串不再接受）",
                          details={"argument": "fields",
                                   "received": fields_raw[:80]})
        if not isinstance(fields_raw, list) or not fields_raw:
            raise V3Error("INVALID_ARGUMENT", "fields 必须是非空字符串数组")
        for f in fields_raw:
            if f not in FIELDS_ORDER:
                raise V3Error(
                    "INVALID_ARGUMENT",
                    f"fields 含非法字段: {f!r}（仅 open/high/low/close/volume/amount）",
                    details={"argument": "fields", "received": fields_raw})
        if len(set(fields_raw)) != len(fields_raw):
            raise V3Error("INVALID_ARGUMENT", "fields 存在重复项")
        fields = [f for f in FIELDS_ORDER if f in set(fields_raw)]

    limit = args.get("limit", 0)
    if isinstance(limit, bool) or not isinstance(limit, int) or limit < 0:
        raise V3Error("INVALID_ARGUMENT",
                      f"limit 必须是非负整数（收到 {limit!r}）",
                      details={"argument": "limit"})

    snapshot_id = args.get("snapshot_id")
    if snapshot_id is not None and (not isinstance(snapshot_id, str)
                                    or not snapshot_id):
        raise V3Error("INVALID_ARGUMENT", "snapshot_id 必须是非空字符串")

    return {"codes": codes, "frequency": frequency, "start": start, "end": end,
            "fq": fq, "fields": fields, "limit": limit,
            "snapshot_id": snapshot_id,
            "adjustment_anchor": args.get("adjustment_anchor"),
            "factor_snapshot_id": args.get("factor_snapshot_id")}


def _expected_slots(exchange: str, start: str, end: str) -> list[str] | None:
    """请求区间内该交易所日历的预期交易日（ISO）；日历范围外返回 None。"""
    if not (CALENDAR_COVERAGE_START.replace("-", "") <= start
            and end <= CALENDAR_COVERAGE_END.replace("-", "")):
        return None
    try:
        days = _calendar().trading_days_between(start, end)
    except Exception:  # noqa: BLE001 - 日历模块异常按未知处理
        return None
    out = []
    for d in days:
        text = str(d)
        out.append(_iso(text) if len(text) == 8 and text.isdigit() else text)
    return out


def _bars_from_rows(rows: list[dict], identity: Identity,
                    bar_state: str) -> list[dict]:
    """引擎行 → 合同行对象（全六字段；十进制字符串/整数；完整性校验）。"""
    out: list[dict] = []
    for row in rows:
        iso_date = _iso(str(row.get("date"))[:8])
        bar = {"date": iso_date, "bar_state": bar_state,
               "source_id": BARS_SOURCE_ID}
        for field in FIELDS_ORDER:
            value = row.get(field)
            if value is None:
                raise EngineFailure(
                    "SOURCE_SCHEMA_ERROR",
                    f"{identity.code} {iso_date} 缺少必填字段 {field}"
                    "（核心六字段不得缺失，不能用 null/0 假造）")
            if field in ("open", "high", "low", "close"):
                try:
                    text = decimal_str(value)
                except ValueError as exc:
                    raise EngineFailure(
                        "SOURCE_SCHEMA_ERROR",
                        f"{identity.code} {iso_date} {field} 非数值: {value!r}"
                    ) from exc
                places = len(text.partition(".")[2])
                if places > _price_decimals(identity.asset_type):
                    raise EngineFailure(
                        "SOURCE_SCHEMA_ERROR",
                        f"{identity.code} {iso_date} {field} 小数位 {places} "
                        f"超过声明精度 {_price_decimals(identity.asset_type)}")
                bar[field] = text
            elif field == "volume":
                if isinstance(value, bool) or not isinstance(value, (int, float)):
                    raise EngineFailure(
                        "SOURCE_SCHEMA_ERROR",
                        f"{identity.code} {iso_date} volume 非数值: {value!r}")
                if float(value) != int(value):
                    raise EngineFailure(
                        "SOURCE_SCHEMA_ERROR",
                        f"{identity.code} {iso_date} volume 非整数: {value!r}")
                volume = int(value)
                if volume < 0 or volume > 2**53 - 1:
                    raise EngineFailure(
                        "SOURCE_SCHEMA_ERROR",
                        f"{identity.code} {iso_date} volume 越界: {volume}")
                bar[field] = volume
            else:  # amount
                if isinstance(value, bool) or not isinstance(value, (int, float)):
                    raise EngineFailure(
                        "SOURCE_SCHEMA_ERROR",
                        f"{identity.code} {iso_date} amount 非数值: {value!r}")
                if float(value) < 0:
                    raise EngineFailure(
                        "SOURCE_SCHEMA_ERROR",
                        f"{identity.code} {iso_date} amount 为负: {value!r}")
                text = decimal_str(value)
                # 0.14.0：长历史实测引擎存在带小数的成交额（如 002600 2026-06-23）。
                # 合同 amount=非负十进制字符串（CNY 元），小数合法；声明精度 2 位，
                # 超过 2 位视为源形状异常（不允许更高精度噪声）。
                places = len(text.partition(".")[2])
                if places > 2:
                    raise EngineFailure(
                        "SOURCE_SCHEMA_ERROR",
                        f"{identity.code} {iso_date} amount 小数位 {places} "
                        "超过声明精度 2（引擎口径异常）")
                bar[field] = text
        # 未复权股票/ETF OHLC 必须 > 0 且 low <= open/close <= high（README §6.1）
        from decimal import Decimal as _D
        op, hi, lo, cl = (_D(bar[f]) for f in ("open", "high", "low", "close"))
        if lo <= 0 or op <= 0 or cl <= 0 or hi <= 0:
            raise EngineFailure(
                "SOURCE_SCHEMA_ERROR",
                f"{identity.code} {iso_date} 未复权核心价格必须为正"
                f"（open={bar['open']} high={bar['high']} low={bar['low']}"
                f" close={bar['close']}）")
        if not (lo <= min(op, cl) and max(op, cl) <= hi):
            raise EngineFailure(
                "SOURCE_SCHEMA_ERROR",
                f"{identity.code} {iso_date} OHLC 次序非法"
                f"（open={bar['open']} high={bar['high']} low={bar['low']}"
                f" close={bar['close']}）")
        out.append(bar)
    return out


def _units_block(identity: Identity) -> dict:
    return {"price": "CNY", "volume": identity.volume_unit, "amount": "CNY",
            "price_decimals": _price_decimals(identity.asset_type),
            "amount_decimals": 2, "null_reason": None}


def _error_meta(identity: Identity, *, expected: int | None) -> dict:
    """错误证券的 meta（coverage 未知、status=error；README §9）。"""
    is_research_series = identity.asset_type in ("index", "board")
    return {
        "instrument": {
            "instrument_id": identity.instrument_id, "code": identity.code,
            "exchange": identity.exchange, "asset_type": identity.asset_type,
            "namespace": identity.namespace, "name": None,
            "identity_verified": identity.verified,
            "identity_source_id": (research.series_source_id(identity.instrument_id)
                                   if is_research_series and identity.verified
                                   else (IDENTITY_SOURCE_ID
                                         if identity.verified else None)),
        },
        "status": "error",
        "units": (research.series_units(identity.asset_type)
                  if is_research_series else _units_block(identity)),
        "adjustment": {"mode": "none", "anchor_date": None,
                       "factor_snapshot_id": None, "algorithm": None},
        "coverage": {"state": "unknown", "expected_rows": expected,
                     "available_rows": None, "returned_rows": 0, "gaps": []},
        "truncated": False, "truncation_reason": None,
    }


def get_kline_v3(args: dict) -> dict:
    """get_kline 的 v3 实现：完整/部分/失败统一 KlineResponse 信封。"""
    try:
        query = _validate_kline_request(args)
    except V3Error as exc:
        return tool_failure([exc])

    request_hash = digest(query)
    codes: list[str] = query["codes"]

    # -- B 批（0.14.0）：fq=qfq/hfq 走 R/F/A 链（含 A 快照重读分支）--
    if query["fq"] != "none":
        return research.adjusted_kline(query, request_hash)

    # -- 快照重读（不触上游、不占行情槽；范围/证券/周期/复权不一致明确失败）--
    if query["snapshot_id"] is not None:
        return _kline_from_snapshot(query, request_hash)

    # -- 身份核验（目录不可用 → 逐码 SOURCE_UNAVAILABLE）--
    catalog, catalog_retrieved = catalog_codes()
    identities: dict[str, Identity] = {}
    fetch_errors: dict[str, V3Error] = {}
    for key in codes:
        try:
            identities[key] = resolve_identity(key, catalog=catalog)
        except V3Error as exc:
            fetch_errors[key] = exc

    # -- 响应规模预检：预期槽位（含 gap 对象）合计超限 → RESULT_TOO_LARGE --
    expected_by_code: dict[str, list[str] | None] = {}
    total_slots = 0
    slots_known = True
    for key in codes:
        if key not in identities:
            expected_by_code[key] = None
            continue
        expected = _expected_slots(identities[key].exchange,
                                   query["start"], query["end"])
        expected_by_code[key] = expected
        if expected is None:
            slots_known = False
        else:
            total_slots += len(expected)
    if slots_known and total_slots > MAX_RESPONSE_ROWS:
        return _kline_result_too_large(query, request_hash, total_slots)

    # -- 行情读取（请求槽：并发满载 → 有界 BUSY）--
    acquired = _kline_slots.acquire(timeout=_KLINE_SLOT_WAIT_SECONDS)
    if not acquired:
        return tool_failure([V3Error(
            "BUSY",
            f"行情并发已满（{MAX_CONCURRENCY}），请按 retry_after_ms 等待后重试",
            details={"retry_after_ms": int(_KLINE_SLOT_WAIT_SECONDS * 1000)})])
    try:
        return _kline_fetch_and_build(query, request_hash, codes, identities,
                                      fetch_errors, expected_by_code,
                                      catalog_retrieved)
    finally:
        _kline_slots.release()


def _kline_fetch_and_build(query, request_hash, codes, identities, fetch_errors,
                           expected_by_code, catalog_retrieved) -> dict:
    deadline = time.monotonic() + REQUEST_TIMEOUT_MS / 1000.0
    per_call_timeout = min(15.0, REQUEST_TIMEOUT_MS / 1000.0)
    rows_by_code: dict[str, list[dict]] = {}
    kinds: dict[str, str] = {}
    for key in codes:
        if key in fetch_errors or key not in identities:
            continue
        remaining = deadline - time.monotonic()
        if remaining <= 0.5:
            fetch_errors[key] = V3Error(
                "SOURCE_TIMEOUT",
                "单请求期限（30s）耗尽，该证券未读取",
                instrument_id=key,
                details={"deadline_ms": REQUEST_TIMEOUT_MS})
            continue
        identity = identities[key]
        if identity.asset_type in ("index", "board"):
            # 0.14.0 A 批：指数（腾讯 ifzq 三主机→新浪 轮转）/板块（eastmoney，
            # 0.16.0 起持久缓存+风控退避+缺口补采）
            kinds[key] = (identity.asset_type, None, {})  # 失败序列也进目录
            try:
                _kind, provider, rows, fetch_meta = research.fetch_series_rows(
                    key, query["start"], query["end"])
                kinds[key] = (_kind, provider, fetch_meta)
                rows_by_code[key] = rows
            except (research.ResearchError, V3Error) as exc:
                code = getattr(exc, "code", "SOURCE_UNAVAILABLE")
                fetch_errors[key] = V3Error(code, exc.message, instrument_id=key)
            except Exception as exc:  # noqa: BLE001 - 上游异常统一分类
                fetch_errors[key] = V3Error("SOURCE_UNAVAILABLE",
                                            f"{type(exc).__name__}: {exc}"[:200],
                                            instrument_id=key)
            continue
        try:
            rows_by_code[key] = engine_daily_rows(
                identities[key].code, query["start"], query["end"],
                timeout=min(per_call_timeout, remaining))
        except EngineFailure as exc:
            rows_by_code.pop(key, None)
            if exc.code == "BUSY":
                fetch_errors[key] = V3Error(
                    "BUSY", f"引擎访问闸门已满: {exc.message}",
                    instrument_id=key,
                    details={"retry_after_ms": int(_KLINE_SLOT_WAIT_SECONDS * 1000)})
            else:
                fetch_errors[key] = V3Error(exc.code, exc.message,
                                            instrument_id=key)

    # -- 实际行数超限（预检只覆盖已知日历范围）--
    total_rows = sum(len(r) for r in rows_by_code.values())
    if total_rows > MAX_RESPONSE_ROWS:
        return _kline_result_too_large(query, request_hash, total_rows)

    health = HEALTH.get()
    latest = health.get("latest")
    if latest is None:
        latest = engine_latest_trade_date()

    # -- bar_state 冻结于捕获时刻（快照重读不再重算）--
    bar_states: dict[str, str] = {}
    for key, rows in rows_by_code.items():
        if rows:
            max_date = max((str(r.get("date8") or r.get("date"))[:8])
                           for r in rows)
            bar_states[key] = _bar_state_for(max_date, latest)

    sources = {
        IDENTITY_SOURCE_ID: _identity_source(catalog_retrieved),
        SEGMENT_SOURCE_ID: _segment_source(catalog_retrieved),
        BARS_SOURCE_ID: _source_record(
            retrieved_at=_now_iso(), reference=(
                "urn:stockdb:engine:日k:<code>:<date-prefix> (engine daily "
                "kline tables over HTTP, unadjusted)"),
            finality=_bars_finality_evidence()),
    }
    for key, (kind, provider, fetch_meta) in kinds.items():
        # 失败序列的来源也登记：错误 meta 的 identity_source_id 必须可解析。
        # 板块缓存服务时 retrieved_at 取窗口内最旧真实取得时间（不伪标实时抓取）
        sources[research.series_source_id(key)] = (
            research.series_source_record(
                kind, key, (fetch_meta or {}).get("retrieved_at")
                or _now_iso(),
                provider=provider, fetch_meta=fetch_meta or {}))
    snapshot_id = SNAPSHOTS.new_id("kline")
    # E01（消费方反馈）：所有 v3 行情响应（含 R/长历史/普通 core）都携带价格
    # 序列研究元数据——扩展校验器对一切带 query 的 KlineResponse 强制
    # research_metadata；R 与 A 必须共享同一价格 series 描述（复权语义由
    # meta.adjustment 表达，不进 series_catalog）。
    catalog_desc: dict[str, dict | None] = {}
    cal_ver = _calendar().XSHG_HOLIDAYS_THROUGH
    for key in codes:
        entry = kinds.get(key)
        kind = entry[0] if entry else None
        provider = entry[1] if entry else None
        if kind and rows_by_code.get(key):
            catalog_desc[key] = research.series_descriptor(
                key, kind=kind, rows=rows_by_code[key],
                calendar_id=CALENDAR_ID, calendar_version=cal_ver,
                provider_used=provider)
        elif key in identities and identities[key].asset_type not in (
                "index", "board") and rows_by_code.get(key):
            catalog_desc[key] = research.series_descriptor(
                key, kind="equity",
                rows=[{"date8": str(r.get("date"))[:8]}
                      for r in rows_by_code[key]],
                calendar_id=CALENDAR_ID, calendar_version=cal_ver)
        else:
            catalog_desc[key] = None
    SNAPSHOTS.capture(snapshot_id, {
        "kind": "kline", "query": dict(query),
        "rows": {k: list(v) for k, v in rows_by_code.items()},
        "sources": {k: dict(v) for k, v in sources.items()},
        "identities": {k: _identity_dict(v) for k, v in identities.items()},
        "bar_states": dict(bar_states), "latest": latest,
        "catalog": catalog_desc,
    }, row_count=total_rows)

    envelope = _kline_build_response(query, request_hash, codes, identities,
                                     fetch_errors, expected_by_code, sources,
                                     snapshot_id, rows_by_code, bar_states)
    if research is not None:
        envelope.update(research.research_extensions(None, catalog_desc))
    return envelope


def _kline_build_response(query, request_hash, codes, identities, fetch_errors,
                          expected_by_code, sources, snapshot_id, rows_by_code,
                          bar_states, *, prebuilt_bars: bool = False) -> dict:
    fields: list[str] = query["fields"]
    limit: int = query["limit"]
    data: dict[str, list[dict]] = {}
    meta: dict[str, dict] = {}
    errors: list[dict] = []
    truncated_any = False
    snap = SNAPSHOTS.get(snapshot_id)
    expires_at = (datetime.fromtimestamp(snap["expires_at"]).astimezone()
                  .isoformat(timespec="seconds")) if snap else None

    for key in codes:
        identity = identities.get(key)
        error = fetch_errors.get(key)
        rows = rows_by_code.get(key, [])
        if identity is None:
            # 身份解析失败（fetch_errors 必有该项）：保留键 + error meta
            if error is not None:
                errors.append(error.to_error_object())
            expected_list = expected_by_code.get(key)
            meta[key] = _error_meta(
                identity_from_key(key),
                expected=len(expected_list) if expected_list is not None else None)
            data[key] = []
            continue
        expected_list = expected_by_code.get(key)
        expected = len(expected_list) if expected_list is not None else None

        if error is not None:
            errors.append(error.to_error_object())
            meta[key] = _error_meta(identity, expected=expected)
            data[key] = []
            continue

        try:
            if prebuilt_bars:
                bars = list(rows)  # 已是合同 Bar（复权 A / 注入路径），不再转换
            elif identity.asset_type in ("index", "board"):
                bars = research.research_series_bars(
                    rows, identity.asset_type, key,
                    bar_states.get(key, "unknown"),
                    research.series_source_id(key))
            else:
                bars = _bars_from_rows(rows, identity,
                                       bar_states.get(key, "unknown"))
        except EngineFailure as exc:
            errors.append(V3Error(exc.code, exc.message,
                                  instrument_id=key).to_error_object())
            meta[key] = _error_meta(identity, expected=expected)
            data[key] = []
            continue

        available = len(bars)
        row_dates = {b["date"] for b in bars}
        gaps: list[dict] = []
        if expected_list is not None:
            gaps = [{"date": d, "time": None, "reason": "UNKNOWN",
                     "evidence": []}
                    for d in expected_list if d not in row_dates]

        served = bars
        truncated = False
        truncation_reason = None
        if limit > 0 and len(bars) > limit:
            served = bars[-limit:]
            truncated = True
            truncation_reason = "caller_limit"
        truncated_any = truncated_any or truncated

        projected = [{"date": b["date"], "bar_state": b["bar_state"],
                      "source_id": b["source_id"],
                      **{f: b[f] for f in fields}} for b in served]

        if expected is None:
            coverage_state = "unknown"
        elif not gaps and not truncated:
            coverage_state = "complete"
        else:
            coverage_state = "incomplete"

        # 非完整覆盖必须有机器错误（README §7.1；截断除外——truncated 已声明）
        if coverage_state != "complete" and not truncated:
            if any(e.get("instrument_id") in (None, key) for e in errors):
                pass  # 已有该证券错误（如读取失败）
            elif expected is None:
                errors.append(V3Error(
                    "INCOMPLETE_COVERAGE",
                    f"{identity.instrument_id} 请求区间超出已核实交易日历范围"
                    f"（{CALENDAR_COVERAGE_START}..{CALENDAR_COVERAGE_END}），"
                    "预期槽位未知，不能判定完整覆盖",
                    instrument_id=key,
                    details={"reason": "calendar_coverage_unknown",
                             "calendar_coverage_start": CALENDAR_COVERAGE_START,
                             "calendar_coverage_end": CALENDAR_COVERAGE_END}
                ).to_error_object())
            else:
                errors.append(V3Error(
                    "INCOMPLETE_COVERAGE",
                    f"{identity.instrument_id} 范围内有 {len(gaps)} 个未解释缺口"
                    "（无上市/停牌/退市公开证据，不能归为合法空）",
                    instrument_id=key,
                    details={"dates": [g["date"] for g in gaps][:50],
                             "reason": "unknown_gap_no_evidence"}
                ).to_error_object())

        is_research_series = identity.asset_type in ("index", "board")
        if is_research_series:
            name = (research.INDEX_SERIES.get(key, {}).get("name")
                    or research.BOARD_SERIES.get(key, {}).get("name"))
        else:
            name = rows[-1].get("name") if rows else None
        if coverage_state == "complete":
            instrument_status = "ok" if bars else "empty"
        else:
            instrument_status = "partial" if bars else "error"
        meta[key] = {
            "instrument": {
                "instrument_id": identity.instrument_id,
                "code": identity.code, "exchange": identity.exchange,
                "asset_type": identity.asset_type,
                "namespace": identity.namespace, "name": name,
                "identity_verified": True,
                "identity_source_id": (research.series_source_id(key)
                                       if is_research_series
                                       else IDENTITY_SOURCE_ID),
            },
            "status": instrument_status,
            "units": (research.series_units(identity.asset_type)
                      if is_research_series else _units_block(identity)),
            "adjustment": {"mode": "none", "anchor_date": None,
                           "factor_snapshot_id": None, "algorithm": None},
            "coverage": {"state": coverage_state, "expected_rows": expected,
                         "available_rows": available,
                         "returned_rows": len(projected), "gaps": gaps},
            "truncated": truncated, "truncation_reason": truncation_reason,
        }
        data[key] = projected

    # 顶层状态（README §7.3）
    all_complete = bool(meta) and all(
        m["coverage"]["state"] == "complete" for m in meta.values())
    usable = (any(bool(rows) for rows in data.values())
              or any(m["coverage"]["state"] == "complete"
                     and m["status"] == "empty" for m in meta.values()))
    if all_complete and not errors and not truncated_any:
        status = "complete"
    elif usable:
        status = "partial"
    else:
        status = "failed"

    header = _base_header(status, errors=errors, snapshot_id=snapshot_id,
                          snapshot_expires_at=expires_at,
                          data_hash=digest(data), truncated=truncated_any)
    header.update({"query": query, "request_hash": request_hash,
                   "data": data, "meta": meta, "source_catalog": sources})
    return header


def _kline_result_too_large(query, request_hash, size) -> dict:
    data = {key: [] for key in query["codes"]}
    meta = {}
    for key in query["codes"]:
        meta[key] = _error_meta(identity_from_key(key), expected=None)
    header = _base_header(
        "failed",
        errors=[V3Error(
            "RESULT_TOO_LARGE",
            f"请求规模 {size} 超出单响应行数上限 {MAX_RESPONSE_ROWS}"
            "（含预期槽位/gap 对象）；请缩小证券集合或日期范围"
            "（或用同一 snapshot_id 拆分查询）",
            details={"size": size, "max_response_rows": MAX_RESPONSE_ROWS}
        ).to_error_object()],
        snapshot_id=None, snapshot_expires_at=None, data_hash=digest(data))
    header.update({"query": query, "request_hash": request_hash,
                   "data": data, "meta": meta, "source_catalog": {}})
    return header


def _snapshot_or_error(snapshot_id: str) -> tuple[dict | None, V3Error | None]:
    """快照读取：不存在→SNAPSHOT_NOT_FOUND；过期→SNAPSHOT_EXPIRED（不重抓实时值）。"""
    item = SNAPSHOTS.get(snapshot_id)
    if item is None:
        return None, V3Error(
            "SNAPSHOT_NOT_FOUND",
            f"快照 {snapshot_id} 不存在（服务重启也会丢失快照，不悄悄重抓实时值）",
            details={"snapshot_id": snapshot_id})
    if SNAPSHOTS.is_expired(item):
        return None, V3Error(
            "SNAPSHOT_EXPIRED",
            f"快照 {snapshot_id} 已过保留期（{SNAPSHOT_RETENTION_SECONDS}s），"
            "不得用实时值顶替旧快照；请发起新读取",
            details={"snapshot_id": snapshot_id})
    return item, None


def _kline_from_snapshot(query, request_hash) -> dict:
    """快照重读：只用已捕获内容（含冻结 bar_state/来源）；范围外明确失败。"""
    snap, snap_error = _snapshot_or_error(query["snapshot_id"])
    if snap is None:
        return tool_failure([snap_error])
    payload = snap["payload"]
    if payload.get("kind") not in ("kline", "kline-adjusted"):
        return tool_failure([V3Error(
            "SNAPSHOT_SCOPE_MISMATCH",
            f"快照 {query['snapshot_id']} 不是行情快照"
            f"（kind={payload.get('kind')!r}；因子快照 F 不能作为行情 snapshot 重读）",
            details={"snapshot_id": query["snapshot_id"],
                     "kind": payload.get("kind")})])
    captured_query = payload["query"]
    scope_errors: list[V3Error] = []
    for key in query["codes"]:
        if key not in captured_query["codes"]:
            scope_errors.append(V3Error(
                "SNAPSHOT_SCOPE_MISMATCH",
                f"{key} 不在快照已捕获证券范围内（不能把新证券拼进旧快照）",
                instrument_id=key,
                details={"snapshot_id": query["snapshot_id"]}))
    if (query["start"] < captured_query["start"]
            or query["end"] > captured_query["end"]):
        scope_errors.append(V3Error(
            "SNAPSHOT_SCOPE_MISMATCH",
            f"请求区间 [{query['start']},{query['end']}] 超出快照捕获范围 "
            f"[{captured_query['start']},{captured_query['end']}]",
            details={"snapshot_id": query["snapshot_id"],
                     "captured_start": captured_query["start"],
                     "captured_end": captured_query["end"]}))
    if (query["frequency"] != captured_query["frequency"]
            or query["fq"] != captured_query["fq"]):
        message = "快照重读必须使用相同周期/复权"
        if payload.get("kind") == "kline-adjusted":
            message = ("复权 A 快照须携带与捕获相同的 fq/adjustment_anchor/"
                       "factor_snapshot_id 重放（收到 fq="
                       f"{query['fq']!r}，快照捕获 fq="
                       f"{captured_query['fq']!r}）")
        scope_errors.append(V3Error(
            "SNAPSHOT_SCOPE_MISMATCH", message,
            details={"snapshot_id": query["snapshot_id"],
                     "captured_fq": captured_query["fq"],
                     "requested_fq": query["fq"]}))
    if scope_errors:
        return tool_failure(scope_errors)

    start_n, end_n = int(query["start"]), int(query["end"])
    rows_by_code: dict[str, list[dict]] = {}
    for key in query["codes"]:
        rows_by_code[key] = [
            r for r in payload["rows"].get(key, [])
            if start_n <= _row_date_key(r) <= end_n]
    identities = {key: identity_from_key(
        key, verified=payload["identities"][key]["verified"],
        volume_unit=payload["identities"][key]["volume_unit"])
        for key in query["codes"]}
    expected_by_code = {key: _expected_slots(identities[key].exchange,
                                             query["start"], query["end"])
                        for key in query["codes"]}
    envelope = _kline_build_response(
        query, request_hash, query["codes"], identities, {},
        expected_by_code, payload["sources"], query["snapshot_id"],
        rows_by_code, payload["bar_states"],
        prebuilt_bars=payload.get("kind") == "kline-adjusted")
    # E01：捕获时已带价格序列目录的快照（R/长历史/普通 core），重读与子范围
    # 读取同样返回 research 元数据（原始捕获不变）。
    if research is not None and payload.get("catalog"):
        envelope.update(research.research_extensions(
            None, payload["catalog"]))
    return envelope


# ---------------------------------------------------------------------------
# get_stock_list v3（README §8.1）
# ---------------------------------------------------------------------------


def _validate_list_args(args: dict) -> tuple[list[str], list[str], str]:
    allowed = {"contract_version", "asset_types", "exchanges", "as_of",
               "snapshot_id"}
    unknown = set(args) - allowed
    if unknown:
        raise V3Error("INVALID_ARGUMENT", f"未知参数: {sorted(unknown)}",
                      details={"unknown_parameters": sorted(unknown)})
    if args.get("snapshot_id") is None and "snapshot_id" in args:
        raise V3Error("INVALID_ARGUMENT", "snapshot_id 不允许显式传 null")
    asset_types = args.get("asset_types")
    exchanges = args.get("exchanges")
    if (not isinstance(asset_types, list) or not asset_types
            or len(set(asset_types)) != len(asset_types)
            or any(a not in ("equity", "etf", "index", "board")
                   for a in asset_types)):
        raise V3Error("INVALID_ARGUMENT",
                      "asset_types 必须是非空唯一的 equity/etf/index/board 数组",
                      details={"argument": "asset_types"})
    if (not isinstance(exchanges, list) or not exchanges
            or len(set(exchanges)) != len(exchanges)
            or any(e not in ("XSHG", "XSHE", "XBSE", "CN") for e in exchanges)):
        raise V3Error("INVALID_ARGUMENT",
                      "exchanges 必须是非空唯一的 XSHG/XSHE/XBSE/CN 数组",
                      details={"argument": "exchanges"})
    as_of = _date8(args.get("as_of"), "as_of")
    return asset_types, exchanges, as_of


def _failed_list(scope: dict, code: str, message: str,
                 **details) -> dict:
    header = _base_header("failed",
                          errors=[V3Error(code, message,
                                          details=details or None).to_error_object()],
                          snapshot_id=None, snapshot_expires_at=None,
                          data_hash=digest([]))
    header.update({"scope": scope, "as_of_type": None, "data": [],
                   "total": 0, "universe_complete": False})
    return header


def get_stock_list_v3(args: dict) -> dict:
    try:
        asset_types, exchanges, as_of = _validate_list_args(args)
    except V3Error as exc:
        return tool_failure([exc])
    scope = {"asset_types": asset_types, "exchanges": exchanges, "as_of": as_of}

    # 快照重读
    if args.get("snapshot_id") is not None:
        snap, snap_error = _snapshot_or_error(args["snapshot_id"])
        if snap is None:
            return _failed_list(scope, snap_error.code, snap_error.message,
                                snapshot_id=args["snapshot_id"])
        payload = snap["payload"]
        if payload.get("kind") != "list" or payload["scope"] != scope:
            return _failed_list(
                scope, "SNAPSHOT_SCOPE_MISMATCH",
                "快照范围与请求 scope 不一致（清单快照只能原 scope 重读）",
                snapshot_id=args["snapshot_id"])
        data = payload["data"]
        header = _base_header(
            "complete", errors=[],
            snapshot_id=args["snapshot_id"],
            snapshot_expires_at=(datetime.fromtimestamp(snap["expires_at"])
                                 .astimezone().isoformat(timespec="seconds")),
            data_hash=digest(data))
        header.update({"scope": scope, "as_of_type": "observed_current",
                       "data": data, "total": len(data),
                       "universe_complete": True,
                       "source_catalog": payload["sources"]})
        return header

    unsupported_assets = [a for a in asset_types if a not in ("equity", "etf")]
    unsupported_exchanges = [e for e in exchanges if e not in ("XSHG", "XSHE")]
    if unsupported_assets or unsupported_exchanges:
        return _failed_list(
            scope, "UNSUPPORTED_ASSET",
            "清单能力仅覆盖 XSHG/XSHE × equity/etf（当前观察清单）；"
            f"请求包含未支持项 assets={unsupported_assets} "
            f"exchanges={unsupported_exchanges}",
            unsupported_assets=unsupported_assets,
            unsupported_exchanges=unsupported_exchanges)

    # as_of 必须是本次当前清单的观察日期（README §8.1：observed_current 语义）
    health = HEALTH.get()
    latest = health.get("latest")
    if latest is None:
        latest = engine_latest_trade_date()
    if latest is None or as_of != latest:
        return _failed_list(
            scope, "UNSUPPORTED_AS_OF",
            f"as_of={as_of} 不是本清单的观察日期（引擎最新交易日={latest}）；"
            "历史清单需 historical_identity_v1 通过独立验收后提供"
            "（不能把当前清单贴上过去的 as_of）",
            requested=as_of, observed=latest)

    catalog, retrieved_at = catalog_codes()
    if catalog is None:
        return _failed_list(
            scope, "SOURCE_UNAVAILABLE",
            "身份目录（引擎股票代码表）暂不可用",
            reason="identity_catalog_unavailable")

    data = []
    for code in sorted(catalog):
        segment = _segment_of(code)
        if segment is None:
            continue
        seg_exchange, seg_asset = segment
        if seg_exchange in exchanges and seg_asset in asset_types:
            data.append({
                "instrument_id": f"{seg_exchange}/{seg_asset}/exchange/{code}",
                "code": code, "exchange": seg_exchange,
                "asset_type": seg_asset, "namespace": "exchange",
                "name": None, "identity_verified": True,
                "identity_source_id": IDENTITY_SOURCE_ID,
            })
    sources = {IDENTITY_SOURCE_ID: _identity_source(retrieved_at),
               SEGMENT_SOURCE_ID: _segment_source(retrieved_at)}
    snapshot_id = SNAPSHOTS.new_id("list")
    SNAPSHOTS.capture(snapshot_id, {"kind": "list", "scope": scope,
                                    "data": data, "sources": sources,
                                    "as_of": as_of}, row_count=len(data))
    snap = SNAPSHOTS.get(snapshot_id)
    header = _base_header(
        "complete", errors=[], snapshot_id=snapshot_id,
        snapshot_expires_at=(datetime.fromtimestamp(snap["expires_at"])
                             .astimezone().isoformat(timespec="seconds")),
        data_hash=digest(data))
    header.update({"scope": scope, "as_of_type": "observed_current",
                   "data": data, "total": len(data), "universe_complete": True,
                   "source_catalog": sources})
    return header


# ---------------------------------------------------------------------------
# get_trading_days v3（README §8.2）
# ---------------------------------------------------------------------------


def get_trading_days_v3(args: dict) -> dict:
    if not isinstance(args, dict):
        return tool_failure([V3Error("INVALID_ARGUMENT", "arguments 必须是对象")])
    allowed = {"contract_version", "exchange", "start", "end", "snapshot_id"}
    unknown = set(args) - allowed
    if unknown:
        return tool_failure([V3Error("INVALID_ARGUMENT",
                                     f"未知参数: {sorted(unknown)}",
                                     details={"unknown_parameters": sorted(unknown)})])
    if args.get("snapshot_id") is None and "snapshot_id" in args:
        return tool_failure([V3Error("INVALID_ARGUMENT",
                                     "snapshot_id 不允许显式传 null")])
    exchange = args.get("exchange")
    if exchange not in ("XSHG", "XSHE", "XBSE"):
        return tool_failure([V3Error(
            "INVALID_ARGUMENT", "exchange 必须是 XSHG/XSHE/XBSE",
            details={"argument": "exchange", "received": repr(exchange)[:40]})])
    try:
        start = _date8(args.get("start"), "start")
        end = _date8(args.get("end"), "end")
    except V3Error as exc:
        return tool_failure([exc])
    if start > end:
        return tool_failure([V3Error("INVALID_ARGUMENT", "start 不能大于 end",
                                     details={"start": start, "end": end})])

    def _failed_cal(code: str, message: str, **details) -> dict:
        header = _base_header(
            "failed",
            errors=[V3Error(code, message,
                            details=details or None).to_error_object()],
            snapshot_id=None, snapshot_expires_at=None, data_hash=digest([]))
        header.update({"exchange": exchange, "start": start, "end": end,
                       "trading_days": [], "calendar_id": None,
                       "calendar_version": None, "coverage_start": None,
                       "coverage_end": None})
        return header

    # 快照重读
    if args.get("snapshot_id") is not None:
        snap, snap_error = _snapshot_or_error(args["snapshot_id"])
        if snap is None:
            return _failed_cal(snap_error.code, snap_error.message,
                               snapshot_id=args["snapshot_id"])
        payload = snap["payload"]
        if (payload.get("kind") != "calendar"
                or payload["exchange"] != exchange
                or payload["start"] != start or payload["end"] != end):
            return _failed_cal(
                "SNAPSHOT_SCOPE_MISMATCH",
                "日历快照只能按相同 exchange/start/end 重读",
                snapshot_id=args["snapshot_id"])
        trading_days = payload["days"]
        header = _base_header(
            "complete", errors=[], snapshot_id=args["snapshot_id"],
            snapshot_expires_at=(datetime.fromtimestamp(snap["expires_at"])
                                 .astimezone().isoformat(timespec="seconds")),
            data_hash=digest(trading_days))
        header.update(_calendar_body(exchange, start, end, trading_days,
                                     snapshot_source=True))
        return header

    if (start < CALENDAR_COVERAGE_START.replace("-", "")
            or end > CALENDAR_COVERAGE_END.replace("-", "")):
        return _failed_cal(
            "OUT_OF_CALENDAR",
            f"请求区间 [{start},{end}] 超出已核实日历边界 "
            f"[{CALENDAR_COVERAGE_START},{CALENDAR_COVERAGE_END}]；"
            "不按工作日推断交易日",
            coverage_start=CALENDAR_COVERAGE_START,
            coverage_end=CALENDAR_COVERAGE_END)

    try:
        days = _calendar().trading_days_between(start, end)
    except Exception as exc:  # noqa: BLE001
        return tool_failure([V3Error("INTERNAL_ERROR",
                                     f"日历计算异常: {exc}")])
    trading_days = [(_iso(d) if len(str(d)) == 8 and str(d).isdigit() else str(d))
                    for d in days]
    snapshot_id = SNAPSHOTS.new_id("cal")
    SNAPSHOTS.capture(snapshot_id, {"kind": "calendar", "exchange": exchange,
                                    "start": start, "end": end,
                                    "days": trading_days},
                      row_count=len(trading_days))
    snap = SNAPSHOTS.get(snapshot_id)
    header = _base_header(
        "complete", errors=[], snapshot_id=snapshot_id,
        snapshot_expires_at=(datetime.fromtimestamp(snap["expires_at"])
                             .astimezone().isoformat(timespec="seconds")),
        data_hash=digest(trading_days))
    header.update(_calendar_body(exchange, start, end, trading_days))
    return header


def _calendar_body(exchange: str, start: str, end: str,
                   trading_days: list[str], *, snapshot_source: bool = False) -> dict:
    body = {
        "exchange": exchange, "start": start, "end": end,
        "trading_days": trading_days,
        "calendar_id": CALENDAR_ID,
        "calendar_version": _calendar().XSHG_HOLIDAYS_THROUGH,
        "coverage_start": CALENDAR_COVERAGE_START,
        "coverage_end": CALENDAR_COVERAGE_END,
        "source_catalog": {
            "calendar-table": _source_record(
                retrieved_at=_now_iso(),
                reference=("core/calendar_market.py XSHG holiday table, "
                           "extracted from exchange_calendars 4.13.2 (XSHG), "
                           "verified year-by-year 2024-2026"),
                finality=("static embedded holiday table; immutability by "
                          "code review (module ships in image)")),
        },
        "extensions": {
            "note": ("XSHE/XBSE 均使用 XSHG 休市表（沪深北法定节假日安排一致"
                     "为本表依据；分所临时休市差异以交易所公告为准并修订本表）"),
        },
    }
    return body


# ---------------------------------------------------------------------------
# get_data_status v3（README §8.3）
# ---------------------------------------------------------------------------

_CORE_CAPS_EXCEPTIONS = [
    "identity: engine securities catalog (股票代码 table) membership plus "
    "official SSE/SZSE code-segment classification; per-instrument history "
    "starts at its own listing date within dataset coverage",
    "bar_state=final requires post-close sync-log evidence or a later "
    "trading session in the engine; otherwise unknown (never assumed final)",
    "LOF/closed-end/REIT segments (160-169/180/501/502/506/508) and Beijing "
    "exchange (920) are outside core_daily_v1 and refused explicitly",
    "history bounds are observed dataset bounds, not a per-instrument guarantee",
]


def _astock_lifecycle_status() -> dict:
    """astock 上游升级状态（只读缓存；UPGRADE-RELEASE §1 拟新增扩展名）。"""
    import json as _json
    from pathlib import Path as _Path
    out: dict = {"extension_contract": "stockdb-astock-provider-lifecycle",
                 "extension_revision": "1.0.0"}
    try:
        obs_path = (_Path(__file__).resolve().parents[2] / "astock"
                    / "observations.json")
        if obs_path.exists():
            history = _json.loads(obs_path.read_text(encoding="utf-8"))
            last_ok = next((o for o in reversed(history)
                            if o.get("status") != "query_failed"), None)
            out["last_discovery"] = history[-1] if history else None
            out["last_successful_observation"] = last_ok
        store = (_Path(__file__).resolve().parents[2] / "astock"
                 / "lifecycle-store.json")
        if store.exists():
            st = _json.loads(store.read_text(encoding="utf-8"))
            out["active_candidate"] = st.get("active")
            out["candidates"] = {k: v.get("state") for k, v in
                                 (st.get("candidates") or {}).items()}
    except Exception:  # noqa: BLE001 - 状态只读失败不影响核心
        pass
    return out


def get_data_status_v3(args: dict | None = None) -> dict:
    if args:
        extra = set(args) - {"contract_version"}
        if extra:
            return tool_failure([V3Error(
                "INVALID_ARGUMENT", f"未知参数: {sorted(extra)}",
                details={"unknown_parameters": sorted(extra)})])
    srv = _srv()
    health = HEALTH.get()
    latest = health.get("latest")
    caps = []
    for exchange in ("XSHG", "XSHE"):
        for asset in ("equity", "etf"):
            caps.append({
                "asset_type": asset, "exchange": exchange,
                "namespace": "exchange", "frequencies": ["1d"],
                "fq_modes": ["none"],
                "history_start": "2000-01-04" if asset == "equity" else "2005-02-23",
                "history_end": _iso(latest) if latest else None,
                "exceptions": _CORE_CAPS_EXCEPTIONS,
            })
    profiles = [
        {"id": "core_daily_v1", "supported": True, "reason": None,
         "capabilities": caps},
        {"id": "fullmarket_daily_v1", "supported": False,
         "reason": "全市场逐证券×交易日覆盖解释未独立验收；首批仅按请求证券读取",
         "capabilities": []},
        {"id": "index_board_daily_v1", "supported": True,
         "reason": None,
         "capabilities": (research.index_board_capabilities(latest)
                          if research is not None else [])},
        {"id": "minute_v1", "supported": False,
         "reason": "分钟K未接入 v3 合同（周期边界/竞价归属细则未冻结）",
         "capabilities": []},
        {"id": "adjusted_history_v1", "supported": True,
         "reason": None,
         "capabilities": (research.adjusted_history_capabilities(latest)
                          if research is not None else [])},
        {"id": "historical_identity_v1", "supported": False,
         "reason": "历史身份（上市/退市/别名有效期）无证据源，清单仅 observed_current",
         "capabilities": []},
    ]
    if research is not None:
        for profile in profiles:
            if not profile["supported"] and not profile["reason"]:
                profile["reason"] = "未实现"
    issues = []
    status = "ready"
    if latest is None:
        status = "degraded"
        issues.append({
            "profile_id": "core_daily_v1",
            "code": "SOURCE_UNAVAILABLE",
            "message": (f"引擎健康探测未就绪或失败: "
                        f"{health.get('error') or 'probing'}"
                        "（后台每 60s 重试；行情读取不受影响，按逐码错误返回）"),
            "retryable": True,
        })
    elif research is not None:
        # §8.3：可选能力当前不可用 → 如实 degraded 并逐项说明（只读缓存，不现查上游）
        em = research.em_board_availability()
        if em["fail_at"] > em["ok_at"]:
            status = "degraded"
            issues.append({
                "profile_id": "index_board_daily_v1",
                "code": "SOURCE_UNAVAILABLE",
                "message": ("eastmoney 板块源最近访问失败（BK 板块序列按合同"
                            "如实 SOURCE_UNAVAILABLE；指数序列不受影响）："
                            f"{em['fail_reason'] or 'unreachable'}"),
                "retryable": True,
            })
    return {
        "contract_version": CONTRACT_VERSION,
        "contract_revision": CONTRACT_REVISION,
        "protocol_versions": [srv.PROTOCOL_VERSION],
        "contract_versions": ["v2", CONTRACT_VERSION],
        "server": _server_identity(),
        "status": status,
        "checked_at": _now_iso(),
        "issues": issues,
        "profiles": profiles,
        "limits": {
            "max_codes": MAX_CODES,
            "max_concurrency": MAX_CONCURRENCY,
            "request_timeout_ms": REQUEST_TIMEOUT_MS,
            "max_response_rows": MAX_RESPONSE_ROWS,
            "snapshot_retention_seconds": SNAPSHOT_RETENTION_SECONDS,
        },
        "legacy": {"default_contract": "v2", "sunset_at": None},
        "data_status": {
            "latest_trade_date": _iso(latest) if latest else None,
            "scope": "summary_only",
            "refreshed_at": health.get("checked_at"),
        },
        "extensions": {
            "notice": "core_daily_v1 首批已声明能力当前就绪不代表扩展能力可用",
            "snapshot_store_size": SNAPSHOTS.size(),
            **({"research_v1": research.research_status_metadata()}
               if research is not None else {}),
            **({"astock_lifecycle_v1": _astock_lifecycle_status()}
               if research is not None else {}),
        },
    }


# ---------------------------------------------------------------------------
# 实际发布的 tools/list schema（0.13.1：修复 C01 发现 schema 缺陷）
#
# 消费方复核（FEEDBACK 2026-10-24 §4）实证 0.13.0 发布的 outputSchema 旧版
# 分支（仅 type:object）吞掉非法 v3 对象、inputSchema 未落实 required/互斥/
# 类型，C01 判 FAIL。修补原则：
#   - v3 分支 = 冻结合同对应 Request/Response 定义**原文**（程序化从
#     contract/contract.schema.json 抽取 $defs 传递闭包嵌入，不手工抄写，
#     不改合同一个字节）；
#   - 旧版分支仅匹配**未携带 contract_version** 的请求/输出
#     （not: required contract_version），不能再吞掉任何声明 v3 的对象；
#   - 嵌入自包含 $defs，消费方可独立解析（无需外部文件）。
# ---------------------------------------------------------------------------

CONTRACT_SCHEMA_PATH = Path(__file__).resolve().parent / "contract" / "contract.schema.json"
_DRAFT_SCHEMA = "https://json-schema.org/draft/2020-12/schema"


def _contract_defs_closure(root_name: str) -> dict:
    """从冻结合同抽取 root_name 定义及其传递依赖的全部 $defs（原样引用）。

    兼容 `#/$defs/X` 与 `#/$defs/X/properties/y` 形式的内部引用（后者同样
    只需 X 进入闭包即可在新文档内解析）。
    """
    schema = json.loads(CONTRACT_SCHEMA_PATH.read_text(encoding="utf-8"))
    defs = schema["$defs"]
    if root_name not in defs:
        raise KeyError(f"contract def not found: {root_name}")
    needed: set[str] = {root_name}

    def walk(node: Any) -> None:
        if isinstance(node, dict):
            ref = node.get("$ref")
            if isinstance(ref, str) and ref.startswith("#/$defs/"):
                name = ref.split("/")[2]
                if name not in needed:
                    needed.add(name)
                    walk(defs[name])
            for value in node.values():
                walk(value)
        elif isinstance(node, list):
            for value in node:
                walk(value)

    walk(defs[root_name])
    return {name: defs[name] for name in sorted(needed)}


# 各工具旧版输入 schema = 0.13.0 之前实际发布形态（逐字保留，仅加
# “未携带 contract_version”分支限定；旧客户端的校验行为不变）。
_LEGACY_ONLY_CV = {"not": {"required": ["contract_version"]}}

_LEGACY_KLINE_INPUT = {
    "type": "object",
    "description": "旧版分支：仅未携带 contract_version 的请求（行为与 0.12.x 一致）",
    **_LEGACY_ONLY_CV,
    "properties": {
        "code": {"type": "string",
                 "description": "6 位股票代码，如 600633（与 codes 二选一）"},
        "codes": {"type": "array", "items": {"type": "string"},
                  "description": "批量股票代码列表，最多 50 个（与 code 二选一；需 pybao SDK）"},
        "frequency": {"type": "string",
                      "enum": ["1m", "5m", "15m", "30m", "60m", "1d", "1w", "1M"],
                      "description": "K 线周期；1d=日K，5m/15m/30m/60m=分钟K，1m/1w/1M 需 pybao SDK",
                      "default": "1d"},
        "fq": {"type": "string", "enum": ["none", "qfq", "hfq"],
               "description": "复权方式；none=不复权(默认)，qfq=前复权，hfq=后复权（需 pybao SDK）",
               "default": "none"},
        "limit": {"type": "integer", "minimum": 0,
                  "description": "最多返回行数；0=不截断（默认）", "default": 0},
        "start": {"type": "string",
                  "description": "日K：8位起始日期(如 20260620)；分钟K：14位时间戳(如 20260625145200)"},
        "end": {"type": "string",
                "description": "日K 结束日期(可选；与 start 构成闭区间 [start, end]，含端点)"},
        "fields": {"type": "string", "description": "投影字段，逗号分隔（可选）"},
    },
    "required": [],
}

_LEGACY_LIST_INPUT = {
    "type": "object",
    "description": "旧版分支：仅未携带 contract_version 的请求（无参数，返回 {total, codes}）",
    **_LEGACY_ONLY_CV,
    "properties": {},
}

_LEGACY_CALENDAR_INPUT = {
    "type": "object",
    "description": "旧版分支：仅未携带 contract_version 的请求",
    **_LEGACY_ONLY_CV,
    "properties": {
        "start": {"type": "string", "description": "8位起始日期（含）"},
        "end": {"type": "string",
                "description": "8位结束日期（含）；缺省=start 后 90 个自然日"},
        "limit": {"type": "integer", "default": 60,
                  "description": "返回上限（硬上限400，超出截断）"},
    },
    "required": ["start"],
}

_LEGACY_STATUS_INPUT = {
    "type": "object",
    "description": "旧版分支：仅未携带 contract_version 的请求（无参数摘要）",
    **_LEGACY_ONLY_CV,
    "properties": {},
}

# 旧版输出 envelope（_apply_contract 的 8 键恒在契约）：仅未携带
# contract_version 的输出可进入本分支；v3 输出必须走完整合同 Response 校验。
_LEGACY_RESULT_DEF = {
    "type": "object",
    "description": (
        "旧版结果（未携带 contract_version 的请求；旧版 tools/call 不产生 "
        "structuredContent，本分支仅在校验旧形状时生效）：8 键 envelope + 工具字段"
    ),
    "not": {"required": ["contract_version"]},
    "required": ["source", "known_at", "is_partial", "truncated", "total",
                 "errors", "known_limitations"],
    "properties": {
        "source": {"type": "string"},
        "known_at": {"type": ["string", "null"]},
        "is_partial": {"type": "boolean"},
        "truncated": {"type": "boolean"},
        "total": {"type": ["integer", "object", "null"]},
        "errors": {"type": "array"},
        "known_limitations": {"type": "array"},
    },
}

_TOOL_REQUEST_DEFS = {
    "get_kline": ("KlineRequest", _LEGACY_KLINE_INPUT),
    "get_stock_list": ("StockListRequest", _LEGACY_LIST_INPUT),
    "get_trading_days": ("TradingDaysRequest", _LEGACY_CALENDAR_INPUT),
    "get_data_status": ("StatusRequest", _LEGACY_STATUS_INPUT),
}
_TOOL_RESPONSE_DEFS = {
    "get_kline": "KlineResponse",
    "get_stock_list": "StockListResponse",
    "get_trading_days": "TradingDaysResponse",
    "get_data_status": "StatusResponse",
}


def published_input_schema(tool: str) -> dict:
    """tools/list 发布的 inputSchema：anyOf[旧版分支（无 contract_version），
    v3 分支（合同 Request 定义原文 + 传递闭包 $defs）]。"""
    request_def, legacy = _TOOL_REQUEST_DEFS[tool]
    return {
        "$schema": _DRAFT_SCHEMA,
        # MCP 2025-06-18 Tool 定义要求 inputSchema 根节点显式 type=object
        #（内部 $ref 指向 object 不足以替代该必填字段——FEEDBACK 1024 §3）
        "type": "object",
        "anyOf": [
            {"$ref": f"#/$defs/{request_def}"},
            legacy,
        ],
        "$defs": _contract_defs_closure(request_def),
    }


def published_output_schema(tool: str) -> dict:
    """tools/list 发布的 outputSchema：anyOf[v3 合同 Response 原文（+非kline
    工具另加 ToolFailureResponse 联合分支）， 旧版 envelope（无
    contract_version）]，各分支均为传递闭包 $defs 的自包含定义。

    - 旧版分支带 not:required[contract_version]——任何声明 v3 的对象都只能
      走完整合同校验，宽泛分支不再吞掉非法 v3 输出（FEEDBACK 1024 §4.1）；
    - 状态/清单/日历的统一失败信封（ToolFailureResponse）是合同 §4.3 要求的
      合法输出，须被自身 schema 接受（FEEDBACK 1024 §2 的 3 例实测遗漏）；
    - 根节点显式 type=object（MCP 2025-06-18 Tool 定义，FEEDBACK 1024 §3）。
    """
    response_def = _TOOL_RESPONSE_DEFS[tool]
    defs = _contract_defs_closure(response_def)
    branches = [{"$ref": f"#/$defs/{response_def}"}]
    # 合同 README §4.3：状态/清单/日历的 v3 输出必须接受各自正常 Response 与
    # ToolFailureResponse 的联合；KlineResponse 自身已包含失败形状（对照项）。
    # FEEDBACK 1024 §2 实证缺失该分支时，服务自己返回的合法失败信封会被
    # 自己发布的 outputSchema 判非法（3 例实测）。
    if response_def != "KlineResponse":
        defs.update(_contract_defs_closure("ToolFailureResponse"))
        branches.append({"$ref": "#/$defs/ToolFailureResponse"})
    defs["LegacyToolResult"] = _LEGACY_RESULT_DEF
    branches.append({"$ref": "#/$defs/LegacyToolResult"})
    return {
        "$schema": _DRAFT_SCHEMA,
        # MCP 2025-06-18 Tool 定义要求 outputSchema 根节点显式 type=object
        "type": "object",
        "anyOf": branches,
        "$defs": defs,
    }


def published_tool_schemas() -> dict:
    """四工具 + 因子工具 {tool: {"inputSchema":…, "outputSchema":…}}（tools/list 装配用）。

    任一异常返回空 dict——TOOLS 装配退回旧版-only schema（v3 分发不受影响，
    缺陷只影响 schema 声明且会留 stderr 日志）。
    """
    try:
        schemas = {tool: {"inputSchema": published_input_schema(tool),
                          "outputSchema": published_output_schema(tool)}
                   for tool in _TOOL_REQUEST_DEFS}
        if research is not None:
            schemas["get_adjust_factors"] = research.published_factor_schemas()
        return schemas
    except Exception as exc:  # noqa: BLE001 - schema 装配失败降级并留痕
        print(f"market_v3: published_tool_schemas 生成失败（TOOLS 退回旧版 schema）: "
              f"{type(exc).__name__}: {exc}", file=sys.stderr)
        return {}


# ---------------------------------------------------------------------------
# 分发入口（由 stockdb_mcp_server._call_tool 调用）
# ---------------------------------------------------------------------------

_V3_TOOLS = frozenset({"get_kline", "get_stock_list", "get_trading_days",
                       "get_data_status", "get_adjust_factors"})


def maybe_dispatch(tool_name: str, args: Any) -> dict | None:
    """显式 v3 请求 → MCP result dict；非 v3 请求（无 contract_version）→ None。

    未知合同版本 → UNSUPPORTED_CONTRACT（details.requested_contract 记录被拒
    版本）；v3 合同未覆盖的工具同样 UNSUPPORTED_CONTRACT。
    """
    if not isinstance(args, dict) or "contract_version" not in args:
        return None
    requested = args.get("contract_version")
    if not isinstance(requested, str) or not requested:
        return _wrap(tool_failure([V3Error(
            "INVALID_ARGUMENT",
            "contract_version 必须是非空字符串（显式选择合同版本）",
            details={"argument": "contract_version",
                     "received": repr(requested)[:40]})]))
    if requested != CONTRACT_VERSION:
        return _wrap(tool_failure([V3Error(
            "UNSUPPORTED_CONTRACT",
            f"未实现的合同版本: {requested}（本服务支持: v2（缺省）, "
            f"{CONTRACT_VERSION}）",
            details={"requested_contract": requested,
                     "supported": ["v2", CONTRACT_VERSION]})]))
    if tool_name not in _V3_TOOLS:
        return _wrap(tool_failure([V3Error(
            "UNSUPPORTED_CONTRACT",
            f"工具 {tool_name} 不在 {CONTRACT_VERSION} 覆盖的工具集"
            "（get_kline/get_stock_list/get_trading_days/get_data_status/"
            "get_adjust_factors）内",
            details={"requested_contract": requested,
                     "tool": tool_name})]))

    try:
        if tool_name == "get_kline":
            envelope = get_kline_v3(args)
        elif tool_name == "get_stock_list":
            envelope = get_stock_list_v3(args)
        elif tool_name == "get_trading_days":
            envelope = get_trading_days_v3(args)
        elif tool_name == "get_adjust_factors":
            envelope = (research.get_adjust_factors_v3(args)
                        if research is not None else tool_failure([V3Error(
                            "UNSUPPORTED_CONTRACT",
                            "get_adjust_factors 的 v3 分支未加载"
                            "（market_v3_research 缺失）")]))
        else:
            envelope = get_data_status_v3(args)
    except V3Error as exc:
        envelope = tool_failure([exc])
    except Exception as exc:  # noqa: BLE001 - 未分类异常：机器错误 + request_id 可追踪
        envelope = tool_failure([V3Error(
            "INTERNAL_ERROR",
            f"v3 内部错误: {type(exc).__name__}: {exc}")])
    return _wrap(envelope)


def _wrap(envelope: dict) -> dict:
    """信封 → MCP tools/call result（双载荷：structuredContent + 恰好一个同对象
    JSON 文本块；失败时 isError=true，README §4.2-§4.3）。"""
    text = json.dumps(envelope, ensure_ascii=False, allow_nan=False)
    return {
        "content": [{"type": "text", "text": text}],
        "structuredContent": envelope,
        "isError": envelope.get("status") == "failed",
    }
