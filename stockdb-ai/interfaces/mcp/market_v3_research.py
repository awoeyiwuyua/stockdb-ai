#!/usr/bin/env python3
"""market_v3_research — 补充合同 stockdb-market-v3-research-extension（修订 1.0.0）服务端实现。

由 market_v3 在显式 contract_version="stockdb-market-v3" 请求上分流调用：
  - A 批 index_board_daily_v1：4 个指定指数（腾讯 ifzq 日K，价格链路 price-only）
    与 7 个东财 BK 板块（eastmoney push2his，源不可达时如实失败）。
  - B 批 adjusted_history_v1：get_adjust_factors v3 分支（FactorRequest/Response）、
    引擎 复权 cum 序列 → affine-v1 因子（multiplier>0、offset=0）、
    qfq/hfq 固定锚点、R/F/A 快照链与 Decimal 复算。
  - 长历史：500/250 交易日窗口按已核实日历计算；快照持久化 ≥7 天并可跨重启重读。

设计原则（DEVELOPMENT.md 逐节对应）：
  - 原始提供者隔离：指数=交易所/中证（编制方），板块=eastmoney；绝不跨 namespace
    串序列（I04），THS 仅在请求 THS 键时才有语义（本版不支持，如实拒绝）。
  - SDK 复权语义以 /opt/stockdb/pybao/stock_sdk.py 源码为证：
    qfq = raw × f_cur/f_latest、hfq = raw × f_cur（f=除权日累计因子 cum）→
    affine-v1 的 multiplier = cum(≤date)/cum(≤anchor)、offset = 0。
  - 量额不可证明时返回 null + null_reason（I06），不猜手/股换算。
  - R/F/A 快照持久化到磁盘（A15：正常重启后未到期快照可读）。
"""

from __future__ import annotations

import json
import os
import re
import threading
import time
import urllib.parse
import urllib.request
from datetime import datetime, timedelta
from decimal import Decimal, ROUND_HALF_UP, getcontext
from pathlib import Path
from typing import Any

getcontext().prec = 50  # Decimal 精度：12 位系数 × 8 位价格远小于 50 位

EXTENSION_CONTRACT = "stockdb-market-v3-research-extension"
EXTENSION_REVISION = "1.0.0"

EXTENSION_CONTRACT_VERSION = "stockdb-market-v3"  # 线协议（与基线一致，不混用）

# ---------------------------------------------------------------------------
# A 批：序列注册表（身份映射 + 公开证据；I01/I05）
# ---------------------------------------------------------------------------

# 指数：规范键 → 腾讯 ifzq symbol。identity_evidence 为公开编制方/发布市场页面，
# 证明「代码 ↔ 指数」映射（不从数字前缀推定）。
INDEX_SERIES: dict[str, dict[str, Any]] = {
    "XSHG/index/exchange/000001": {
        "symbol": "sh000001", "name": "上证指数", "compiler": "SSE",
        "calendar_exchange": "XSHG",
        "identity_evidence": "https://www.sse.com.cn/market/sseindex/indexlist/s/i/000001/",
        "price_decimals": 3,
    },
    "XSHE/index/exchange/399001": {
        "symbol": "sz399001", "name": "深证成指", "compiler": "SZSE",
        "calendar_exchange": "XSHE",
        "identity_evidence": "http://www.szse.cn/index/indexList.html (399001 深证成指)",
        "price_decimals": 3,
    },
    "XSHE/index/exchange/399006": {
        "symbol": "sz399006", "name": "创业板指", "compiler": "SZSE",
        "calendar_exchange": "XSHE",
        "identity_evidence": "http://www.szse.cn/index/indexList.html (399006 创业板指)",
        "price_decimals": 3,
    },
    # 中证1000：XSHG=本合同选择的行情发布市场定位；csi=编制者命名空间（中证指数有限公司）
    "XSHG/index/csi/000852": {
        "symbol": "sh000852", "name": "中证1000", "compiler": "CSI",
        "calendar_exchange": "XSHG",
        "identity_evidence": "https://www.csindex.com.cn/#/indices/family/detail?indexCode=000852",
        "price_decimals": 3,
    },
}

# 东财板块：规范键 → eastmoney secid。THS 参考序列仅为消费方既有映射背景，
# 不是身份断言；本服务不提供 THS 序列（请求 THS 键 → UNSUPPORTED_ASSET）。
BOARD_SERIES: dict[str, dict[str, Any]] = {
    "CN/board/eastmoney/BK1408": {"secid": "90.BK1408", "name": "机器人",
                                  "ths_reference": "886069 人形机器人"},
    "CN/board/eastmoney/BK1134": {"secid": "90.BK1134", "name": "算力",
                                  "ths_reference": "885957 东数西算(算力)"},
    "CN/board/eastmoney/BK1138": {"secid": "90.BK1138", "name": "液冷",
                                  "ths_reference": "886044 液冷服务器"},
    "CN/board/eastmoney/BK1101": {"secid": "90.BK1101", "name": "先进封装",
                                  "ths_reference": "886009 先进封装"},
    "CN/board/eastmoney/BK1037": {"secid": "90.BK1037", "name": "消费电子",
                                  "ths_reference": "881124 消费电子"},
    "CN/board/eastmoney/BK0728": {"secid": "90.BK0728", "name": "环保",
                                  "ths_reference": "881181 环境治理"},
    "CN/board/eastmoney/BK1160": {"secid": "90.BK1160", "name": "折叠屏",
                                  "ths_reference": "885809 柔性屏(折叠屏)"},
}

INDEX_NAMESPACES = {"exchange", "csi"}
BOARD_NAMESPACES = {"eastmoney"}
# 板块日历依据：东财板块按 A 股交易日发布，本服务以 XSHG 日历为依据并在
# series_catalog 中声明（I05：CN 板块必须明确所依据交易所日历）。
BOARD_CALENDAR_EXCHANGE = "XSHG"

_UNITS_NULL_REASON_INDEX = (
    "tencent ifzq day rows expose a single volume figure whose unit (shares vs "
    "lots) is not verifiable from the source, and no per-day amount field "
    "exists; price-only acceptance per DEVELOPMENT.md §3.3")
_UNITS_NULL_REASON_BOARD = (
    "eastmoney board kline volume/amount units are not independently "
    "verifiable while the endpoint is unreachable; price-only acceptance per "
    "DEVELOPMENT.md §3.3")

# ---------------------------------------------------------------------------
# 通用小工具
# ---------------------------------------------------------------------------


def _iso(date8: str) -> str:
    return f"{date8[:4]}-{date8[4:6]}-{date8[6:8]}"


def _now_iso() -> str:
    return datetime.now().astimezone().isoformat(timespec="seconds")


class ResearchError(Exception):
    """research 内部错误（已带合同错误码），由 market_v3 转 V3Error 信封。"""

    def __init__(self, code: str, message: str, *, details: dict | None = None):
        super().__init__(message)
        self.code = code
        self.message = message
        self.details = details or {}


# ---------------------------------------------------------------------------
# HTTP 抓取（纯标准库；带浏览器形态头，quote_sources 同款经验）
# ---------------------------------------------------------------------------

_UA = ("Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 "
       "(KHTML, like Gecko) Chrome/120.0 Safari/537.36")


def _http_json(url: str, *, referer: str, timeout: float,
               headers: dict | None = None) -> Any:
    merged = {"User-Agent": _UA, "Referer": referer, "Accept": "*/*"}
    if headers:
        merged.update(headers)
    req = urllib.request.Request(url, headers=merged)
    with urllib.request.urlopen(req, timeout=timeout) as resp:
        body = resp.read()
    return json.loads(body)


# ---------------------------------------------------------------------------
# 腾讯 ifzq 指数日K（A 批指数数据路径）
# ---------------------------------------------------------------------------

# 腾讯日K主机轮转（2026-10-25 实测：可达性随网络位置漂移——NAS 容器内
# ifzq.gtimg.cn 可用而 web./proxy. 变体 501；开发机反之。三主机轮转+页级重试）
_IFZQ_HOSTS = ("https://ifzq.gtimg.cn/appstock/app/fqkline/get",
               "https://web.ifzq.gtimg.cn/appstock/app/fqkline/get",
               "https://proxy.finance.qq.com/ifzqgtimg/appstock/app/"
               "fqkline/get")
_IFZQ_PAGE = 760  # 单次 count：实测 ≤1200 稳定；按 760 分页穷尽并去重（H03）


def _ifzq_get(param: str, timeout: float) -> dict:
    """逐主机尝试（后进先换），全部失败抛最后一个异常。"""
    last_exc: Exception | None = None
    for host in _IFZQ_HOSTS:
        for _attempt in range(2):
            try:
                return _http_json(f"{host}?param={urllib.parse.quote(param)}",
                                  referer="https://gu.qq.com/", timeout=timeout)
            except Exception as exc:  # noqa: BLE001 - 主机/次级重试
                last_exc = exc
    raise last_exc  # type: ignore[misc]


_SINA_URL = ("https://quotes.sina.cn/cn/api/json_v2.php/"
             "CN_MarketDataService.getKLineData")


def _sina_index_daily(symbol: str, start8: str, end8: str, *,
                      timeout: float = 15.0) -> list[dict]:
    """新浪指数日K（第四提供者；2026-10-25 实测三腾讯主机同遭 WAF 后引入）。

    单次 datalen 覆盖窗口交易日数（实测 ≤1100 稳定）；行 {day,open,high,low,
    close,volume}。volume 单位同样不可独立证明 → 不取（price-only 一致口径）。
    """
    from interfaces.mcp import market_v3 as v3
    try:
        slots = v3._expected_slots("XSHG", start8, end8)
    except Exception:  # noqa: BLE001
        slots = None
    datalen = min(1100, (len(slots) + 16 if slots else 64))
    data = _http_json(
        f"{_SINA_URL}?symbol={urllib.parse.quote(symbol)}"
        f"&scale=240&ma=no&datalen={datalen}",
        referer="https://finance.sina.com.cn/", timeout=timeout)
    rows: dict[str, dict] = {}
    if isinstance(data, list):
        for item in data:
            day = str(item.get("day", ""))
            d8 = day.replace("-", "")
            if start8 <= d8 <= end8:
                rows[d8] = {"date8": d8, "open": str(item.get("open")),
                            "close": str(item.get("close")),
                            "high": str(item.get("high")),
                            "low": str(item.get("low"))}
    return [rows[k] for k in sorted(rows)]


def fetch_index_daily(symbol: str, start8: str, end8: str,
                      *, timeout: float = 12.0) -> tuple[str, list[dict]]:
    """指数日K多提供者轮转：腾讯三主机 → 新浪。返回 (provider, rows)。

    单一提供者整体故障（如 WAF 封 IP）时换提供者如实续供；series_catalog 的
    series_id/source_catalog 按实际服务提供者记录（不冒充单一来源）。
    """
    errors: list[str] = []
    for provider, fetch in (("tencent", tencent_index_daily),
                            ("sina", _sina_index_daily)):
        try:
            rows = fetch(symbol, start8, end8, timeout=timeout)
            if rows:
                return provider, rows
            errors.append(f"{provider}: empty window")
        except Exception as exc:  # noqa: BLE001 - 逐提供者收集
            errors.append(f"{provider}: {type(exc).__name__}: {exc}"[:120])
    raise ResearchError(
        "SOURCE_UNAVAILABLE",
        "指数日K全部提供者失败（腾讯三主机 + 新浪）；" + "; ".join(errors)[:200],
        details={"providers": ["tencent", "sina"], "errors": errors})


