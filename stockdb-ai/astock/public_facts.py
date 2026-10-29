#!/usr/bin/env python3
"""astock.public_facts — M2 公开事实域服务实现（stockdb-public-facts-v1/1.0.0）。

两个拟新增 MCP 工具（PUBLIC-FACTS.md）：
- get_public_records：announcements/news/investor_qa 三域，capture 固定快照
  后分页；记录 published_at/published_date/first_seen/time_precision 分离。
- get_public_artifact：服务已发出的 opaque artifact_id 分块读取（≤20MiB，
  chunk 默认/最大 262144 字节）。

取数实现复用上游生成模块（astock/generated/*，见 extraction manifest）；
本模块是 wrapper：归一化、时间语义、空值/完整性区分、snapshot/分页/artifact。
上游函数返回相对时间文本时按 parser_reference_at（本次原页取回时刻）冻结
换算，跨午夜/重放/重启不按新的 now 重算（sse_e_interaction 的 now 参数）。
"""

from __future__ import annotations

import base64
import hashlib
import json
import re
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

# ---------------------------------------------------------------------------
# 常量与错误
# ---------------------------------------------------------------------------

WIRE_VERSION = "stockdb-public-facts-v1/1.0.0"
LIMIT_MAX = 200
LIMIT_DEFAULT = 50
CHUNK_MAX = 262144
ARTIFACT_MAX = 20 * 1024 * 1024
ALLOWED_MIME = {"application/pdf", "text/plain", "text/html",
                "application/json"}

RETRYABLE = {"SOURCE_UNAVAILABLE", "SOURCE_TIMEOUT", "RATE_LIMITED", "BUSY"}

DATASET_PROVIDERS = {
    "announcements": {"cninfo": {"markets": {"XSHG", "XSHE", "XBSE"}}},
    "news": {"eastmoney": {"markets": None},   # 全市场快讯/个股
             },
    "investor_qa": {"sse_e": {"markets": {"XSHG"}},
                    "cninfo_irm": {"markets": {"XSHE"}}},
}

INSTRUMENT_RE = re.compile(
    r"^(XSHG|XSHE|XBSE)/equity/exchange/(\d{6})$")

_MARKET_OF_CODE = {"6": "XSHG", "9": "XSHG", "0": "XSHE", "3": "XSHE",
                   "2": "XSHE", "4": "XBSE", "8": "XBSE", "920": "XBSE"}


class PublicFactsError(Exception):
    def __init__(self, code: str, message: str, *, retryable: bool = False,
                 details: dict | None = None):
        super().__init__(message)
        self.code = code
        self.message = message
        self.retryable = retryable or code in RETRYABLE
        self.details = details or {}


# ---------------------------------------------------------------------------
# 生成模块加载（构建产物，运行时不解析 Markdown）
# ---------------------------------------------------------------------------

_GEN_DIR = Path(__file__).resolve().parent / "generated"
_gen_cache: dict[str, Any] = {}


def _gen_module(name: str):
    """加载已生成的上游提取模块；依赖缺失/损坏明确失败。"""
    if name in _gen_cache:
        return _gen_cache[name]
    path = _GEN_DIR / f"{name}.py"
    if not path.is_file():
        raise PublicFactsError("INTERNAL_ERROR",
                               f"generated module missing: {name}")
    import importlib.util
    spec = importlib.util.spec_from_file_location(f"astock.{name}", path)
    mod = importlib.util.module_from_spec(spec)
    try:
        spec.loader.exec_module(mod)
    except ModuleNotFoundError as exc:  # 依赖锁未安装
        raise PublicFactsError(
            "INTERNAL_ERROR",
            f"generated module {name} dependency missing: {exc}") from exc
    _gen_cache[name] = mod
    return mod


# ---------------------------------------------------------------------------
# 请求正规化（PUBLIC-FACTS §2）
# ---------------------------------------------------------------------------


def _date8(value: Any, field: str) -> str:
    if not isinstance(value, str) or not re.fullmatch(r"\d{8}", value):
        raise PublicFactsError(
            "INVALID_ARGUMENT", f"{field} 必须是 YYYYMMDD",
            details={"argument": field, "received": repr(value)[:60]})
    try:
        datetime.strptime(value, "%Y%m%d")
    except ValueError:
        raise PublicFactsError("INVALID_ARGUMENT", f"{field} 不是真实日期",
                               details={"argument": field,
                                        "received": value}) from None
    return value


def _iso(date8: str) -> str:
    return f"{date8[:4]}-{date8[4:6]}-{date8[6:8]}"


def normalize_request(args: dict) -> dict:
    allowed = {"contract_version", "dataset_id", "source_provider",
               "start_date", "end_date", "instrument_id", "limit", "cursor",
               "snapshot_id"}
    unknown = set(args) - allowed
    if unknown:
        raise PublicFactsError("INVALID_ARGUMENT",
                               f"未知字段: {sorted(unknown)}",
                               details={"unknown": sorted(unknown)})
    if "contract_version" not in args:
        raise PublicFactsError("INVALID_ARGUMENT", "contract_version 必填")
    if args.get("contract_version") != "stockdb-public-facts-v1":
        raise PublicFactsError(
            "UNSUPPORTED_CONTRACT",
            f"contract_version 必须为 stockdb-public-facts-v1"
            f"（收到 {args.get('contract_version')!r}）")
    dataset = args.get("dataset_id")
    if dataset is None:
        raise PublicFactsError("INVALID_ARGUMENT", "dataset_id 必填")
    if dataset not in DATASET_PROVIDERS:
        raise PublicFactsError(
            "UNSUPPORTED_SCOPE", f"未知 dataset_id: {dataset!r}",
            details={"supported": sorted(DATASET_PROVIDERS)})
    provider = args.get("source_provider")
    if provider is None:
        raise PublicFactsError("INVALID_ARGUMENT", "source_provider 必填")
    if provider not in DATASET_PROVIDERS[dataset]:
        raise PublicFactsError(
            "UNSUPPORTED_SCOPE",
            f"{dataset} 不支持 provider {provider!r}",
            details={"supported": sorted(DATASET_PROVIDERS[dataset])})
    start = _date8(args.get("start_date"), "start_date")
    end = _date8(args.get("end_date"), "end_date")
    if start > end:
        raise PublicFactsError("INVALID_ARGUMENT", "start_date > end_date")
    instrument = args.get("instrument_id")
    market = None
    if dataset in ("announcements", "investor_qa") and not instrument:
        raise PublicFactsError(
            "INVALID_ARGUMENT", f"{dataset} 必须指定 instrument_id")
    if instrument is not None:
        m = INSTRUMENT_RE.match(instrument)
        if not m:
            raise PublicFactsError(
                "INVALID_ARGUMENT",
                "instrument_id 必须为 XSHG|XSHE|XBSE/equity/exchange/六位代码",
                details={"received": instrument})
        market, code = m.group(1), m.group(2)
        allowed_markets = DATASET_PROVIDERS[dataset][provider]["markets"]
        if allowed_markets is not None and market not in allowed_markets:
            raise PublicFactsError(
                "UNSUPPORTED_SCOPE",
                f"{dataset}/{provider} 不覆盖 {market}（沪深不串源）",
                details={"market": market,
                         "covered": sorted(allowed_markets)})
    limit = args.get("limit", LIMIT_DEFAULT)
    if not isinstance(limit, int) or isinstance(limit, bool) \
            or not (1 <= limit <= LIMIT_MAX):
        raise PublicFactsError(
            "INVALID_ARGUMENT",
            f"limit 必须为 1–{LIMIT_MAX}（收到 {limit!r}）")
    cursor = args.get("cursor")
    snapshot_id = args.get("snapshot_id")
    if cursor is not None and not snapshot_id:
        raise PublicFactsError(
            "INVALID_ARGUMENT", "cursor 必须同时携带 snapshot_id")
    return {"dataset_id": dataset, "source_provider": provider,
            "start_date": start, "end_date": end,
            "instrument_id": instrument, "limit": limit,
            "cursor": cursor, "snapshot_id": snapshot_id}