def tencent_index_daily(symbol: str, start8: str, end8: str, *,
                        timeout: float = 12.0) -> list[dict]:
    """腾讯日K → 行 [{date8, open, high, low, close}]（升序、去重、区间过滤）。

    行结构 [date, open, close, high, low, volume]（注意 close 在 high 前，
    2026-10-23 上证 qt 快照交叉核对）；volume 单位不可证明 → 不取。
    分页：count 固定、起点逐步推进，直到覆盖 start 或源数据耗尽。
    """
    rows: dict[str, dict] = {}
    window_start = datetime.strptime(start8, "%Y%m%d").date()
    window_end = datetime.strptime(end8, "%Y%m%d").date()
    cursor = window_start
    for _ in range(12):  # 上限防护：12 页 × 760 交易日 >> 1000 日探测窗
        param = (f"{symbol},day,{cursor.isoformat()},"
                 f"{window_end.isoformat()},{_IFZQ_PAGE},")
        data = _ifzq_get(param, timeout)
        node = ((data.get("data") or {}).get(symbol) or {})
        day_rows = node.get("day") or []
        if not day_rows:
            break
        got_new = False
        for row in day_rows:
            if not isinstance(row, list) or len(row) < 6:
                continue
            d = str(row[0])
            try:
                ddate = datetime.strptime(d, "%Y-%m-%d").date()
            except ValueError:
                continue
            if window_start <= ddate <= window_end:
                if d not in rows:
                    got_new = True
                rows[d] = {
                    "date8": ddate.strftime("%Y%m%d"),
                    "open": str(row[1]), "close": str(row[2]),
                    "high": str(row[3]), "low": str(row[4]),
                }
        if not got_new:
            break
        last_date = datetime.strptime(str(day_rows[-1][0]), "%Y-%m-%d").date()
        if last_date >= window_end:
            break
        cursor = last_date  # 下一页从本页最后一日起（去重后拼接，边界相接不重不漏）
    return [rows[k] for k in sorted(rows)]


# ---------------------------------------------------------------------------
# eastmoney 板块日K（A 批板块数据路径；源不可达时如实失败）
#
# 2026-10-29 根因实测（见交付报告）：push2his 集群（92/21/40/默认域同一 IP 簇）
# 上线了行为风控——对非浏览器客户端（urllib/curl 等无完整浏览器形态的请求）
# 静默断连（TCP/TLS 握手正常、HTTP 请求后 0 字节关闭，RemoteDisconnected），
# 并按客户端行为评分升级：连续自动化形态请求 → 滑块验证码 → IP 级全断（含
# 真实浏览器）。原实现每次请求连发 4 主机 + 无退避 + 无缓存，持续触发并维持
# 惩罚状态（消费端 10-27 缺日、10-29 SOURCE_UNAVAILABLE 的直接原因）。
#
# 应对（不绕风控、不造假）：
#   1. polite 客户端——完整浏览器形态头（UA/Accept-Language/逐板块 Referer）、
#      站点公开 ut 参数、持久 qgqp_b_id 设备 Cookie（首次生成随机 32hex）；
#   2. 风控退避——RemoteDisconnected/BadStatusLine 等静默断连判定为 waf_drop，
#      全局冷却（30min 起指数退避至 4h 封顶）；冷却期内 fail-fast 不触网，
#      绝不把「连发重试」变成维持封禁的流量；仅超时/网络不可达类错误轮转主机；
#   3. 持久缓存——每板块日K逐日落盘（真实取得才写），上游失败时如实以缓存
#      服务（partial + gaps 由基线缺口机制如实上报），绝无数据的日子不制造；
#   4. 缺口补采——请求路径合并补采 + 低频后台巡检（冷却许可时按缺口窗口拉取）。
# ---------------------------------------------------------------------------

_EM_HOSTS = ("92.push2his.eastmoney.com", "21.push2his.eastmoney.com",
             "40.push2his.eastmoney.com", "push2his.eastmoney.com")
_EM_REFERER = "https://quote.eastmoney.com/bk/{secid}.html"
# 站点网页同款公开 token（quote.eastmoney.com 页面 JS 实测携带，非凭证）
_EM_UT = "fa5fd1943c7b386f172d6893dbfba10b"
_EM_UA = ("Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
          "(KHTML, like Gecko) Chrome/126.0.0.0 Safari/537.36")
# waf_drop 全局冷却：30min 起、按连败指数退避、4h 封顶
_EM_WAF_COOLDOWN_BASE = 1800.0
_EM_WAF_COOLDOWN_MAX = 4 * 3600.0
_EM_HOST_RETRY_SLEEP = 2.0  # 轮转下一主机前的最小间隔（同簇主机不连发）

_EM_STATE_LOCK = threading.Lock()
_EM_LAST_STATE: dict[str, Any] = {
    "ok_at": 0.0, "fail_at": 0.0, "fail_reason": None,
    "cooldown_until": 0.0, "backoff_streak": 0,
    "last_drop_class": None, "requests_total": 0, "requests_ok": 0,
}
_EM_BACKFILL_STATE: dict[str, Any] = {"last_cycle_at": 0.0, "last_summary": None}


def em_board_availability() -> dict[str, Any]:
    """最近一次东财访问结果（状态工具只读缓存，绝不同步现查上游）。"""
    defaults: dict[str, Any] = {
        "ok_at": 0.0, "fail_at": 0.0, "fail_reason": None,
        "cooldown_until": 0.0, "backoff_streak": 0,
        "last_drop_class": None, "requests_total": 0, "requests_ok": 0,
    }
    with _EM_STATE_LOCK:
        state = {**defaults, **_EM_LAST_STATE}
        state = {k: v for k, v in state.items() if not k.startswith("_")}
        state["backfill"] = {
            "last_cycle_at": _EM_BACKFILL_STATE.get("last_cycle_at", 0.0),
            "last_summary": _EM_BACKFILL_STATE.get("last_summary"),
        }
        return state


def _em_record(ok: bool, reason: str | None = None, **extra: Any) -> None:
    with _EM_STATE_LOCK:
        if ok:
            _EM_LAST_STATE.update(ok_at=time.time(), fail_reason=None,
                                  backoff_streak=0, cooldown_until=0.0,
                                  last_drop_class=None)
        else:
            _EM_LAST_STATE.update(fail_at=time.time(), fail_reason=reason)
        _EM_LAST_STATE.update(extra)


def _em_client_state() -> dict:
    """持久客户端状态：qgqp_b_id Cookie + 退避连败数/冷却截止。

    落盘目录复用快照目录解析（config.DATA_DIR/env/回退）；状态跨重启保留，
    避免每次重启都换新设备指纹（那才是可疑形态）。文件不可写时退化为进程内
    状态（不阻塞响应）。
    """
    with _EM_STATE_LOCK:
        cached = _EM_LAST_STATE.get("_client_state")
    if cached is not None:
        return cached
    state: dict[str, Any] = {}
    path = None
    try:
        path = _board_cache_dir() / "em_client_state.json"
        if path.exists():
            loaded = json.loads(path.read_text(encoding="utf-8"))
            if isinstance(loaded, dict):
                state = loaded
    except Exception:  # noqa: BLE001 - 状态读不到就从零开始
        state = {}
    if not state.get("qgqp_b_id"):
        import secrets
        state["qgqp_b_id"] = secrets.token_hex(16)
    state.setdefault("backoff_streak", 0)
    state.setdefault("cooldown_until", 0.0)
    if path is not None:
        try:
            path.parent.mkdir(parents=True, exist_ok=True)
            tmp = path.with_suffix(".tmp")
            tmp.write_text(json.dumps(state, ensure_ascii=False),
                           encoding="utf-8")
            tmp.replace(path)
        except OSError:
            pass  # 磁盘不可写：进程内状态兜底
    with _EM_STATE_LOCK:
        _EM_LAST_STATE["_client_state"] = state
    return state


def _em_persist_state() -> None:
    state = _em_client_state()
    try:
        path = _board_cache_dir() / "em_client_state.json"
        tmp = path.with_suffix(".tmp")
        tmp.write_text(json.dumps(state, ensure_ascii=False), encoding="utf-8")
        tmp.replace(path)
    except Exception:  # noqa: BLE001 - 状态持久化尽力而为
        pass


def _em_headers(secid: str) -> dict:
    state = _em_client_state()
    return {
        "User-Agent": _EM_UA,
        "Accept": "*/*",
        "Accept-Language": "zh-CN,zh;q=0.9",
        "Referer": _EM_REFERER.format(secid=secid),
        "Cookie": f"qgqp_b_id={state.get('qgqp_b_id', '')}",
    }


def _em_is_waf_drop(exc: Exception) -> bool:
    """静默断连类错误（风控特征）：TCP/TLS 正常但 HTTP 前被掐断。"""
    import http.client
    return isinstance(exc, (http.client.RemoteDisconnected,
                            http.client.BadStatusLine,
                            http.client.IncompleteRead))


def _em_enter_cooldown(reason: str, drop_class: str) -> float:
    """进入全局风控冷却（指数退避）。返回冷却截止 epoch。"""
    state = _em_client_state()
    state["backoff_streak"] = int(state.get("backoff_streak", 0)) + 1
    streak = state["backoff_streak"]
    cooldown = min(_EM_WAF_COOLDOWN_BASE * (2 ** (streak - 1)),
                   _EM_WAF_COOLDOWN_MAX)
    state["cooldown_until"] = time.time() + cooldown
    _em_persist_state()
    _em_record(False, reason, cooldown_until=state["cooldown_until"],
               backoff_streak=streak, last_drop_class=drop_class)
    return state["cooldown_until"]


def eastmoney_board_daily(secid: str, start8: str, end8: str, *,
                          timeout: float = 8.0) -> list[dict]:
    """push2his 板块日K → 行 [{date8, open, high, low, close}]。

    klines CSV：f51 日期, f52 open, f53 close, f54 high, f55 low, f56 volume,
    f57 amount（量额单位不可独立证明 → 不取，price-only）。
    风控冷却期内 fail-fast（不触网，如实 SOURCE_UNAVAILABLE）；waf_drop 触发
    全局退避且不再轮转同簇主机（连发只会加深惩罚）；仅超时/网络不可达轮转。
    """
    state = _em_client_state()
    now = time.time()
    cooldown_until = float(state.get("cooldown_until") or 0.0)
    if now < cooldown_until:
        remaining = int(cooldown_until - now)
        _em_record(False, f"风控冷却中（剩余 {remaining}s 不触网）",
                   cooldown_until=cooldown_until,
                   backoff_streak=int(state.get("backoff_streak") or 0),
                   last_drop_class="waf_cooldown")
        raise ResearchError(
            "SOURCE_UNAVAILABLE",
            f"eastmoney 板块源处于风控退避期（上次静默断连后冷却，"
            f"剩余 {remaining}s）；期间不触网重试，避免维持封禁状态；"
            "已有缓存如实服务，缺口按合同上报",
            details={"cooldown_remaining_s": remaining,
                     "backoff_streak": state.get("backoff_streak"),
                     "reason": "waf_backoff_cooldown"})
    errors: list[str] = []
    for idx, host in enumerate(_EM_HOSTS):
        url = (f"https://{host}/api/qt/stock/kline/get?"
               f"secid={urllib.parse.quote(secid)}&"
               f"ut={_EM_UT}&fields1=f1,f2,f3&"
               "fields2=f51,f52,f53,f54,f55,f56,f57"
               f"&klt=101&fqt=0&beg={start8}&end={end8}")
        try:
            with _EM_STATE_LOCK:
                _EM_LAST_STATE["requests_total"] = (
                    _EM_LAST_STATE.get("requests_total", 0) + 1)
            data = _http_json(url, referer=_EM_REFERER.format(secid=secid),
                              timeout=timeout, headers=_em_headers(secid))
        except Exception as exc:  # noqa: BLE001 - 逐主机分类收集失败原因
            desc = f"{host}: {type(exc).__name__}: {exc}"[:160]
            errors.append(desc)
            if _em_is_waf_drop(exc):
                # 风控静默断连：不再轮转（同簇同策略），进入全局冷却
                until = _em_enter_cooldown(
                    "; ".join(errors)[-400:], "waf_drop")
                raise ResearchError(
                    "SOURCE_UNAVAILABLE",
                    "eastmoney push2his 对本客户端静默断连（行为风控），"
                    f"已进入全局退避至 {_iso_time(until)}；"
                    "不轮转同簇主机连发重试，避免维持封禁；按合同如实失败",
                    details={"hosts": list(_EM_HOSTS), "errors": errors[:4],
                             "cooldown_until": until,
                             "backoff_streak": _em_client_state().get(
                                 "backoff_streak")}) from exc
            if idx + 1 < len(_EM_HOSTS):
                time.sleep(_EM_HOST_RETRY_SLEEP)
            continue
        klines = ((data.get("data") or {}).get("klines")) or []
        rows: dict[str, dict] = {}
        for line in klines:
            parts = str(line).split(",")
            if len(parts) < 6:
                continue
            d = parts[0].replace("-", "")
            if not (start8 <= d <= end8):
                continue
            rows[d] = {"date8": d, "open": parts[1], "close": parts[2],
                       "high": parts[3], "low": parts[4]}
        state["backoff_streak"] = 0
        state["cooldown_until"] = 0.0
        _em_persist_state()
        with _EM_STATE_LOCK:
            _EM_LAST_STATE["requests_ok"] = (
                _EM_LAST_STATE.get("requests_ok", 0) + 1)
        _em_record(True)
        return [rows[k] for k in sorted(rows)]
    # 全主机超时/网络不可达（非风控断连）：如实失败，不进风控退避
    _em_record(False, "; ".join(errors)[:400])
    raise ResearchError(
        "SOURCE_UNAVAILABLE",
        f"eastmoney 板块日K源当前不可达（已轮转 {_EM_HOSTS} 全部失败）；"
        "按合同如实失败，不用其他提供者序列冒充（I04）",
        details={"hosts": list(_EM_HOSTS), "errors": errors[:4]})


def _iso_time(epoch: float) -> str:
    return datetime.fromtimestamp(epoch).astimezone().isoformat(
        timespec="seconds")


# ---------------------------------------------------------------------------
# 板块日K持久缓存（真实取得才写；上游失败时如实以缓存服务 + 缺口上报）
# ---------------------------------------------------------------------------


def _board_cache_dir() -> Path:
    return _snapshot_dir() / "board_daily_cache"


def _board_cache_path(key: str) -> Path:
    safe = re.sub(r"[^A-Za-z0-9_.-]", "_", key)
    return _board_cache_dir() / f"{safe}.json"


def _board_cache_load(key: str) -> dict:
    path = _board_cache_path(key)
    try:
        loaded = json.loads(path.read_text(encoding="utf-8"))
        if (isinstance(loaded, dict) and isinstance(loaded.get("bars"), dict)
                and loaded.get("instrument_id") == key):
            return loaded
    except (OSError, json.JSONDecodeError):
        pass
    return {"instrument_id": key, "bars": {}, "updated_at": None,
            "fetch_attempts": 0, "fetch_failures": 0}


def _board_cache_save(key: str, store: dict) -> None:
    try:
        path = _board_cache_path(key)
        path.parent.mkdir(parents=True, exist_ok=True)
        store["updated_at"] = _now_iso()
        tmp = path.with_suffix(".tmp")
        tmp.write_text(json.dumps(store, ensure_ascii=False),
                       encoding="utf-8")
        tmp.replace(path)
    except OSError:
        pass  # 缓存写失败不阻塞响应（内存内本次合并仍有效）


def board_daily_cached(key: str, secid: str, start8: str, end8: str, *,
                       timeout: float = 8.0) -> tuple[list[dict], dict]:
    """板块日K读取（缓存优先合并）：→ (行, cache_info)。

    - 上游成功：逐日 upsert（源修订覆盖旧值 = 允许的历史修订；不删除源未返回
      的既有日——源 200 降级响应不删真实历史）；服务窗口 = 源行 ∪ 缓存行
      （按日合并）。
    - 上游失败且缓存覆盖窗口内有真实已取日：如实以缓存服务（缺口由基线
      expected-slots 机制上报 partial + INCOMPLETE_COVERAGE，绝无虚造）；
      cache_info.cache_served=True、retrieved_at=窗口内最旧已取时间（来源
      可追溯，不得把缓存读取伪标为实时抓取）。
    - 上游失败且窗口内无任何缓存：原样抛出（与既有诚实失败行为一致）。
    """
    native = key.split("/")[-1]
    store = _board_cache_load(key)
    bars = store.setdefault("bars", {})
    now_iso = _now_iso()
    cache_info: dict[str, Any] = {"cache_served": False, "retrieved_at": None}
    try:
        rows = eastmoney_board_daily(secid, start8, end8, timeout=timeout)
    except ResearchError as exc:
        store["fetch_attempts"] = int(store.get("fetch_attempts", 0)) + 1
        store["fetch_failures"] = int(store.get("fetch_failures", 0)) + 1
        cached_rows = []
        oldest = None
        for d in sorted(bars):
            if start8 <= d <= end8:
                cached_rows.append({"date8": d, **{
                    f: bars[d][f] for f in ("open", "high", "low", "close")}})
                ts = bars[d].get("retrieved_at")
                if ts and (oldest is None or ts < oldest):
                    oldest = ts
        if not cached_rows:
            _board_cache_save(key, store)
            raise
        cache_info["cache_served"] = True
        cache_info["retrieved_at"] = oldest
        cache_info["source_error"] = exc.message[:200]
        _board_cache_save(key, store)
        return cached_rows, cache_info
    store["fetch_attempts"] = int(store.get("fetch_attempts", 0)) + 1
    fresh_dates: set[str] = set()
    for row in rows:
        d = row["date8"]
        entry = {f: row[f] for f in ("open", "high", "low", "close")}
        prior = bars.get(d)
        if prior and any(str(prior.get(f)) != str(entry[f])
                         for f in ("open", "high", "low", "close")):
            revisions = store.setdefault("revisions", {})
            revisions[d] = {"at": now_iso,
                            "from": {f: prior.get(f)
                                     for f in ("open", "high", "low", "close")}}
        entry["retrieved_at"] = now_iso
        bars[d] = entry
        fresh_dates.add(d)
    _board_cache_save(key, store)
    # 服务窗口 = 源行 ∪ 缓存行（源 200 降级不丢既有真实日）
    merged = [dict(row) for row in rows]
    seen = set(fresh_dates)
    extra_cached = 0
    oldest = None
    for d in sorted(bars):
        if d in seen or not (start8 <= d <= end8):
            continue
        merged.append({"date8": d, **{
            f: bars[d][f] for f in ("open", "high", "low", "close")}})
        extra_cached += 1
        ts = bars[d].get("retrieved_at")
        if ts and (oldest is None or ts < oldest):
            oldest = ts
    merged.sort(key=lambda r: r["date8"])
    if extra_cached:
        cache_info["merged_cached_extras"] = extra_cached
        cache_info["retrieved_at"] = oldest
    return merged, cache_info


def board_cache_gap_report(latest_completed8: str | None = None) -> dict:
    """各板块缓存缺口（对照 XSHG 日历；只统计 < latest_completed8 的交易日）。

    latest_completed8 缺省取引擎最新交易日（只读 HEALTH，不触发引擎探测）；
    不可用时用「今天」——今天当日收盘前的缺日不算缺口（final 语义另行处理）。
    """
    from interfaces.mcp import market_v3 as v3
    if latest_completed8 is None:
        latest_completed8 = (v3.HEALTH.get().get("latest") or
                             datetime.now().strftime("%Y%m%d"))
    report: dict[str, Any] = {}
    window_start = max(
        v3.CALENDAR_COVERAGE_START.replace("-", ""),
        (datetime.now() - timedelta(days=820)).strftime("%Y%m%d"))
    window_end = datetime.now().strftime("%Y%m%d")
    for key, meta in BOARD_SERIES.items():
        store = _board_cache_load(key)
        cached = set((store.get("bars") or {}).keys())
        try:
            slots = v3._expected_slots("XSHG", window_start, window_end) or []
        except Exception:  # noqa: BLE001 - 日历不可用按空处理
            slots = []
        dates8 = [d.replace("-", "") for d in slots]
        gaps = [d for d in dates8
                if d not in cached and d <= latest_completed8
                and d >= (meta.get("board_start8") or "00000000")]
        report[key] = {
            "name": meta["name"], "secid": meta["secid"],
            "cached_days": len(cached),
            "cache_first": min(cached) if cached else None,
            "cache_last": max(cached) if cached else None,
            "gap_days": gaps,
            "gap_count": len(gaps),
        }
    return report


def board_backfill_cycle(*, force: bool = False) -> dict:
    """缺口补采一轮：逐板块按缺口窗口拉取合并（冷却许可时）。

    有界重试：一轮每板块至多 1 次源请求；板块间 1.5s 间隔；全局风控冷却
    未到期时整体跳过（force 仅供运维/测试显式穿透）。
    """
    state = _em_client_state()
    now = time.time()
    cooldown_until = float(state.get("cooldown_until") or 0.0)
    summary: dict[str, Any] = {"started_at": _now_iso(), "boards": {},
                               "skipped": None}
    if not force and now < cooldown_until:
        summary["skipped"] = (f"风控冷却中（剩余 {int(cooldown_until - now)}s）"
                              "——不触网，缺口等待下一轮")
        with _EM_STATE_LOCK:
            _EM_BACKFILL_STATE.update(last_cycle_at=time.time(),
                                      last_summary=summary)
        return summary
    report = board_cache_gap_report()
    touched = 0
    for key, info in report.items():
        if not info["gap_days"]:
            summary["boards"][key] = {"action": "none",
                                      "gap_count": 0}
            continue
        if touched >= 7:  # 一轮有界：至多覆盖全部 7 个已声明板块一次
            summary["boards"][key] = {"action": "deferred"}
            continue
        meta = BOARD_SERIES[key]
        beg = info["gap_days"][0]
        end = max(info["gap_days"][-1],
                  datetime.now().strftime("%Y%m%d"))
        try:
            _rows, _ci = board_daily_cached(key, meta["secid"], beg, end)
            after = board_cache_gap_report(
                datetime.now().strftime("%Y%m%d"))[key]
            summary["boards"][key] = {
                "action": "fetched", "window": [beg, end],
                "gap_before": info["gap_count"],
                "gap_after": after["gap_count"]}
        except ResearchError as exc:
            summary["boards"][key] = {"action": "failed",
                                      "error": exc.code,
                                      "detail": exc.message[:160]}
        touched += 1
        time.sleep(1.5)  # 板块间礼貌间隔
    with _EM_STATE_LOCK:
        _EM_BACKFILL_STATE.update(last_cycle_at=time.time(),
                                  last_summary=summary)
    return summary


_backfill_thread_started = False
_backfill_thread_lock = threading.Lock()


def _ensure_backfill_thread() -> None:
    """启动低频缺口巡检线程（进程级一次；kill-switch 立即退出）。

    单测/验收 stub 通过 STOCKDB_V3_BOARD_BACKFILL=off 关闭；正式服务默认开。
    首轮延迟 90s（避免短生命周期进程触网），之后每 20min±120s 一轮，且只在
    存在缺口且冷却许可时真正出网。
    """
    global _backfill_thread_started
    kill = os.environ.get("STOCKDB_V3_BOARD_BACKFILL", "").strip().lower()
    if kill in ("0", "off", "false", "no"):
        return
    with _backfill_thread_lock:
        if _backfill_thread_started:
            return
        _backfill_thread_started = True

    def _loop() -> None:
        time.sleep(90)
        while True:
            try:
                board_backfill_cycle()
            except Exception:  # noqa: BLE001 - 巡检线程绝不带崩服务
                pass
            time.sleep(1200 + (time.time() % 240) - 120)

    threading.Thread(target=_loop, daemon=True,
                     name="em-board-backfill").start()


# ---------------------------------------------------------------------------
# 引擎复权事件（B 批因子原始数据；语义证据见模块 docstring）
# ---------------------------------------------------------------------------

_factor_cache: dict[str, Any] = {}
_factor_lock = threading.Lock()
_FACTOR_TTL = 300.0


def _engine():
    from interfaces.mcp import market_v3 as v3
    return v3