def canonical_query(query: dict) -> dict:
    """标准化 query：全字段固定、无值 null、limit 为解析后默认。"""
    return {"contract_version": "stockdb-public-facts-v1",
            "dataset_id": query["dataset_id"],
            "source_provider": query["source_provider"],
            "start_date": query["start_date"], "end_date": query["end_date"],
            "instrument_id": query["instrument_id"], "limit": query["limit"],
            "cursor": None, "snapshot_id": query.get("snapshot_id")}


def digest(value: Any) -> str:
    return hashlib.sha256(json.dumps(
        value, ensure_ascii=False, sort_keys=True, separators=(",", ":"),
        allow_nan=False).encode("utf-8")).hexdigest()


# ---------------------------------------------------------------------------
# capture（三域 → 规范 record）
# ---------------------------------------------------------------------------


def _now_utc() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def _now_cn() -> datetime:
    import datetime as dt
    return datetime.now(dt.timezone(dt.timedelta(hours=8)))


def _first_seen(notebook: dict, key: str) -> str:
    """服务首次观察账本：同 key 首次时间永久保留（重放/重启不刷新）。"""
    path = _snapshot_dir() / "first-seen.json"
    try:
        ledger = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        ledger = {}
    if key not in ledger:
        ledger[key] = _now_utc()
        try:
            path.write_text(json.dumps(ledger, ensure_ascii=False),
                            encoding="utf-8")
        except OSError:
            pass
    return ledger[key]


def _snapshot_dir() -> Path:
    """快照/artifact/first-seen 目录：config.DATA_DIR 优先；不可写时回退
    env/临时目录（持久化尽力而为，与 market_v3 快照仓同一纪律）。"""
    import os
    import tempfile
    cached = _snapshot_dir_cached.get("path")
    if cached is not None:
        return cached
    candidates = []
    try:
        import config
        candidates.append(Path(config.DATA_DIR) / "public_facts")
    except Exception:  # noqa: BLE001
        pass
    if os.environ.get("STOCKDB_V3_SNAPSHOT_DIR"):
        candidates.append(Path(os.environ["STOCKDB_V3_SNAPSHOT_DIR"]))
    candidates.append(Path(tempfile.gettempdir()) / "stockdb_public_facts")
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
    raise RuntimeError("no writable public-facts snapshot directory")


_snapshot_dir_cached: dict[str, Path | None] = {"path": None}


def _parse_cn_time(text: str, ref: datetime | None, fmt: str) -> str | None:
    """绝对时间解析；失败 None（由 completeness/unknown 承接，不猜）。"""
    try:
        dt = datetime.strptime((text or "").strip(), fmt)
    except (ValueError, TypeError):
        return None
    return dt.strftime("%Y-%m-%dT%H:%M") + "+08:00"


def capture_announcements(query: dict) -> list[dict]:
    code = query["instrument_id"].split("/")[-1]
    mod = _gen_module("astock_gen_announcement")
    rows = mod.cninfo_announcements(code, page_size=200)
    start, end = query["start_date"], query["end_date"]
    out = []
    for r in rows:
        date8 = (r.get("date") or "").replace("-", "")
        if not (start <= date8 <= end):
            continue
        published_at = None
        time_precision = "date"
        if r.get("date"):
            published_date = _iso(date8)
        else:
            published_date = None
        out.append({
            "record_id": "cninfo:" + _stable_hash(
                r.get("url") or (code + (r.get("title") or ""))),
            "record_version": "cninfo-v1",
            "instrument_id": query["instrument_id"],
            "title": r.get("title") or None,
            "category": r.get("type") or None,
            "original_url": r.get("url") or None,
            "document_artifact_id": None,  # 原件按需取回（D11 单独验证）
            "published_at": published_at,
            "published_date": published_date,
            "time_precision": time_precision,
            "time_unknown_reason": (None if published_date
                                    else "cninfo ts 未解析出日期"),
            "source_updated_at": None,
        })
    return out