def engine_factor_events(code: str) -> list[tuple[str, dict]]:
    """引擎 复权:<code>:<date> 全事件（升序）。TTL 缓存。

    返回 [(date8, {div, give, trans, mult, cum})]；行语义：
      - div/give/trans：每股现金分红(元)/送股/转增（引擎原始字段，供事件证据）；
      - mult：本事件单次因子 = pre_close/(pre_close-调整)，送转时含股本项；
      - cum：截至本事件的累计因子，cum_n = cum_{n-1} × mult_n
        （实测 000001: 134.934×1.022=137.903 ✓）。
    SDK 消费方式（stock_sdk.py _apply_fq_in_memory）：qfq=raw×f_cur/f_latest、
    hfq=raw×f_cur，f=≤当日最近事件 cum（首个事件前=1.0）。
    """
    now = time.time()
    with _factor_lock:
        hit = _factor_cache.get(code)
        if hit and now - hit["at"] <= _FACTOR_TTL:
            return list(hit["events"])
    events: list[tuple[str, dict]] = []
    try:
        for year in range(1991, datetime.now().year + 1):
            rows = _engine().engine_get(f"复权:{code}:{year}*",
                                        cmd="get", timeout=15.0)
            if not isinstance(rows, list):
                continue
            for item in rows:
                if (isinstance(item, list) and len(item) == 2
                        and isinstance(item[1], dict)):
                    date8 = str(item[0]).split(":")[-1]
                    if len(date8) == 8 and date8.isdigit():
                        events.append((date8, dict(item[1])))
    except Exception as exc:  # noqa: BLE001 - 引擎失败按源错误上抛
        raise ResearchError("SOURCE_UNAVAILABLE",
                            f"读取引擎复权表失败: {type(exc).__name__}: {exc}"[:200]
                            ) from exc
    events.sort(key=lambda e: e[0])
    with _factor_lock:
        _factor_cache[code] = {"at": now, "events": events}
    return list(events)


def cum_at(events: list[tuple[str, dict]], date8: str) -> Decimal:
    """>=当日的最近除权日（≤date8 的最大事件日）对应 cum；首个事件前 = 1。

    与 SDK bisect_right(dates, r_date)-1 语义一致（事件当日使用新 cum）。
    """
    value = Decimal(1)
    for event_date, fields in events:
        if event_date <= date8:
            value = Decimal(repr(float(fields.get("cum", 1.0))))
        else:
            break
    return value


def engine_factor_events_inject(code: str, events: list[tuple[str, dict]]) -> None:
    """测试注入：直接替换某码的事件缓存。"""
    with _factor_lock:
        _factor_cache[code] = {"at": time.time(), "events": list(events)}


# ---------------------------------------------------------------------------
# 持久化快照仓（A15：R/F/A 重启可读；≥7 天保留）
# ---------------------------------------------------------------------------


def _snapshot_dir() -> Path:
    """快照落盘目录：config.DATA_DIR（容器 /data）/env 覆盖；不可写时回退临时目录。

    持久化是 A15 的硬要求，但目录不可用不能阻塞响应——回退目录下持久化
    仍然生效（重启后同目录可读），只是与业务数据不同卷。
    """
    cached = _snapshot_dir_cached.get("path")
    if cached is not None:
        return cached
    candidates = []
    try:
        import config
        candidates.append(Path(config.DATA_DIR) / "market_v3_snapshots")
    except Exception:  # noqa: BLE001 - 裁剪部署无 config
        pass
    env_dir = os.environ.get("STOCKDB_V3_SNAPSHOT_DIR")
    if env_dir:
        candidates.append(Path(env_dir))
    candidates.append(Path("/data/market_v3_snapshots"))
    import tempfile
    candidates.append(Path(tempfile.gettempdir()) / "stockdb_v3_snapshots")
    for path in candidates:
        try:
            path.mkdir(parents=True, exist_ok=True)
            probe = path / ".probe"
            probe.write_text("ok", encoding="utf-8")
            probe.unlink(missing_ok=True)
            _snapshot_dir_cached["path"] = path
            return path
        except OSError:
            continue
    raise RuntimeError("no writable snapshot directory available")


_snapshot_dir_cached: dict[str, Path | None] = {"path": None}


class PersistentSnapshotStore:
    """内存 + 磁盘双层快照仓（与 market_v3.SnapshotStore 同接口）。

    - capture()：内存登记 + 原子落盘（tmp+rename）；
    - get()：内存未命中时从磁盘按需加载（重启后未到期快照可读）；
    - 过期/超量淘汰同步删盘；clear() 同时清两层（测试辅助）。
    """

    def __init__(self) -> None:
        self._lock = threading.Lock()
        self._items: dict[str, dict] = {}
        self._disk_checked: set[str] = set()

    @staticmethod
    def _path(snapshot_id: str) -> Path:
        safe = re.sub(r"[^A-Za-z0-9_.-]", "_", snapshot_id)
        return _snapshot_dir() / f"{safe}.json"

    def _purge_locked(self) -> None:
        from interfaces.mcp import market_v3 as v3
        now = time.time()
        for key in [k for k, v in self._items.items() if v["expires_at"] <= now]:
            self._unlink(key)
            del self._items[key]
        total_rows = sum(v.get("row_count", 0) for v in self._items.values())
        while ((len(self._items) > v3._SNAPSHOT_COUNT_CAP
                or total_rows > v3._SNAPSHOT_ROW_CAP) and self._items):
            oldest = min(self._items, key=lambda k: self._items[k]["created_at"])
            total_rows -= self._items[oldest].get("row_count", 0)
            self._unlink(oldest)
            del self._items[oldest]

    def _unlink(self, snapshot_id: str) -> None:
        try:
            self._path(snapshot_id).unlink(missing_ok=True)
        except OSError:
            pass

    def capture(self, snapshot_id: str, payload: dict, *,
                row_count: int = 0) -> None:
        from interfaces.mcp import market_v3 as v3
        now = time.time()
        item = {"payload": payload, "row_count": row_count,
                "created_at": now,
                "expires_at": now + v3.SNAPSHOT_RETENTION_SECONDS}
        path = self._path(snapshot_id)
        try:
            tmp = path.with_suffix(".tmp")
            tmp.write_text(json.dumps(item, ensure_ascii=False,
                                      allow_nan=False), encoding="utf-8")
            tmp.replace(path)
        except OSError:
            pass  # 磁盘写失败不阻塞响应；重启持久化尽力而为并如实失败（NOT_FOUND）
        with self._lock:
            self._items[snapshot_id] = item
            self._purge_locked()

    def get(self, snapshot_id: str) -> dict | None:
        with self._lock:
            item = self._items.get(snapshot_id)
        if item is not None:
            return item
        if snapshot_id in self._disk_checked:
            return None
        path = self._path(snapshot_id)
        self._disk_checked.add(snapshot_id)
        if not path.exists():
            return None
        try:
            item = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            return None
        if not isinstance(item, dict) or "payload" not in item:
            return None
        with self._lock:
            self._items.setdefault(snapshot_id, item)
        return item

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
        with self._lock:
            for key in list(self._items):
                self._unlink(key)
            self._items.clear()
            self._disk_checked.clear()


# ---------------------------------------------------------------------------
# series_catalog（I05：逐请求证券的 SeriesDescriptor）
# ---------------------------------------------------------------------------


def series_descriptor(key: str, *, kind: str, rows: list[dict] | None,
                      calendar_id: str, calendar_version: str,
                      provider_used: str | None = None) -> dict | None:
    """成功序列 → SeriesDescriptor；错误序列（rows=None）→ None。

    history_start/end 取该次响应实际可证明边界（首批行/末批行日期），
    不把概览 latest_trade_date 当逐证券边界（DEVELOPMENT §3.3）。
    """
    if rows is None or not rows:
        return None
    dates = [r["date8"] if "date8" in r else str(r.get("date", ""))[:10].replace("-", "")
             for r in rows]
    dates = [d for d in dates if d]
    if not dates:
        return None
    exchange, asset_type, namespace, native = key.split("/")
    if kind == "index":
        meta = INDEX_SERIES[key]
        access = provider_used or "tencent"
        series_prefix = {"tencent": "tencent-ifzq-index-daily",
                         "sina": "sina-kline-index-daily"}.get(
                             access, f"{access}-index-daily")
        return {
            "instrument_id": key,
            "series_id": f"{series_prefix}:{meta['symbol']}",
            "provider": meta["compiler"],
            "methodology_id": f"{meta['compiler']}-index-{native}",
            "methodology_version": None,  # 编制方法版本不可取得（限制如实声明）
            "history_start": _iso(min(dates)), "history_end": _iso(max(dates)),
            "calendar_exchange": meta["calendar_exchange"],
            "calendar_id": calendar_id, "calendar_version": calendar_version,
            "native_volume_unit": None,
            "volume_multiplier_to_wire": None,
            "origin_reference": meta["identity_evidence"],
            "limitations": [
                "volume/amount null: source unit not verifiable (price-only)",
                "methodology_version unknown: index methodology doc not machine-readable",
            ],
        }
    if kind == "board":
        meta = BOARD_SERIES[key]
        return {
            "instrument_id": key,
            "series_id": f"eastmoney-push2his-board-daily:{meta['secid']}",
            "provider": "eastmoney",
            "methodology_id": f"eastmoney-board-{native}",
            "methodology_version": None,  # 板块编制规则版本不可取得
            "history_start": _iso(min(dates)), "history_end": _iso(max(dates)),
            "calendar_exchange": BOARD_CALENDAR_EXCHANGE,
            "calendar_id": calendar_id, "calendar_version": calendar_version,
            "native_volume_unit": None,
            "volume_multiplier_to_wire": None,
            "origin_reference": (f"https://quote.eastmoney.com/bk/{meta['secid']}.html"),
            "limitations": [
                "volume/amount null: source unit not verifiable (price-only)",
                "board rename/recompile history not separately versioned by provider",
            ],
        }
    if kind == "factor":
        return {
            "instrument_id": key,
            "series_id": f"engine-cum-adjust:{native}",
            # 事实性上游=free-stockdb 发行服务（引擎唯一数据来源）；更上游的
            # 提供者身份引擎不可见，已在 limitations 声明（不虚构交易所直连）
            "provider": "free-stockdb-upstream",
            "methodology_id": "affine-v1-from-engine-cum",
            "methodology_version": "engine-cum-v1",
            "history_start": _iso(min(dates)), "history_end": _iso(max(dates)),
            "calendar_exchange": exchange if exchange in ("XSHG", "XSHE") else None,
            "calendar_id": calendar_id, "calendar_version": calendar_version,
            "native_volume_unit": None,
            "volume_multiplier_to_wire": None,
            "origin_reference": ("urn:stockdb:engine:复权:<code>:<date> "
                                 "(cum field; conversion proven in "
                                 "/opt/stockdb/pybao/stock_sdk.py)"),
            "limitations": [
                "upstream-of-upstream provider identity not visible through "
                "the free-stockdb mirror dataset",
            ],
        }
    if kind == "equity":
        return {
            "instrument_id": key,
            "series_id": f"engine-daily-kline:{native}",
            "provider": "free-stockdb-upstream",
            "methodology_id": "sse-szse-exchange-daily-bar",
            "methodology_version": None,
            "history_start": _iso(min(dates)), "history_end": _iso(max(dates)),
            "calendar_exchange": exchange if exchange in ("XSHG", "XSHE") else None,
            "calendar_id": calendar_id, "calendar_version": calendar_version,
            "native_volume_unit": ("fund_units" if asset_type == "etf"
                                   else "shares"),
            "volume_multiplier_to_wire": "1",
            "origin_reference": ("urn:stockdb:engine:日k:<code>:<date> "
                                 "(unadjusted daily bars)"),
            "limitations": [
                "upstream-of-upstream provider identity not visible through "
                "the free-stockdb mirror dataset",
            ],
        }
    return None


def research_extensions(raw_snapshot_id: str | None,
                        catalog: dict[str, dict | None]) -> dict:
    return {"extensions": {
        "research_v1": {
            "extension_contract": EXTENSION_CONTRACT,
            "extension_revision": EXTENSION_REVISION,
            "raw_snapshot_id": raw_snapshot_id,
            "series_catalog": catalog,
        }}}


# ---------------------------------------------------------------------------
# 因子构造（B 批核心：cum → affine-v1 multiplier/offset）
# ---------------------------------------------------------------------------

FACTOR_ALGORITHM = "affine-v1"
FACTOR_METHODODOLOGY_ID = "affine-v1-from-engine-cum"
FACTOR_METHODODOLOGY_VERSION = "engine-cum-v1"
FACTOR_FORMULA_REFERENCE = (
    "/opt/stockdb/pybao/stock_sdk.py::_apply_fq_in_memory — "
    "qfq: adjusted = raw / (f_latest/f_current); hfq: adjusted = raw / (1/f_current); "
    "f = cum of latest ex-dividend event ≤ date (1.0 before first event). "
    "affine-v1 form: multiplier(date) = cum(≤date)/cum(≤anchor) > 0, offset = 0; "
    "engine row check: cum_n = cum_{n-1} × mult_n, mult = pre_close/(pre_close−div−…); "
    "sample: 复权:000001:20260924 div=0.249 mult=1.022 cum=137.903, "
    "日k:000001:20260923 close=11.60 → 11.60/(11.60−0.249)=1.02193≈1.022")


def factor_multiplier(events: list[tuple[str, dict]], date8: str,
                      anchor8: str) -> Decimal:
    """multiplier(date) = cum(≤date)/cum(≤anchor)，量化到 12 位小数并规范形式。

    锚点日 = 1（cum 比约分）；offset 恒 0（SDK 乘法语义，见 docstring 证据）。
    输出为 canonical 十进制（去尾零，如 1 / 0.952380952381）。
    """
    ratio = (cum_at(events, date8) / cum_at(events, anchor8))
    quantized = ratio.quantize(Decimal("1E-12"), rounding=ROUND_HALF_UP)
    return quantized.normalize()


def build_factor_rows(code: str, events: list[tuple[str, dict]],
                      raw_dates: list[str], anchor8: str, *,
                      source_id: str) -> list[dict]:
    """逐原始行情日一行：{date, multiplier, offset, source_id}（A03）。

    只在有真实原始 bar 的交易日给系数；原始缺行（停牌等合法 gap）不造行，
    不用 1/0 补空。
    """
    rows = []
    for date8 in sorted(raw_dates):
        rows.append({
            "date": _iso(date8),
            "multiplier": format(factor_multiplier(events, date8, anchor8), "f"),
            "offset": "0",
            "source_id": source_id,
        })
    return rows


# ---------------------------------------------------------------------------
# Decimal 仿射复算（§6 唯一验收公式）
# ---------------------------------------------------------------------------


def apply_affine(raw_value: str, multiplier: str, offset: str,
                 price_decimals: int) -> str:
    """adjusted = raw × multiplier + offset，ROUND_HALF_UP 到声明精度。

    合法复权值可 ≤0（§6：不套原始正价格闸门）；multiplier>0 保证 OHLC 次序。
    """
    value = (Decimal(raw_value) * Decimal(multiplier)
             + Decimal(offset)).quantize(Decimal(1).scaleb(-price_decimals),
                                         rounding=ROUND_HALF_UP)
    return format(value.normalize(), "f")


# ---------------------------------------------------------------------------
# B 批：get_adjust_factors v3（FactorRequest → FactorResponse）
# ---------------------------------------------------------------------------

FACTOR_SOURCE_ID = "engine-adjust-cum"
FACTORITY_IDENTITY_SOURCE_ID = "ident-engine-catalog"


def _factor_query(codes: list[str], start: str, end: str, fq: str,
                  anchor: str, snapshot_id: str | None) -> dict:
    """FactorQuery 正规化形态（schema 固定 6 字段；A18）。"""
    return {"codes": codes, "start": start, "end": end, "fq": fq,
            "adjustment_anchor": anchor, "snapshot_id": snapshot_id}


def validate_factor_request(args: dict) -> dict:
    """FactorRequest 校验（A19 负例全拒绝）；非法抛 V3Error。"""
    from interfaces.mcp import market_v3 as v3
    allowed = {"contract_version", "code", "codes", "start", "end", "fq",
               "adjustment_anchor", "snapshot_id"}
    unknown = set(args) - allowed
    if unknown:
        raise v3.V3Error("INVALID_ARGUMENT",
                         f"未知参数: {sorted(unknown)}（v3 因子请求不接受 "
                         "frequency/fields/limit/date_pattern）",
                         details={"unknown_parameters": sorted(unknown)})
    for field in ("code", "codes", "snapshot_id"):
        if field in args and args[field] is None:
            raise v3.V3Error("INVALID_ARGUMENT",
                             f"{field} 不允许显式传 null（缺省即省略该字段）",
                             details={"argument": field})
    has_code, has_codes = args.get("code") is not None, args.get("codes") is not None
    if has_code and has_codes:
        raise v3.V3Error("INVALID_ARGUMENT", "code 与 codes 不能同时提供")
    if not has_code and not has_codes:
        raise v3.V3Error("INVALID_ARGUMENT", "code 与 codes 必须提供其一")
    codes_raw = [args["code"]] if has_code else args["codes"]
    if not isinstance(codes_raw, list) or not codes_raw:
        raise v3.V3Error("INVALID_ARGUMENT", "codes 必须是非空数组",
                         details={"argument": "codes"})
    if len(codes_raw) > v3.MAX_CODES:
        raise v3.V3Error("INVALID_ARGUMENT",
                         f"codes 最多 {v3.MAX_CODES} 个（收到 {len(codes_raw)}）",
                         details={"received": len(codes_raw)})
    seen: set[str] = set()
    codes: list[str] = []
    for item in codes_raw:
        key = item if isinstance(item, str) else repr(item)
        if key in seen:
            raise v3.V3Error("INVALID_ARGUMENT", f"codes 存在重复项: {key}",
                             details={"duplicate": key})
        seen.add(key)
        parts = v3.parse_instrument_id(item, argument="code" if has_code else "codes")
        codes.append("/".join(parts))
    start = v3._date8(args.get("start"), "start")
    end = v3._date8(args.get("end"), "end")
    if start > end:
        raise v3.V3Error("INVALID_ARGUMENT", "start 不能大于 end",
                         details={"start": start, "end": end})
    fq = args.get("fq")
    if fq not in ("qfq", "hfq"):
        raise v3.V3Error("INVALID_ARGUMENT",
                         f"因子请求 fq 只接受 qfq/hfq（收到 {fq!r}，none 不是复权因子）",
                         details={"argument": "fq", "received": repr(fq)})
    anchor = v3._date8(args.get("adjustment_anchor"), "adjustment_anchor")
    return _factor_query(codes, start, end, fq, anchor,
                         args.get("snapshot_id"))


def _anchor_completed_check(anchor: str, latest: str | None) -> None:
    """锚点必须是已完成交易日（不得默认今天；未完成锚点拒绝，A19）。"""
    from interfaces.mcp import market_v3 as v3
    cal = v3._calendar()
    try:
        is_trading = cal.is_trading_day(anchor)
    except Exception:  # noqa: BLE001 - 日历异常按未知处理
        is_trading = None
    if is_trading is False:
        raise v3.V3Error("INVALID_ARGUMENT",
                         f"锚点 {anchor} 不是交易日（必须是已完成交易日）",
                         details={"argument": "adjustment_anchor",
                                  "received": anchor})
    if latest is not None and anchor > latest:
        raise v3.V3Error("INVALID_ARGUMENT",
                         f"锚点 {anchor} 晚于引擎最新已完成交易日 {latest}"
                         "（未完成交易日不能作锚点，也不默认今天）",
                         details={"argument": "adjustment_anchor",
                                  "anchor": anchor, "latest_completed": latest})


def _derivation(source_catalog_ref: str) -> dict:
    return {
        "methodology_id": FACTOR_METHODODOLOGY_ID,
        "methodology_version": FACTOR_METHODODOLOGY_VERSION,
        "source_id": FACTOR_SOURCE_ID,
        "formula_reference": source_catalog_ref,
    }


def get_adjust_factors_v3(args: dict) -> dict:
    """get_adjust_factors 的 v3 分支（B 批）。"""
    from interfaces.mcp import market_v3 as v3
    try:
        query = validate_factor_request(args)
    except v3.V3Error as exc:
        return v3.tool_failure([exc])
    request_hash = v3.digest(query)
    codes: list[str] = query["codes"]

    # ---- 请求级资产资格（扩展校验器 factor_request：仅 XSHG/XSHE equity/etf
    #      exchange 可进入因子查询；指数/板块拒绝 UNSUPPORTED_ADJUSTMENT）----
    for key in codes:
        _exchange, asset_type, _namespace, _native = key.split("/")
        if asset_type in ("index", "board"):
            return v3.tool_failure([v3.V3Error(
                "UNSUPPORTED_ADJUSTMENT",
                f"{key}：指数/板块保持自身点位口径，没有股票除权因子"
                "（不能套用成分股因子）",
                instrument_id=key,
                details={"asset_type": asset_type})])
        if (_exchange not in ("XSHG", "XSHE")
                or asset_type not in ("equity", "etf")
                or _namespace != "exchange"):
            return v3.tool_failure([v3.V3Error(
                "UNSUPPORTED_ASSET",
                f"{key}：复权因子能力仅覆盖 XSHG/XSHE × equity/etf × exchange",
                instrument_id=key,
                details={"exchange": _exchange, "asset_type": asset_type,
                         "namespace": _namespace})])

    # ---- 因子快照重读（保留原锚点，即使锚点不在子区间内，A05/A12）----
    if query["snapshot_id"] is not None:
        return _factor_from_snapshot(query, request_hash)

    latest = v3.HEALTH.get().get("latest") or v3.engine_latest_trade_date()
    try:
        _anchor_completed_check(query["adjustment_anchor"], latest)
    except v3.V3Error as exc:
        return v3.tool_failure([exc])

    acquired = v3._kline_slots.acquire(timeout=v3._KLINE_SLOT_WAIT_SECONDS)
    if not acquired:
        return v3.tool_failure([v3.V3Error(
            "BUSY", "行情并发已满，请稍后重试",
            details={"retry_after_ms": int(v3._KLINE_SLOT_WAIT_SECONDS * 1000)})]
        )
    try:
        return _factor_fetch_and_build(query, request_hash, latest)
    finally:
        v3._kline_slots.release()


def _factor_snapshot_scope_error(snapshot_id: str, message: str,
                                 **details) -> dict:
    from interfaces.mcp import market_v3 as v3
    return v3.tool_failure([v3.V3Error(
        "SNAPSHOT_SCOPE_MISMATCH", message,
        details={"snapshot_id": snapshot_id, **details})])


def _factor_from_snapshot(query: dict, request_hash: str) -> dict:
    """F 快照重读：子区间/子集合可读，fq 与锚点必须与捕获一致（不重置锚点）。"""
    from interfaces.mcp import market_v3 as v3
    snap, err = v3._snapshot_or_error(query["snapshot_id"])
    if snap is None:
        return v3.tool_failure([err])
    payload = snap["payload"]
    if payload.get("kind") != "factor":
        # 行情快照（R/A）传入因子工具 → 明确拒绝（A12 互传负例）
        return v3.tool_failure([v3.V3Error(
            "SNAPSHOT_SCOPE_MISMATCH",
            f"快照 {query['snapshot_id']} 不是因子快照"
            f"（kind={payload.get('kind')!r}；行情快照不能作为 factor snapshot 重读）",
            details={"snapshot_id": query["snapshot_id"],
                     "kind": payload.get("kind")})])
    captured = payload["query"]
    if (query["fq"] != captured["fq"]
            or query["adjustment_anchor"] != captured["adjustment_anchor"]):
        return _factor_snapshot_scope_error(
            query["snapshot_id"],
            "因子快照重读必须保持原 fq 与锚点（子区间重读不重置锚点）",
            captured_fq=captured["fq"], captured_anchor=captured["adjustment_anchor"])
    outside = [c for c in query["codes"] if c not in captured["codes"]]
    if outside:
        return _factor_snapshot_scope_error(
            query["snapshot_id"],
            f"证券 {outside} 不在因子快照捕获范围内", outside=outside)
    if (query["start"] < captured["start"] or query["end"] > captured["end"]):
        return _factor_snapshot_scope_error(
            query["snapshot_id"],
            "请求区间超出因子快照捕获范围（不能实时补新日期）",
            captured_start=captured["start"], captured_end=captured["end"])
    s_n, e_n = int(query["start"]), int(query["end"])
    data: dict[str, list[dict]] = {}
    for key in query["codes"]:
        data[key] = [dict(r) for r in payload["rows"].get(key, [])
                     if s_n <= int(r["date"].replace("-", "")) <= e_n]
    records = {}
    for key in query["codes"]:
        err = payload.get("errors", {}).get(key)
        slots = v3._expected_slots("XSHG", query["start"], query["end"])
        records[key] = {"rows": data[key], "slots": slots, "error": err}
    return _factor_build_response(query, request_hash, records,
                                  payload["sources"], query["snapshot_id"],
                                  snap["expires_at"], payload.get("catalog", {}))