def capture_news(query: dict) -> list[dict]:
    mod = _gen_module("astock_gen_news")
    start, end = query["start_date"], query["end_date"]
    out = []
    if query["instrument_id"]:
        code = query["instrument_id"].split("/")[-1]
        rows = mod.eastmoney_stock_news(code, page_size=200)
    else:
        rows = mod.eastmoney_global_news(page_size=200)
    for r in rows:
        raw_time = (r.get("time") or r.get("showTime") or "").strip()
        published_at = None
        published_date = None
        precision = "unknown"
        unknown_reason = None
        m = re.match(r"(\d{4}-\d{2}-\d{2})", raw_time)
        m2 = re.match(r"(\d{4}-\d{2}-\d{2}) (\d{2}:\d{2})", raw_time)
        if m2:
            published_at = m2.group(1) + "T" + m2.group(2) + "+08:00"
            published_date = m2.group(1)
            precision = "timestamp"
        elif m:
            published_date = m.group(1)
            precision = "date"
        else:
            unknown_reason = f"时间原文不可解析: {raw_time[:40]!r}"
        in_range = (published_date is not None
                    and start <= published_date.replace("-", "") <= end)
        if published_date and not in_range:
            continue
        rec = {
            "record_id": "eastmoney:" + _stable_hash(
                r.get("url") or (r.get("title", "") + raw_time)),
            "record_version": "eastmoney-news-v1",
            "instrument_id": query["instrument_id"],
            "title": r.get("title") or None,
            "content_text": r.get("content") or r.get("summary") or None,
            "publisher": r.get("source") or None,
            "original_url": r.get("url") or None,
            "published_at": published_at,
            "published_date": published_date,
            "time_precision": precision,
            "time_unknown_reason": unknown_reason,
            "source_updated_at": None,
        }
        if published_date is None and query["instrument_id"] is None:
            # 全市场快讯日期不可解析：不得混入声明完整的日期范围
            continue
        out.append(rec)
    return out


def capture_investor_qa(query: dict) -> list[dict]:
    provider = query["source_provider"]
    code = query["instrument_id"].split("/")[-1]
    start, end = query["start_date"], query["end_date"]
    ref = _now_cn()  # parser_reference_at：本次原页取回时刻（冻结）
    out = []
    if provider == "sse_e":
        mod = _gen_module("astock_gen_qa")
        import pandas as _pd
        # 2026-10-25 实测：sse 站点对 code 参数的单票过滤已失效（恒空），
        # 上游函数行为变化如实登记；改用全市场 answered 流多页拉取后按 code
        # 服务端过滤（同一 sse_e 原始源），coverage.basis 声明过滤位置
        rows_all = []
        for page in (1, 2, 3):
            frame = mod.sse_e_interaction(code=None, kind="answered",
                                          page=page, page_size=50)
            rows_all.extend(frame.to_dict("records")
                            if hasattr(frame, "to_dict") else [])
        rows = [r for r in rows_all
                if str(r.get("code") or "").lstrip("0") == code.lstrip("0")
                or r.get("code") == code]
        for r in rows:
            q_raw = (r.get("question_time") or "").strip()
            a_raw = (r.get("answer_time") or "").strip()
            q_at = _relative_or_abs(q_raw, ref)
            a_at = _relative_or_abs(a_raw, ref)
            reply_at = a_at
            published_at = reply_at or q_at
            published_date = published_at[:10] if published_at else None
            in_range = published_date and start <= published_date.replace(
                "-", "") <= end
            if not in_range:
                continue
            out.append({
                "record_id": f"sse_e:{r.get('id') or _stable_hash(str(r))}",
                "record_version": "sse-e-v1",
                "instrument_id": query["instrument_id"],
                "question_id": str(r.get("id") or "") or None,
                "question_text": r.get("question") or None,
                "question_at": q_at,
                "reply_text": r.get("answer") or None,
                "reply_at": a_at,
                "published_at": published_at,
                "published_date": published_date,
                "time_precision": "timestamp" if published_at else "unknown",
                "time_unknown_reason": None if published_at else
                f"相对时间原文不可解析: {q_raw[:30]!r}",
                "raw_question_time": q_raw,
                "raw_reply_time": a_raw,
                "parser_reference_at": ref.strftime(
                    "%Y-%m-%dT%H:%M") + "+08:00",
                "parser_timezone": "Asia/Shanghai",
                "original_url": r.get("source_url") or None,
                "source_updated_at": None,
            })
    else:  # cninfo_irm（深市）
        mod = _gen_module("astock_gen_qa")
        rows = mod.cninfo_irm(code, page_size=30)
        for r in rows:
            q_raw = r.get("ask_time") or ""
            q_at = _parse_cn_time(q_raw, ref, "%Y-%m-%d %H:%M")
            published_at = q_at
            published_date = published_at[:10] if published_at else None
            if not (published_date and start <= published_date.replace(
                    "-", "") <= end):
                continue
            out.append({
                "record_id": "cninfo_irm:" + _stable_hash(
                    (r.get("question") or "") + q_raw),
                "record_version": "cninfo-irm-v1",
                "instrument_id": query["instrument_id"],
                "question_id": None,
                "question_text": r.get("question") or None,
                "question_at": q_at,
                "reply_text": r.get("answer") or None,
                "reply_at": None,
                "published_at": published_at,
                "published_date": published_date,
                "time_precision": "timestamp" if published_at else "unknown",
                "time_unknown_reason": None if published_at else
                f"时间原文不可解析: {q_raw[:30]!r}",
                "raw_question_time": q_raw,
                "raw_reply_time": None,
                "parser_reference_at": ref.strftime(
                    "%Y-%m-%dT%H:%M") + "+08:00",
                "parser_timezone": "Asia/Shanghai",
                "original_url": None,
                "source_updated_at": None,
            })
    return out


def _relative_or_abs(text: str, ref: datetime) -> str | None:
    """sse_e 相对/绝对时间 → ISO（北京时区冻结换算；认不出 None）。"""
    text = (text or "").strip()
    if not text:
        return None
    m = re.match(r"(\d{4})-(\d{1,2})-(\d{1,2})\s+(\d{2}):(\d{2})", text)
    if m:
        y, mo, d, h, mi = map(int, m.groups())
        try:
            dt = datetime(y, mo, d, h, mi)
        except ValueError:
            return None
        return dt.strftime("%Y-%m-%dT%H:%M") + "+08:00"
    m = re.match(r"(\d{4})年(\d{1,2})月(\d{1,2})日\s*(\d{2}):(\d{2})", text)
    if m:
        y, mo, d, h, mi = map(int, m.groups())
        try:
            dt = datetime(y, mo, d, h, mi)
        except ValueError:
            return None
        return dt.strftime("%Y-%m-%dT%H:%M") + "+08:00"
    m = re.match(r"^(今天|昨天|前天)\s*(\d{1,2}):(\d{2})$", text)
    if m:
        import datetime as dt
        day = {"今天": 0, "昨天": 1, "前天": 2}[m.group(1)]
        when = (ref - dt.timedelta(days=day)).replace(
            hour=int(m.group(2)), minute=int(m.group(3)))
        return when.strftime("%Y-%m-%dT%H:%M") + "+08:00"
    m = re.match(r"^(\d+)\s*(秒|分钟|小时|天)前$", text)
    if m:
        import datetime as dt
        unit = {"秒": "seconds", "分钟": "minutes", "小时": "hours",
                "天": "days"}[m.group(2)]
        when = ref - dt.timedelta(**{unit: int(m.group(1))})
        return when.strftime("%Y-%m-%dT%H:%M") + "+08:00"
    if text in ("刚刚", "刚才"):
        return ref.strftime("%Y-%m-%dT%H:%M") + "+08:00"
    return None


def _stable_hash(text: str) -> str:
    return hashlib.sha256(text.encode("utf-8")).hexdigest()[:16]


CAPTURE = {"announcements": capture_announcements, "news": capture_news,
           "investor_qa": capture_investor_qa}