def _factor_fetch_and_build(query: dict, request_hash: str,
                            latest: str | None) -> dict:
    from interfaces.mcp import market_v3 as v3
    catalog, catalog_retrieved = v3.catalog_codes()
    deadline = time.monotonic() + v3.REQUEST_TIMEOUT_MS / 1000.0
    records: dict[str, dict] = {}
    for key in query["codes"]:
        _exchange, _asset_type, _namespace, native = key.split("/")
        try:
            v3.resolve_identity(key, catalog=catalog)
        except v3.V3Error as exc:
            records[key] = {"rows": [], "expected": None,
                            "error": exc.to_error_object()}
            continue
        if time.monotonic() > deadline - 0.5:
            records[key] = {"rows": [], "expected": None, "error": v3.V3Error(
                "SOURCE_TIMEOUT", "单请求期限耗尽，该证券因子未读取",
                instrument_id=key,
                details={"deadline_ms": v3.REQUEST_TIMEOUT_MS}).to_error_object()}
            continue
        try:
            events = engine_factor_events(native)
            raw = v3.engine_daily_rows(
                native, query["start"], query["end"],
                timeout=min(15.0, max(1.0, deadline - time.monotonic())))
            raw_dates = [str(r.get("date"))[:8] for r in raw]
            rows = build_factor_rows(native, events, raw_dates,
                                     query["adjustment_anchor"],
                                     source_id=FACTOR_SOURCE_ID) if raw_dates else []
            slots = v3._expected_slots("XSHG", query["start"], query["end"])
            records[key] = {"rows": rows, "slots": slots, "error": None}
        except (ResearchError, v3.EngineFailure) as exc:
            records[key] = {"rows": [], "slots": None, "error": v3.V3Error(
                getattr(exc, "code", "SOURCE_UNAVAILABLE"), exc.message,
                instrument_id=key).to_error_object()}
    sources = {
        FACTOR_SOURCE_ID: v3._source_record(
            retrieved_at=_now_iso(),
            reference=("urn:stockdb:engine:复权:<code>:<year>* (cum/div/give/"
                       "trans/mult per ex-dividend date); affine-v1 conversion "
                       "proven in /opt/stockdb/pybao/stock_sdk.py::"
                       "_apply_fq_in_memory"),
            finality=("engine 复权 table populated by post-close sync; factors "
                      "are event-driven and stable once captured")),
        v3.IDENTITY_SOURCE_ID: v3._identity_source(catalog_retrieved),
    }
    cal_id, cal_ver = v3.CALENDAR_ID, v3._calendar().XSHG_HOLIDAYS_THROUGH
    catalog_desc: dict[str, dict | None] = {}
    for key in query["codes"]:
        rows = records[key]["rows"]
        catalog_desc[key] = (series_descriptor(key, kind="factor", rows=rows,
                                               calendar_id=cal_id,
                                               calendar_version=cal_ver)
                             if rows else None)
    snapshot_id = v3.SNAPSHOTS.new_id("factor")
    v3.SNAPSHOTS.capture(snapshot_id, {
        "kind": "factor", "query": dict(query),
        "rows": {k: [dict(r) for r in rec["rows"]] for k, rec in records.items()},
        "errors": {k: rec["error"] for k, rec in records.items()
                   if rec["error"] is not None},
        "sources": sources, "catalog": catalog_desc,
    }, row_count=sum(len(rec["rows"]) for rec in records.values()))
    snap = v3.SNAPSHOTS.get(snapshot_id)
    return _factor_build_response(query, request_hash, records, sources,
                                  snapshot_id, snap["expires_at"], catalog_desc)


def _factor_build_response(query: dict, request_hash: str,
                           records: dict[str, dict], sources: dict,
                           snapshot_id: str, expires_at: float,
                           catalog_desc: dict[str, dict | None]) -> dict:
    """FactorResponse 装配（query/data/meta 恰好覆盖 codes；A02）。"""
    from interfaces.mcp import market_v3 as v3
    data: dict[str, list[dict]] = {}
    meta: dict[str, dict] = {}
    errors: list[dict] = []
    for key in query["codes"]:
        rec = records.get(key) or {"rows": [], "slots": None, "error": None}
        rows, error = rec["rows"], rec.get("error")
        slots = rec.get("slots")
        exchange, asset_type, namespace, native = key.split("/")
        if error is not None:
            errors.append(error)
        identity_error = bool(error) and error.get("code") in (
            "UNKNOWN_INSTRUMENT", "UNSUPPORTED_ASSET", "AMBIGUOUS_INSTRUMENT")
        row_dates = {row["date"] for row in rows}
        if slots is None:
            expected, gaps = None, []
            coverage_state = "unknown"
        else:
            expected = len(slots)
            gaps = [{"date": d, "time": None, "reason": "UNKNOWN",
                     "evidence": []} for d in slots if d not in row_dates]
            coverage_state = "complete" if not gaps else "incomplete"
        # 非完整覆盖必须有机器错误（与 kline 同原则；因子工具无截断概念）
        if error is None and coverage_state != "complete":
            if slots is None:
                errors.append(v3.V3Error(
                    "INCOMPLETE_COVERAGE",
                    f"{key} 请求区间超出已核实交易日历范围，预期槽位未知",
                    instrument_id=key,
                    details={"reason": "calendar_coverage_unknown"}
                ).to_error_object())
            else:
                errors.append(v3.V3Error(
                    "INCOMPLETE_COVERAGE",
                    f"{key} 范围内有 {len(gaps)} 个无因子行且无证据的槽位"
                    "（不默认 1/0 补空；停牌等合法空需证据）",
                    instrument_id=key,
                    details={"dates": [g["date"] for g in gaps][:50]}
                ).to_error_object())
        if coverage_state == "complete":
            status = "ok" if rows else "empty"
        else:
            status = "partial" if rows else "error"
        meta[key] = {
            "instrument": {
                "instrument_id": key, "code": native,
                "exchange": exchange, "asset_type": asset_type,
                "namespace": namespace, "name": None,
                "identity_verified": not identity_error,
                "identity_source_id": (None if identity_error
                                       else FACTORITY_IDENTITY_SOURCE_ID),
            },
            "status": status,
            "adjustment": ({"mode": query["fq"],
                            "anchor_date": query["adjustment_anchor"],
                            "factor_snapshot_id": snapshot_id,
                            "algorithm": FACTOR_ALGORITHM}
                           if rows or not error else None),
            "coverage": {"state": coverage_state,
                         "expected_rows": expected,
                         "available_rows": (len(rows) if slots is not None
                                            else (len(rows) or None)),
                         "returned_rows": len(rows), "gaps": gaps},
            "truncated": False, "truncation_reason": None,
            "derivation": (_derivation(sources[FACTOR_SOURCE_ID]["reference"])
                           if rows else None),
        }
        data[key] = rows
    all_complete = bool(meta) and all(
        m["coverage"]["state"] == "complete" for m in meta.values())
    usable = (any(data.values())
              or any(m["coverage"]["state"] == "complete"
                     and m["status"] == "empty" for m in meta.values()))
    top_status = ("complete" if all_complete and not errors
                  else "partial" if usable else "failed")
    header = v3._base_header(
        top_status, errors=errors, snapshot_id=snapshot_id,
        snapshot_expires_at=(datetime.fromtimestamp(expires_at).astimezone()
                             .isoformat(timespec="seconds")) if expires_at else None,
        data_hash=v3.digest(data))
    header.update({"query": query, "request_hash": request_hash,
                   "data": data, "meta": meta, "source_catalog": sources})
    header.update(research_extensions(None, catalog_desc))
    return header


# ---------------------------------------------------------------------------
# B 批：get_kline fq=qfq/hfq（R/F/A 链；§6 调用顺序）
# ---------------------------------------------------------------------------


def adjusted_kline(query: dict, request_hash: str) -> dict:
    """fq≠none 的 get_kline：读 F → 抓 R → 计算 A → 双快照 + extensions。"""
    from interfaces.mcp import market_v3 as v3

    # ---- 指数/板块没有股票除权因子：在因子快照查找前明确拒绝（I10/A19）----
    for key in query["codes"]:
        if key.split("/")[1] in ("index", "board"):
            return v3.tool_failure([v3.V3Error(
                "UNSUPPORTED_ADJUSTMENT",
                f"{key}：指数/板块保持自身点位口径，fq 仅支持 none",
                instrument_id=key,
                details={"asset_type": key.split("/")[1]})])

    # ---- A 快照重读：只读原捕获，不访问实时源换值（§6 步骤 5）----
    if query["snapshot_id"] is not None:
        return _adjusted_from_snapshot(query, request_hash)

    factor_id = query["factor_snapshot_id"]
    snap, err = v3._snapshot_or_error(factor_id)
    if snap is None:
        return v3.tool_failure([err])
    payload = snap["payload"]
    if payload.get("kind") != "factor":
        return v3.tool_failure([v3.V3Error(
            "SNAPSHOT_SCOPE_MISMATCH",
            f"快照 {factor_id} 不是因子快照（kind={payload.get('kind')!r}）；"
            "行情快照 R/A 不能作为 factor_snapshot_id 传入（A12）",
            details={"snapshot_id": factor_id,
                     "kind": payload.get("kind")})])
    captured = payload["query"]
    scope_errors: list[v3.V3Error] = []
    outside = [c for c in query["codes"] if c not in captured["codes"]]
    if outside:
        scope_errors.append(v3.V3Error(
            "SNAPSHOT_SCOPE_MISMATCH",
            f"证券 {outside} 不在因子快照 F={factor_id} 捕获范围内",
            instrument_id=outside[0], details={"outside": outside}))
    if (query["start"] < captured["start"] or query["end"] > captured["end"]):
        scope_errors.append(v3.V3Error(
            "SNAPSHOT_SCOPE_MISMATCH",
            f"请求区间 [{query['start']},{query['end']}] 超出因子快照捕获范围 "
            f"[{captured['start']},{captured['end']}]（不能扩张范围）",
            details={"factor_snapshot_id": factor_id}))
    if query["fq"] != captured["fq"]:
        scope_errors.append(v3.V3Error(
            "SNAPSHOT_SCOPE_MISMATCH",
            f"fq={query['fq']} 与因子快照捕获的 fq={captured['fq']} 不一致",
            details={"factor_snapshot_id": factor_id}))
    if query["adjustment_anchor"] != captured["adjustment_anchor"]:
        scope_errors.append(v3.V3Error(
            "SNAPSHOT_SCOPE_MISMATCH",
            f"锚点 {query['adjustment_anchor']} 与因子快照捕获锚点 "
            f"{captured['adjustment_anchor']} 不一致",
            details={"factor_snapshot_id": factor_id}))
    if scope_errors:
        return v3.tool_failure(scope_errors)

    acquired = v3._kline_slots.acquire(timeout=v3._KLINE_SLOT_WAIT_SECONDS)
    if not acquired:
        return v3.tool_failure([v3.V3Error(
            "BUSY", "行情并发已满，请稍后重试",
            details={"retry_after_ms": int(v3._KLINE_SLOT_WAIT_SECONDS * 1000)})])
    try:
        return _adjusted_fetch_and_build(query, request_hash, payload, snap)
    finally:
        v3._kline_slots.release()