# ---------------------------------------------------------------------------
# get_public_records / get_public_artifact（v3 信封）
# ---------------------------------------------------------------------------


def get_public_records_v3(args: dict) -> dict:
    try:
        query = normalize_request(args)
    except PublicFactsError as exc:
        return _failure(exc)
    canonical = canonical_query(query)
    request_hash = digest(canonical)
    if query.get("snapshot_id") or query.get("cursor"):
        return _from_snapshot(query, request_hash)

    # ---- capture：先固定来源范围，再分页（分页不实时读源）----
    try:
        records = CAPTURE[query["dataset_id"]](query)
    except PublicFactsError:
        raise
    except Exception as exc:  # noqa: BLE001 - 上游异常归一化
        return _failure(PublicFactsError(
            "SOURCE_UNAVAILABLE",
            f"上游读取失败: {type(exc).__name__}: {exc}"[:200],
            details={"dataset": query["dataset_id"],
                     "provider": query["source_provider"]}))
    records.sort(key=lambda r: (r.get("published_date") or "",
                                r.get("record_id")))
    snapshot_id = "pf-" + _stable_hash(request_hash + _now_utc())[:16]
    first_seen_at = _now_utc()
    for r in records:
        r["first_seen_at"] = _first_seen(
            {}, r["record_id"]) or first_seen_at
    artifact = {
        "records": records,
        "canonical_query": canonical,
        "query": query,
        "captured_at": _now_utc(),
    }
    blob = json.dumps(artifact, ensure_ascii=False, sort_keys=True,
                      allow_nan=False).encode("utf-8")
    content_sha = hashlib.sha256(blob).hexdigest()
    art_id = "art-" + _stable_hash(content_sha)
    store = _artifact_store()
    (store / f"{art_id}.json").write_bytes(blob)
    record_list_hash = digest(records)
    snapshot = {
        "kind": "public-facts", "artifact_id": art_id,
        "content_sha256": content_sha, "size": len(blob),
        "snapshot_data_hash": record_list_hash,
        "canonical_query": canonical, "query": query,
        "captured_at": _now_utc(),
        "expires_at_ts": time.time() + 7 * 86400,
    }
    store = _artifact_store()
    (store / f"{snapshot_id}.snapshot.json").write_text(
        json.dumps(snapshot, ensure_ascii=False), encoding="utf-8")
    return _page_response(query, canonical, request_hash, records,
                          snapshot_id, snapshot["snapshot_data_hash"],
                          snapshot["captured_at"], 0)


def _page_response(query: dict, canonical: dict, request_hash: str,
                   records: list[dict], snapshot_id: str,
                   snapshot_hash: str, captured_at: str, offset: int) -> dict:
    limit = query["limit"]
    page = records[offset:offset + limit]
    has_more = offset + limit < len(records)
    next_cursor = None
    if has_more:
        next_cursor = "off:" + str(offset + limit) + ":" + _stable_hash(
            snapshot_id + str(offset + limit))[:12]
    start_iso, end_iso = _iso(query["start_date"]), _iso(query["end_date"])
    dates = [r.get("published_date") for r in records
             if r.get("published_date")]
    coverage = {
        "requested_start": start_iso, "requested_end": end_iso,
        "available_start": min(dates) if dates else None,
        "available_end": max(dates) if dates else None,
        # provider 近期窗口/当前快照不可宣布历史 complete；cninfo 列表属
        # enumerated_window（按 issuer×日期窗枚举）但 total 漂移无法本地证明
        # → 保守 partial，basis 声明
        "completeness": "partial",
        "basis": ("provider list window; upstream_total not locally "
                  "provable, completeness=partial until enumerated_window "
                  "basis is verified against upstream paging stability"),
        "upstream_total": None,
        "captured_total": len(records),
    }
    served = _now_cn().strftime("%Y-%m-%dT%H:%M:%S") + "+08:00"
    import datetime as _dt
    expires = (_dt.datetime.now(_dt.timezone(_dt.timedelta(hours=8)))
               + _dt.timedelta(days=7)).strftime("%Y-%m-%dT%H:%M:%S") + "+08:00"
    status = ("complete" if not has_more
              and coverage["completeness"] == "complete" else "partial")
    header = {
        "contract_version": "stockdb-public-facts-v1",
        "contract_revision": "1.0.0",
        "server_build_id": _server_build_id(),
        "request_id": "pf-" + _stable_hash(request_hash + served)[:12],
        "served_at": served,
        "query": canonical,
        "request_hash": request_hash,
        "data_hash": digest(page),
        "snapshot_data_hash": snapshot_hash,
        "snapshot_id": snapshot_id,
        "snapshot_expires_at": expires,
        "status": status,
        "truncated": False,
        "has_more": has_more,
        "next_cursor": next_cursor,
        "coverage": coverage,
        "source_catalog": _source_catalog(query, captured_at),
        "data": page,
        "errors": [],
    }
    return header