def _adjusted_fetch_and_build(query: dict, request_hash: str,
                              factor_payload: dict, factor_snap: dict) -> dict:
    from interfaces.mcp import market_v3 as v3
    catalog, catalog_retrieved = v3.catalog_codes()
    deadline = time.monotonic() + v3.REQUEST_TIMEOUT_MS / 1000.0
    rows_by_code: dict[str, list[dict]] = {}
    errors_by_code: dict[str, v3.V3Error] = {}
    for key in query["codes"]:
        try:
            v3.resolve_identity(key, catalog=catalog)
        except v3.V3Error as exc:
            errors_by_code[key] = exc
            continue
        if time.monotonic() > deadline - 0.5:
            errors_by_code[key] = v3.V3Error(
                "SOURCE_TIMEOUT", "单请求期限耗尽，该证券原始行情未读取",
                instrument_id=key,
                details={"deadline_ms": v3.REQUEST_TIMEOUT_MS})
            continue
        try:
            rows_by_code[key] = v3.engine_daily_rows(
                key.split("/")[-1], query["start"], query["end"],
                timeout=min(15.0, max(1.0, deadline - time.monotonic())))
        except v3.EngineFailure as exc:
            errors_by_code[key] = v3.V3Error(exc.code, exc.message,
                                             instrument_id=key)
    total_rows = sum(len(r) for r in rows_by_code.values())
    if total_rows > v3.MAX_RESPONSE_ROWS:
        return v3._kline_result_too_large(query, request_hash, total_rows)

    health = v3.HEALTH.get()
    latest = health.get("latest") or v3.engine_latest_trade_date()
    bar_states = {}
    for key, rows in rows_by_code.items():
        if rows:
            bar_states[key] = v3._bar_state_for(
                max(str(r.get("date"))[:8] for r in rows), latest)

    # ---- 价格序列目录（R/A 共用同一描述；E01：series_catalog 描述价格序列，
    # 复权语义由 meta.adjustment 表达；F 保持自己的因子序列描述）----
    cal_id, cal_ver = v3.CALENDAR_ID, v3._calendar().XSHG_HOLIDAYS_THROUGH
    price_catalog: dict[str, dict | None] = {}
    for key in query["codes"]:
        rows = rows_by_code.get(key) or []
        price_catalog[key] = (series_descriptor(
            key, kind="equity",
            rows=[{"date8": str(r.get("date"))[:8]} for r in rows],
            calendar_id=cal_id, calendar_version=cal_ver) if rows else None)

    # ---- R：原始六字段行情快照（不可变；可经 fq=none + snapshot_id 重读）----
    raw_sources = {
        v3.IDENTITY_SOURCE_ID: v3._identity_source(catalog_retrieved),
        v3.SEGMENT_SOURCE_ID: v3._segment_source(catalog_retrieved),
        v3.BARS_SOURCE_ID: v3._source_record(
            retrieved_at=_now_iso(),
            reference=("urn:stockdb:engine:日k:<code>:<date-prefix> (engine daily "
                       "kline tables over HTTP, unadjusted; captured as raw R for "
                       f"factor snapshot {query['factor_snapshot_id']})"),
            finality=v3._bars_finality_evidence()),
    }
    raw_id = v3.SNAPSHOTS.new_id("kline")
    raw_query = dict(query)
    raw_query.update({"fq": "none", "snapshot_id": None,
                      "adjustment_anchor": None, "factor_snapshot_id": None})
    v3.SNAPSHOTS.capture(raw_id, {
        "kind": "kline", "query": raw_query,
        "rows": {k: list(v) for k, v in rows_by_code.items()},
        "sources": {k: dict(v) for k, v in raw_sources.items()},
        "identities": {k: v3._identity_dict(v3.identity_from_key(k, verified=True))
                       for k in rows_by_code},
        "bar_states": dict(bar_states), "latest": latest,
        "catalog": dict(price_catalog),
    }, row_count=total_rows)

    # ---- A：按 F 计算（Decimal 仿射；量额保持 R 原值不改写）----
    anchor = query["adjustment_anchor"]
    adjusted_rows: dict[str, list[dict]] = {}
    for key, rows in rows_by_code.items():
        identity = v3.identity_from_key(key)
        events = engine_factor_events(key.split("/")[-1])
        decimals = v3._price_decimals(identity.asset_type)
        factor_by_date = {r["date"]: (r["multiplier"], r["offset"])
                          for r in factor_payload["rows"].get(key, [])}
        out: list[dict] = []
        failed = False
        for row in rows:
            iso_date = _iso(str(row.get("date"))[:8])
            factor = factor_by_date.get(iso_date)
            if factor is None:
                errors_by_code[key] = v3.V3Error(
                    "INCOMPLETE_COVERAGE",
                    f"{key} {iso_date} 有原始行情但因子快照缺该日系数"
                    "（不默认 1/0 补空，A03）",
                    instrument_id=key, details={"date": iso_date})
                failed = True
                break
            multiplier, offset = factor
            try:
                bar = {"date": iso_date,
                       "bar_state": bar_states.get(key, "unknown"),
                       "source_id": v3.BARS_SOURCE_ID}
                for field in ("open", "high", "low", "close"):
                    bar[field] = apply_affine(v3.decimal_str(row[field]),
                                              multiplier, offset, decimals)
                bar["volume"] = row["volume"]
                bar["amount"] = v3.decimal_str(row["amount"])
                out.append(bar)
            except (ValueError, ArithmeticError) as exc:
                errors_by_code[key] = v3.V3Error(
                    "SOURCE_SCHEMA_ERROR",
                    f"{key} {iso_date} 复权计算失败: {exc}",
                    instrument_id=key)
                failed = True
                break
        if failed:
            adjusted_rows[key] = []
            continue
        # 合法复权值可 ≤0（§6）；OHLC 次序由 multiplier>0 保证，不复检正负
        adjusted_rows[key] = out

    a_id = v3.SNAPSHOTS.new_id("kline")
    v3.SNAPSHOTS.capture(a_id, {
        "kind": "kline-adjusted", "query": dict(query),
        "rows": {k: list(v) for k, v in adjusted_rows.items()},
        "sources": {k: dict(v) for k, v in raw_sources.items()},
        "identities": {k: v3._identity_dict(v3.identity_from_key(k, verified=True))
                       for k in adjusted_rows},
        "bar_states": dict(bar_states), "latest": latest,
        "raw_snapshot_id": raw_id,
        "factor_snapshot_id": query["factor_snapshot_id"],
        "adjustment_anchor": anchor, "fq": query["fq"],
        "catalog": dict(price_catalog),
    }, row_count=sum(len(r) for r in adjusted_rows.values()))

    expected_by_code = {key: v3._expected_slots(key.split("/")[0],
                                                 query["start"], query["end"])
                        for key in query["codes"]}
    envelope = v3._kline_build_response(
        query, request_hash, query["codes"],
        {k: v3.identity_from_key(k, verified=k not in errors_by_code)
         for k in query["codes"]},
        errors_by_code, expected_by_code, raw_sources, a_id,
        adjusted_rows, bar_states, prebuilt_bars=True)
    # meta.adjustment 换复复权语义（_kline_build_response 默认 none）
    for key, m in envelope["meta"].items():
        if key in adjusted_rows and key not in errors_by_code:
            m["adjustment"] = {"mode": query["fq"], "anchor_date": anchor,
                               "factor_snapshot_id": query["factor_snapshot_id"],
                               "algorithm": FACTOR_ALGORITHM}
    # E01：A 的 series_catalog = 与 R 相同的价格序列描述（不是因子序列）
    envelope.update(research_extensions(raw_id, dict(price_catalog)))
    return envelope


def _adjusted_from_snapshot(query: dict, request_hash: str) -> dict:
    """A 快照重读：携带相同 fq/anchor/F；只读原捕获（含 R 引用）。"""
    from interfaces.mcp import market_v3 as v3
    snap, err = v3._snapshot_or_error(query["snapshot_id"])
    if snap is None:
        return v3.tool_failure([err])
    payload = snap["payload"]
    if payload.get("kind") not in ("kline", "kline-adjusted"):
        return v3.tool_failure([v3.V3Error(
            "SNAPSHOT_SCOPE_MISMATCH",
            f"快照 {query['snapshot_id']} 不是行情快照"
            f"（kind={payload.get('kind')!r}；因子快照 F 不能作为行情 snapshot 重读）",
            details={"snapshot_id": query["snapshot_id"],
                     "kind": payload.get("kind")})])
    if payload.get("kind") == "kline-adjusted":
        captured = payload["query"]
        if (query["fq"] != captured["fq"]
                or query["adjustment_anchor"] != captured["adjustment_anchor"]
                or query["factor_snapshot_id"] != captured["factor_snapshot_id"]):
            return v3.tool_failure([v3.V3Error(
                "SNAPSHOT_SCOPE_MISMATCH",
                "复权行情快照重读必须携带相同 fq/锚点/因子快照（只读原捕获）",
                details={"snapshot_id": query["snapshot_id"]})])
    elif query["fq"] != "none":
        # 原始 R 的 fq=none 快照不能改 fq 冒充复权重放（A12）
        return v3.tool_failure([v3.V3Error(
            "SNAPSHOT_SCOPE_MISMATCH",
            "fq=none 原始快照不能更换 fq 当复权行情重放（A12）",
            details={"snapshot_id": query["snapshot_id"],
                     "snapshot_fq": payload["query"]["fq"],
                     "requested_fq": query["fq"]})])
    envelope = v3._kline_from_snapshot(query, request_hash)
    if envelope.get("status") == "failed":
        return envelope
    raw_id = payload.get("raw_snapshot_id")
    # E01：A 重读沿用捕获时的价格序列目录（与 R 一致，不由当前行重造）
    merged = dict(envelope.get("extensions") or {})
    merged.update(research_extensions(raw_id, payload.get(
        "catalog") or {})["extensions"])
    envelope["extensions"] = merged
    # R3 修补（消费方 adjusted-replay 反馈）：_kline_from_snapshot 对全部
    # 证券填默认 adjustment=none；重放路径此前遗漏恢复复权语义（首次计算
    # 路径有对应覆盖）。此处用**捕获时**的 mode/anchor/F/algorithm 恢复
    # 每个有复权行的证券的 meta.adjustment——语义来自快照 payload，不按
    # 请求"猜补"；R/普通 none 快照不经本函数，核心未复权分支不受污染。
    # 错误/空行证券与首次响应一致保持 none（首次路径同样只在成功行上覆盖）。
    replay_adjustment = {
        "mode": payload["fq"],
        "anchor_date": payload["adjustment_anchor"],
        "factor_snapshot_id": payload["factor_snapshot_id"],
        "algorithm": FACTOR_ALGORITHM}
    for key, m in envelope.get("meta", {}).items():
        if envelope.get("data", {}).get(key):
            m["adjustment"] = dict(replay_adjustment)
    return envelope


# ---------------------------------------------------------------------------
# A 批：指数/板块日K读取与身份路由
# ---------------------------------------------------------------------------


def resolve_research_identity(key: str) -> tuple[str, str, str, str]:
    """index/board 规范键路由。成功返回 (kind, native, name, calendar_exchange)。

    错误语义（I10）：
      - namespace 不在已声明集合（ths/sw/ti…）→ UNSUPPORTED_ASSET；
      - namespace 内未登记的具体序列（其他指数/板块代码）→ UNKNOWN_INSTRUMENT。
    """
    from interfaces.mcp import market_v3 as v3
    exchange, asset_type, namespace, native = v3.parse_instrument_id(key)
    if asset_type == "index":
        if namespace not in INDEX_NAMESPACES:
            raise v3.V3Error(
                "UNSUPPORTED_ASSET",
                f"指数命名空间 {namespace!r} 不在已声明能力内（本批："
                f"{sorted(INDEX_NAMESPACES)}；ths/sw/ti 序列不经本服务提供）",
                instrument_id=key, details={"namespace": namespace})
        if key not in INDEX_SERIES:
            raise v3.V3Error(
                "UNKNOWN_INSTRUMENT",
                f"指数 {native}（{exchange}/{namespace}）不在已验收的指定指数目录内"
                "（上证指数/深证成指/创业板指/中证1000 之外未声明支持）",
                instrument_id=key,
                details={"reason": "not_in_verified_index_directory"})
        meta = INDEX_SERIES[key]
        return "index", meta["symbol"], meta["name"], meta["calendar_exchange"]
    if asset_type == "board":
        if exchange != "CN":
            raise v3.V3Error(
                "UNSUPPORTED_ASSET",
                f"板块键 exchange 必须是 CN（跨市场提供者板块），收到 {exchange}",
                instrument_id=key, details={"exchange": exchange})
        if namespace not in BOARD_NAMESPACES:
            raise v3.V3Error(
                "UNSUPPORTED_ASSET",
                f"板块命名空间 {namespace!r} 不在已声明能力内（本批："
                f"{sorted(BOARD_NAMESPACES)}；THS 同主题序列不冒充东财 BK，I04）",
                instrument_id=key, details={"namespace": namespace})
        if key not in BOARD_SERIES:
            raise v3.V3Error(
                "UNKNOWN_INSTRUMENT",
                f"东财板块 {native} 不在已验收的指定板块目录内（7 个 BK 之外"
                "未声明支持）",
                instrument_id=key,
                details={"reason": "not_in_verified_board_directory"})
        meta = BOARD_SERIES[key]
        return "board", meta["secid"], meta["name"], BOARD_CALENDAR_EXCHANGE
    raise v3.V3Error("UNSUPPORTED_ASSET",
                     f"research 路由只处理 index/board（收到 {asset_type}）",
                     instrument_id=key, details={"asset_type": asset_type})