def _source_catalog(query: dict, captured_at: str) -> dict:
    dataset, provider = query["dataset_id"], query["source_provider"]
    origin = {
        "cninfo": "http://www.cninfo.com.cn/new/fulltextSearch (巨潮资讯网)",
        "eastmoney": ("https://search-api-web.eastmoney.com (东财个股新闻/"
                      "np-weblist 全球资讯)"),
        "sse_e": "https://sns.sseinfo.com/ (上证e互动)",
        "cninfo_irm": "http://irm.cninfo.com.cn/ (互动易)",
    }[provider]
    return {"s1": {
        "provider": {"cninfo": "cninfo (深圳证券信息网)",
                     "eastmoney": "eastmoney (东方财富)",
                     "sse_e": "sse_e (上海证券交易所 上证e互动)",
                     "cninfo_irm": "cninfo_irm (巨潮资讯网 互动易)"}[provider],
        "origin_reference": origin,
        "retrieved_at": captured_at,
        "adapter_id": f"astock-generated/{dataset}",
        "adapter_version": "1.0.0",
        "upstream_commit": "02aa8b4d1316629e819cdc17a14297ca1a70edcd",
        "parser_version": "astock-wrapper-1.0.0",
        "raw_artifact_id": None,
        "raw_sha256": None,
    }}


_ARTIFACT_STORE = None


def _artifact_store() -> Path:
    global _ARTIFACT_STORE
    if _ARTIFACT_STORE is None:
        _ARTIFACT_STORE = _snapshot_dir()
        (_ARTIFACT_STORE / "artifacts").mkdir(exist_ok=True)
        _ARTIFACT_STORE = _ARTIFACT_STORE
    return _ARTIFACT_STORE


def _from_snapshot(query: dict, request_hash: str) -> dict:
    store = _artifact_store()
    snapshot_id = query.get("snapshot_id")
    path = store / f"{snapshot_id}.snapshot.json"
    if not path.exists():
        return _failure(PublicFactsError(
            "SNAPSHOT_NOT_FOUND", f"快照 {snapshot_id} 不存在"))
    snap = json.loads(path.read_text(encoding="utf-8"))
    if snap.get("expires_at_ts", 0) < time.time():
        return _failure(PublicFactsError(
            "SNAPSHOT_EXPIRED", f"快照 {snapshot_id} 已过保留期"))
    captured = snap["canonical_query"]
    canonical = canonical_query(query)
    for k in ("dataset_id", "source_provider", "instrument_id",
              "start_date", "end_date"):
        if canonical[k] != captured[k]:
            return _failure(PublicFactsError(
                "SNAPSHOT_SCOPE_MISMATCH",
                f"重放 {k}={canonical[k]!r} 与捕获 {captured[k]!r} 不一致",
                details={"field": k}))
    art = json.loads((store / f"{snap['artifact_id']}.json")
                     .read_text(encoding="utf-8"))
    records = art["records"]
    cursor = query.get("cursor")
    offset = 0
    if cursor:
        m = re.match(r"^off:(\d+):([0-9a-f]{12})$", cursor)
        if not m:
            return _failure(PublicFactsError(
                "INVALID_ARGUMENT", "cursor 格式非法",
                details={"received": cursor[:40]}))
        off = int(m.group(1))
        if m.group(2) != _stable_hash(snapshot_id + str(off))[:12]:
            return _failure(PublicFactsError(
                "INVALID_ARGUMENT", "cursor 校验失败（不指向本快照）"))
        offset = off
    return _page_response(query, canonical, request_hash, records,
                          snapshot_id, snap["snapshot_data_hash"],
                          snap["captured_at"], offset)