def fetch_series_rows(key: str, start8: str, end8: str) -> tuple[
        str, str, list[dict], dict]:
    """已解析键 → (kind, provider, 日K行, fetch_meta)。失败抛 ResearchError。

    fetch_meta：板块路径附 {"cache_served", "retrieved_at"}（来源记录用）；
    指数路径为空 dict。
    """
    kind, native, _name, _cal = resolve_research_identity(key)
    if kind == "index":
        provider, rows = fetch_index_daily(native, start8, end8)
        return "index", provider, rows, {}
    rows, cache_info = board_daily_cached(key, native, start8, end8)
    _ensure_backfill_thread()
    return "board", "eastmoney", rows, cache_info


def series_units(kind: str) -> dict:
    """指数/板块 units（price-only：量额 null + 原因，I06）。"""
    decimals = 3 if kind == "index" else 2
    reason = (_UNITS_NULL_REASON_INDEX if kind == "index"
              else _UNITS_NULL_REASON_BOARD)
    return {"price": "points", "volume": "unknown", "amount": "unknown",
            "price_decimals": decimals, "amount_decimals": None,
            "null_reason": reason}


def research_series_bars(rows: list[dict], kind: str, key: str,
                         bar_state: str, source_id: str) -> list[dict]:
    """指数/板块原始行 → 合同 Bar 对象（OHLC points>0；量额 null）。"""
    from interfaces.mcp import market_v3 as v3
    decimals = 3 if kind == "index" else 2
    out = []
    for row in rows:
        iso_date = _iso(row["date8"])
        bar = {"date": iso_date, "bar_state": bar_state,
               "source_id": source_id,
               "volume": None, "amount": None}
        for field in ("open", "high", "low", "close"):
            text = v3.decimal_str(row[field])
            places = len(text.partition(".")[2])
            if places > decimals:
                raise v3.EngineFailure(
                    "SOURCE_SCHEMA_ERROR",
                    f"{key} {iso_date} {field} 小数位 {places} 超过声明精度 {decimals}")
            bar[field] = text
        from decimal import Decimal as _D
        op, hi, lo, cl = (_D(bar[f]) for f in ("open", "high", "low", "close"))
        if min(op, hi, lo, cl) <= 0:
            raise v3.EngineFailure(
                "SOURCE_SCHEMA_ERROR",
                f"{key} {iso_date} 指数/板块点位必须 > 0"
                f"（open={bar['open']} high={bar['high']} low={bar['low']}"
                f" close={bar['close']}）")
        if not (lo <= min(op, cl) and max(op, cl) <= hi):
            raise v3.EngineFailure(
                "SOURCE_SCHEMA_ERROR",
                f"{key} {iso_date} OHLC 次序非法")
        out.append(bar)
    return out


def _v3():
    """取 market_v3 模块实例（包限定导入与运行实例同源，见 server 导入注释）。"""
    from interfaces.mcp import market_v3 as v3
    return v3


def series_source_id(key: str) -> str:
    return f"series-{key.split('/')[-1]}-{key.split('/')[1]}"


def series_source_record(kind: str, key: str, retrieved_at: str,
                          provider: str | None = None,
                          fetch_meta: dict | None = None) -> dict:
    v3 = _v3()
    fetch_meta = fetch_meta or {}
    if kind == "index":
        meta = INDEX_SERIES[key]
        access = provider or "tencent"
        if access == "sina":
            reference = (f"{_SINA_URL}?symbol={meta['symbol']}&scale=240 "
                         f"(index OHLC; identity: "
                         f"{meta['identity_evidence']})")
            finality = ("sina publishes completed daily bars after close; "
                        "finality cross-checked against engine latest trade "
                        "date")
        else:
            reference = (f"https://web.ifzq.gtimg.cn/appstock/app/fqkline/get?"
                         f"param={meta['symbol']},day (index OHLC; identity: "
                         f"{meta['identity_evidence']})")
            finality = ("tencent ifzq publishes completed daily bars after "
                        "close; finality cross-checked against engine latest "
                        "trade date")
        return v3._source_record(
            retrieved_at=retrieved_at, reference=reference,
            finality=finality)
    meta = BOARD_SERIES[key]
    cache_served = bool(fetch_meta.get("cache_served"))
    reference = (f"https://quote.eastmoney.com/bk/{meta['secid']}.html "
                 f"(eastmoney board daily kline via push2his API)")
    finality = "eastmoney publishes completed board bars after close"
    if cache_served:
        # 来源可追溯：缓存服务的数据必须如实标注原始取得时间，不得伪标实时抓取
        reference += (f" [served from server-side persistent board cache; "
                      f"bars originally retrieved "
                      f"{fetch_meta.get('retrieved_at') or 'unknown'}; this "
                      f"attempt failed upstream and is honestly reported via "
                      f"gaps]")
    return v3._source_record(
        retrieved_at=retrieved_at,
        reference=reference,
        finality=finality)


# ---------------------------------------------------------------------------
# 状态：research 能力声明（ResearchStatusMetadata）
# ---------------------------------------------------------------------------


def research_status_metadata() -> dict:
    """get_data_status.extensions.research_v1（§2.2；known_limitations 如实）。"""
    limitations: list[str] = []
    em = em_board_availability()
    if em["ok_at"] <= 0 and em["fail_at"] > 0:
        limitations.append(
            "eastmoney board source currently unreachable (latest failure: "
            f"{em['fail_reason']}); BK series served as honest failures until "
            "the source recovers")
    cooldown_left = int(float(em.get("cooldown_until") or 0) - time.time())
    if cooldown_left > 0:
        limitations.append(
            "eastmoney risk-control backoff active: silent connection drops "
            f"observed, next upstream attempt in ~{cooldown_left}s "
            f"(streak={em.get('backoff_streak')}); cached bars are served "
            "honestly with gaps reported, no fabrication")
    bf = em.get("backfill") or {}
    if bf.get("last_summary"):
        limitations.append(
            "board persistent cache & gap backfill active (last cycle: "
            f"{bf.get('last_cycle_at') and datetime.fromtimestamp(bf['last_cycle_at']).astimezone().isoformat(timespec='seconds')})")
    limitations.append(
        "index/board volume/amount are null (unit not verifiable); "
        "price-only acceptance per DEVELOPMENT.md §3.3")
    limitations.append(
        "1000-session probes before 2024-01-01 are rejected OUT_OF_CALENDAR "
        "(verified calendar bounds 2024-01-01..2026-12-31), not silently "
        "truncated (H10 declaration)")
    return {
        "extension_contract": EXTENSION_CONTRACT,
        "extension_revision": EXTENSION_REVISION,
        "history_targets": {"index_sessions": 500, "board_sessions": 250,
                            "stock_etf_sessions": 500},
        "known_limitations": limitations,
    }


def index_board_capabilities(latest: str | None) -> list[dict]:
    """index_board_daily_v1 的 capability 声明（逐 namespace 逐资产）。"""
    exceptions = [
        "series directory is an explicit verified allowlist: 4 indices "
        "(exchange/csi namespaces) and 7 eastmoney BK boards; other series are "
        "refused UNKNOWN_INSTRUMENT rather than guessed from code prefixes",
        "volume/amount null with units.null_reason (unit unverifiable); "
        "price-only acceptance",
        "board series depend on eastmoney reachability; failures are honest "
        "SOURCE_UNAVAILABLE, never substituted with THS series",
    ]
    end = _iso(latest) if latest else None
    return [
        {"asset_type": "index", "exchange": "XSHG", "namespace": "exchange",
         "frequencies": ["1d"], "fq_modes": ["none"],
         "history_start": None, "history_end": end, "exceptions": exceptions},
        {"asset_type": "index", "exchange": "XSHE", "namespace": "exchange",
         "frequencies": ["1d"], "fq_modes": ["none"],
         "history_start": None, "history_end": end, "exceptions": exceptions},
        {"asset_type": "index", "exchange": "XSHG", "namespace": "csi",
         "frequencies": ["1d"], "fq_modes": ["none"],
         "history_start": None, "history_end": end, "exceptions": exceptions},
        {"asset_type": "board", "exchange": "CN", "namespace": "eastmoney",
         "frequencies": ["1d"], "fq_modes": ["none"],
         "history_start": None, "history_end": end, "exceptions": exceptions},
    ]


def adjusted_history_capabilities(latest: str | None) -> list[dict]:
    end = _iso(latest) if latest else None
    exceptions = [
        "factor algorithm affine-v1 with offset=0 derived from engine 复权 cum "
        "(conversion proven from /opt/stockdb/pybao/stock_sdk.py source)",
        "qfq anchors on request end date, hfq on request start date; any other "
        "completed in-range trading day is also accepted as explicit anchor",
        "adjusted prices may legally be <= 0 for synthetic factors; raw price "
        "positivity gate is not applied to adjusted series",
        "volume/amount are carried unchanged from the raw snapshot R",
    ]
    caps = []
    for exchange in ("XSHG", "XSHE"):
        for asset in ("equity", "etf"):
            caps.append({"asset_type": asset, "exchange": exchange,
                         "namespace": "exchange", "frequencies": ["1d"],
                         "fq_modes": ["none", "qfq", "hfq"],
                         "history_start": ("2000-01-04" if asset == "equity"
                                           else "2005-02-23"),
                         "history_end": end, "exceptions": exceptions})
    return caps


# ---------------------------------------------------------------------------
# tools/list 发布 schema：get_adjust_factors（扩展合同 $defs 闭包）
# ---------------------------------------------------------------------------

EXTENSION_SCHEMA_PATH = (Path(__file__).resolve().parent / "contract"
                         / "research-extension" / "contract.schema.json")
_DRAFT_SCHEMA = "https://json-schema.org/draft/2020-12/schema"


def _extension_defs_closure(*root_names: str) -> dict:
    """从扩展合同抽取多个根定义及其传递依赖 $defs（原样，不改一个字节）。"""
    schema = json.loads(EXTENSION_SCHEMA_PATH.read_text(encoding="utf-8"))
    defs = schema["$defs"]
    needed: set[str] = set()

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

    for name in root_names:
        if name not in defs:
            raise KeyError(f"extension def not found: {name}")
        needed.add(name)
        walk(defs[name])
    return {name: defs[name] for name in sorted(needed)}


_LEGACY_FACTOR_INPUT = {
    "type": "object",
    "description": "旧版分支：仅未携带 contract_version 的请求（{code, date_pattern}）",
    "not": {"required": ["contract_version"]},
    "properties": {
        "code": {"type": "string", "description": "6 位股票代码，如 600633"},
        "date_pattern": {"type": "string",
                         "description": "日期通配，如 2026* 或 *（默认 *）",
                         "default": "*"},
    },
    "required": ["code"],
}


def published_factor_schemas() -> dict:
    """get_adjust_factors 的 input/output schema（v3 扩展分支 + 旧版分支）。"""
    from interfaces.mcp import market_v3 as v3
    defs = _extension_defs_closure("FactorRequest", "FactorResponse",
                                   "ToolFailureResponse")
    return {
        "inputSchema": {
            "$schema": _DRAFT_SCHEMA,
            "type": "object",
            "anyOf": [{"$ref": "#/$defs/FactorRequest"}, _LEGACY_FACTOR_INPUT],
            "$defs": defs,
        },
        "outputSchema": {
            "$schema": _DRAFT_SCHEMA,
            "type": "object",
            "anyOf": [{"$ref": "#/$defs/FactorResponse"},
                      {"$ref": "#/$defs/ToolFailureResponse"},
                      {"$ref": "#/$defs/LegacyToolResult"}],
            "$defs": {**defs, "LegacyToolResult": v3._LEGACY_RESULT_DEF},
        },
    }