def get_public_artifact_v3(args: dict) -> dict:
    allowed = {"contract_version", "artifact_id", "offset", "length"}
    unknown = set(args) - allowed
    if unknown:
        return _failure(PublicFactsError(
            "INVALID_ARGUMENT", f"未知字段: {sorted(unknown)}"))
    if args.get("contract_version") != "stockdb-public-facts-v1":
        return _failure(PublicFactsError("UNSUPPORTED_CONTRACT",
                                         "contract_version 不符"))
    art_id = args.get("artifact_id")
    if not isinstance(art_id, str) or not re.fullmatch(r"art-[0-9a-f]{16}",
                                                       art_id or ""):
        return _failure(PublicFactsError(
            "ARTIFACT_NOT_FOUND", "artifact_id 非法（服务已发出的 opaque id）"))
    offset = args.get("offset", 0)
    length = args.get("length", CHUNK_MAX)
    if not isinstance(offset, int) or isinstance(offset, bool) or offset < 0:
        return _failure(PublicFactsError("INVALID_ARGUMENT",
                                         "offset 必须为非负整数"))
    if not isinstance(length, int) or isinstance(length, bool) \
            or not (1 <= length <= CHUNK_MAX):
        return _failure(PublicFactsError(
            "ARTIFACT_LIMIT",
            f"length 必须为 1–{CHUNK_MAX}"))
    store = _artifact_store()
    path = store / f"{art_id}.json"
    if not path.exists():
        return _failure(PublicFactsError("ARTIFACT_NOT_FOUND",
                                         f"artifact {art_id} 不存在"))
    blob = path.read_bytes()
    if len(blob) > ARTIFACT_MAX:
        return _failure(PublicFactsError("ARTIFACT_LIMIT", "超过 20MiB 上限"))
    if offset >= len(blob):
        return _failure(PublicFactsError(
            "INVALID_ARGUMENT",
            f"offset {offset} 超出 artifact 末尾（size={len(blob)}）"))
    chunk = blob[offset:offset + length]
    return {
        "contract_version": "stockdb-public-facts-v1",
        "contract_revision": "1.0.0",
        "artifact_id": art_id,
        "content_sha256": hashlib.sha256(blob).hexdigest(),
        "size": len(blob),
        "mime": "application/json",
        "charset": "utf-8",
        "offset": offset,
        "chunk_sha256": hashlib.sha256(chunk).hexdigest(),
        "chunk_size": len(chunk),
        "eof": offset + len(chunk) >= len(blob),
        "data_base64": base64.b64encode(chunk).decode("ascii"),
    }


def _server_build_id() -> str:
    import os
    commit = os.environ.get("STOCKDB_BUILD_COMMIT") or ""
    version = "0.14.2"
    return (os.environ.get("STOCKDB_BUILD_ID")
            or f"src+{version}+{commit[:12] if commit else 'nogit'}")


def _failure(exc: PublicFactsError) -> dict:
    return {
        "contract_version": "stockdb-public-facts-v1",
        "contract_revision": "1.0.0",
        "server_build_id": _server_build_id(),
        "request_id": "pf-" + _stable_hash(str(exc) + _now_utc())[:12],
        "served_at": _now_cn().strftime("%Y-%m-%dT%H:%M:%S") + "+08:00",
        "status": "failed",
        "errors": [{"code": exc.code, "message": exc.message,
                    "retryable": exc.retryable}],
    }


import time  # noqa: E402  （snapshot 过期判定用；置于末尾避免顶层副作用审查）
