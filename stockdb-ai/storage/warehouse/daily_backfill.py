"""storage.warehouse.daily_backfill — LevelDB `k日k` 回填标准 `facts/daily` 与原库去重裁剪。

职责：
1. 解析 LevelDB `k日k:<code>:<YYYYMMDD>` MsgPack 值并统一字段映射（`pre_close -> prev_close`），
   只保留调用方注入的股票判定函数（如 A 股代码判定）认可的标的；
2. 利用 `adjust_events` + `k复权` 在写入前计算 `adj_factor` 与 `*_fq` 物化复权列，并经
   `sink.write_daily(..., advance_watermark=False)` 幂等补齐历史分区，同时保持既定近期
   分区（如近一年日线）及 `watermark:daily` 不被改写或倒退；
3. 自动级联补齐因此变完整的历史 `facts/week` 与 `facts/month`；
4. 支持两种安全通道：
   - 跨机同步通道（Windows -> NAS HTTP）：按完整自然月（`YYYYMM`）打包标准 21 列 Parquet
     批次上传入仓 + `readback` 字段级逐行逐列对账（缺行=0、多行=0、错值=0）；
   - 仓内收口通道（NAS 本地合并与去重）：将已落入 `facts/leveldb_raw` 的 `k日k` 历史缺失日
     合并回 `facts/daily` 并字段级校验，再按 `(snapshot_id, source_db)` 无损裁剪 `k日k`
     原库批次（保留未受影响的前序批次零重写，仅重编受影响区间的 `batch_index` / `seq_no`），
     并触发 `bulk_import.verify_bulk_import` 自校验确保 0 哈希错配与 `seq_contiguous=True`。
"""
from __future__ import annotations

import bisect
import datetime as dt_mod
import hashlib
import json
import math
import os
import shutil
import threading
import time
import uuid
from collections.abc import Callable, Iterable, Sequence
from pathlib import Path
from typing import Any

from storage.providers import msgpack_lite

from . import bulk_import, catalog, inventory, layout, leveldb_snapshot, sink

DAILY_BASE_COLUMNS: tuple[tuple[str, str], ...] = sink.DAILY_BASE_COLUMNS
DAILY_BASE_COLUMN_NAMES: tuple[str, ...] = tuple(name for name, _ in DAILY_BASE_COLUMNS)

_SOURCE_PRIORITY: dict[str, int] = {
    "data1": 3,
    "data": 2,
    "mydb": 1,
}


def _now_iso() -> str:
    return time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())


def _safe_float(value: Any) -> float | None:
    if value is None or isinstance(value, bool):
        return None
    try:
        num = float(value)
    except (TypeError, ValueError):
        return None
    if not math.isfinite(num):
        return None
    return 0.0 if num == 0.0 else num


def _decode_payload(raw_value: bytes) -> object:
    if not raw_value:
        return None
    b0 = raw_value[0]
    if 0x80 <= b0 <= 0x9F or b0 in (0xDC, 0xDD, 0xDE, 0xDF):
        try:
            return msgpack_lite.unpack(raw_value)
        except Exception:
            pass
    try:
        text = raw_value.decode("utf-8").lstrip()
        if text.startswith(("{", "[")):
            return json.loads(text)
    except Exception:
        pass
    try:
        return msgpack_lite.unpack(raw_value)
    except Exception:
        return None


def parse_kdaily_key(raw_key: bytes) -> tuple[str, str] | None:
    """解析 `k日k:<code>:<YYYYMMDD>`，合法返回 `(code, date8)`，否则返回 None。"""
    if b"\x00" in raw_key:
        return None
    try:
        key_text = raw_key.decode("utf-8")
    except UnicodeDecodeError:
        return None
    if not key_text.startswith("k日k:"):
        return None
    parts = key_text.split(":", 3)
    if len(parts) != 3:
        return None
    code = parts[1].strip()
    date8 = parts[2].strip()
    if not code or len(date8) != 8 or not date8.isdigit():
        return None
    return code, date8


def parse_kdaily_entry(
    raw_key: bytes,
    raw_value: bytes,
    *,
    code_predicate: Callable[[str], bool] | None = None,
) -> tuple[str, str, dict[str, Any]] | None:
    """解码单条 `k日k` KV 为标准 21 列基础字典；非目标标的白名单或损坏记录返回 None。"""
    parsed = parse_kdaily_key(raw_key)
    if parsed is None:
        return None
    code, date8 = parsed
    if code_predicate is not None and not code_predicate(code):
        return None

    obj = _decode_payload(raw_value)
    if not isinstance(obj, dict):
        return None

    raw_prev = obj.get("prev_close")
    if raw_prev is None:
        raw_prev = obj.get("pre_close")

    row: dict[str, Any] = {
        "code": str(obj.get("code") or code).strip() or code,
        "date": date8,
        "name": str(obj.get("name") or ""),
        "is_st": bool(obj.get("is_st")) if obj.get("is_st") is not None else None,
        "open": _safe_float(obj.get("open")),
        "high": _safe_float(obj.get("high")),
        "low": _safe_float(obj.get("low")),
        "close": _safe_float(obj.get("close")),
        "prev_close": _safe_float(raw_prev),
        "volume": _safe_float(obj.get("volume")),
        "amount": _safe_float(obj.get("amount")),
        "turnover": _safe_float(obj.get("turnover")),
        "pct_chg": _safe_float(obj.get("pct_chg")),
        "amplitude": _safe_float(obj.get("amplitude")),
        "vol_ratio": _safe_float(obj.get("vol_ratio")),
        "pb": _safe_float(obj.get("pb")),
        "pe_ttm": _safe_float(obj.get("pe_ttm")),
        "total_share": _safe_float(obj.get("total_share")),
        "float_share": _safe_float(obj.get("float_share")),
        "total_mv": _safe_float(obj.get("total_mv")),
        "float_mv": _safe_float(obj.get("float_mv")),
    }
    return code, date8, row


def parse_kadjust_entry(
    raw_key: bytes,
    raw_value: bytes,
    *,
    code_predicate: Callable[[str], bool] | None = None,
) -> list[dict[str, Any]]:
    """解码 `k复权:<code>[:<YYYYMMDD>]` 为摊平复权事件列表。"""
    if b"\x00" in raw_key:
        return []
    try:
        key_text = raw_key.decode("utf-8")
    except UnicodeDecodeError:
        return []
    if not key_text.startswith("k复权:"):
        return []
    parts = key_text.split(":", 3)
    if len(parts) < 2:
        return []
    code = parts[1].strip()
    if not code:
        return []
    if code_predicate is not None and not code_predicate(code):
        return []
    key_date = parts[2].strip() if len(parts) >= 3 and len(parts[2].strip()) == 8 and parts[2].strip().isdigit() else None

    obj = _decode_payload(raw_value)
    events: list[dict[str, Any]] = []
    if isinstance(obj, dict):
        if "cum" in obj or "div" in obj or "date" in obj:
            ev_date = str(obj.get("date") or key_date or "").strip()
            if len(ev_date) == 8 and ev_date.isdigit():
                events.append({**obj, "code": code, "date": ev_date})
        else:
            for d_key, ev in obj.items():
                if isinstance(ev, dict):
                    ev_date = str(ev.get("date") or d_key or key_date or "").strip()
                    if len(ev_date) == 8 and ev_date.isdigit():
                        events.append({**ev, "code": code, "date": ev_date})
    elif isinstance(obj, list):
        for item in obj:
            if isinstance(item, dict):
                ev_date = str(item.get("date") or key_date or "").strip()
                if len(ev_date) == 8 and ev_date.isdigit():
                    events.append({**item, "code": code, "date": ev_date})
    return events


def build_factor_series(events: list[dict[str, Any]]) -> dict[str, list[tuple[int, float]]]:
    """复权事件列表 -> `{code: [(date_int, cum), ...] 按日期升序}`。"""
    series: dict[str, list[tuple[int, float]]] = {}
    for ev in events or []:
        code = str(ev.get("code") or "").strip()
        date_str = str(ev.get("date") or "").strip()
        cum = _safe_float(ev.get("cum"))
        if not code or cum is None or len(date_str) != 8 or not date_str.isdigit():
            continue
        series.setdefault(code, []).append((int(date_str), cum))
    for pts in series.values():
        pts.sort()
    return series


def factor_map_at(series: dict[str, list[tuple[int, float]]], date8: str) -> dict[str, float]:
    """返回截至 `date8` 的每只股票有效累计复权因子 `{code: cum}`。"""
    try:
        target_int = int(layout.normalize_date(date8))
    except (TypeError, ValueError):
        return {}
    out: dict[str, float] = {}
    for code, pts in series.items():
        idx = bisect.bisect_right(pts, (target_int, float("inf")))
        if idx > 0:
            out[code] = pts[idx - 1][1]
    return out


def load_factor_series(
    root: Path,
    *,
    include_leveldb_adjust: bool = True,
    code_predicate: Callable[[str], bool] | None = None,
) -> dict[str, list[tuple[int, float]]]:
    """合并 DuckDB `adjust_events` 与可选 `facts/leveldb_raw` `k复权` 构造全历史复权因子序列。"""
    root = Path(root)
    merged_by_pair: dict[tuple[str, str], float] = {}

    con = catalog._connect(root)
    try:
        if include_leveldb_adjust and any(layout.leveldb_raw_dir(root).rglob("*.parquet")):
            bulk_import.ensure_leveldb_views(con, root)
            rows = con.execute(
                "SELECT raw_key, raw_value FROM v_leveldb_latest "
                "WHERE raw_key >= ?::BLOB AND raw_key < ?::BLOB",
                ["k复权:".encode("utf-8"), leveldb_snapshot._prefix_upper_bound("k复权:".encode("utf-8"))],
            ).fetchall()
            for rk, rv in rows:
                for ev in parse_kadjust_entry(bytes(rk), bytes(rv), code_predicate=code_predicate):
                    cum = _safe_float(ev.get("cum"))
                    if cum is not None:
                        merged_by_pair[(str(ev["code"]), str(ev["date"]))] = cum

        adj_rows = con.execute("SELECT code, date, cum FROM adjust_events").fetchall()
        for code, date_str, cum_val in adj_rows:
            code_s = str(code or "").strip()
            d8 = str(date_str or "").strip()
            if not code_s or len(d8) != 8 or not d8.isdigit():
                continue
            if code_predicate is not None and not code_predicate(code_s):
                continue
            cum = _safe_float(cum_val)
            if cum is not None:
                merged_by_pair[(code_s, d8)] = cum
    finally:
        con.close()

    events = [
        {"code": c, "date": d, "cum": v}
        for (c, d), v in merged_by_pair.items()
    ]
    return build_factor_series(events)


def _canonical_cell(val: Any, col_type: str) -> Any:
    if val is None:
        return None
    if col_type == "DATE":
        if isinstance(val, (dt_mod.date, dt_mod.datetime)):
            return val.strftime("%Y-%m-%d")
        return layout.iso_date(str(val))
    if col_type == "BOOLEAN":
        return bool(val)
    if col_type == "DOUBLE":
        return _safe_float(val)
    return str(val)


def _canonical_tuple(raw_tuple: Sequence[Any]) -> tuple[Any, ...]:
    return tuple(
        _canonical_cell(val, col_type)
        for val, (_, col_type) in zip(raw_tuple, DAILY_BASE_COLUMNS)
    )


def compute_daily_batch_digest(tuples: Sequence[Sequence[Any]]) -> str:
    """对标准化 21 列日K元组计算按 `(date, code)` 排序后的确定性 SHA-256。"""
    canon = sorted((_canonical_tuple(t) for t in tuples), key=lambda x: (str(x[1]), str(x[0])))
    h = hashlib.sha256()
    for row in canon:
        h.update(json.dumps(list(row), ensure_ascii=False, separators=(",", ":")).encode("utf-8"))
        h.update(b"\n")
    return h.hexdigest()


def write_daily_backfill_batch_parquet(
    out_path: Path,
    rows: list[dict[str, Any]],
    *,
    snapshot_id: str,
    source_db: str,
    batch_id: str,
) -> dict[str, Any]:
    """将解码后的 21 列标准日K行序列化为按 `(date, code)` 排序的 Zstd Parquet 批次。"""
    import duckdb

    if not rows:
        raise bulk_import.BulkImportError("日线回填批次不能为空（0 行）")

    norm_rows, _ = sink._normalize_rows(rows, DAILY_BASE_COLUMNS)
    dedup: dict[tuple[str, str], tuple[Any, ...]] = {}
    for row in norm_rows:
        c_row = _canonical_tuple(row)
        dedup[(str(c_row[1]), str(c_row[0]))] = c_row
    ordered_rows = [dedup[k] for k in sorted(dedup)]
    dates8 = sorted({str(r[1]).replace("-", "") for r in ordered_rows})

    out_path = Path(out_path)
    out_path.parent.mkdir(parents=True, exist_ok=True)
    tmp_path = out_path.with_name(f".{out_path.name}.{os.getpid()}.tmp")
    con = duckdb.connect()
    try:
        sink._load_rows(con, "_backfill_stage", ordered_rows, DAILY_BASE_COLUMNS)
        sql_path = tmp_path.as_posix()
        con.execute(
            f"COPY (SELECT * FROM _backfill_stage ORDER BY date ASC, code ASC) "
            f"TO '{sql_path}' (FORMAT PARQUET, COMPRESSION ZSTD)"
        )
        os.replace(tmp_path, out_path)
    except Exception:
        tmp_path.unlink(missing_ok=True)
        raise
    finally:
        con.close()

    parquet_bytes = out_path.read_bytes()
    return {
        "snapshot_id": snapshot_id,
        "source_db": source_db,
        "batch_id": batch_id,
        "row_count": len(ordered_rows),
        "dates": dates8,
        "date_min": dates8[0],
        "date_max": dates8[-1],
        "file_size": len(parquet_bytes),
        "parquet_sha256": hashlib.sha256(parquet_bytes).hexdigest(),
        "rows_digest_sha256": compute_daily_batch_digest(ordered_rows),
    }


def _period_range_complete(
    root: Path,
    start_dt: dt_mod.date,
    end_dt: dt_mod.date,
    is_trading_day_fn: Callable[[dt_mod.date], bool] | None,
) -> bool:
    """校验 `[start_dt, end_dt]` 内每个交易日均已在 `facts/daily` 落盘或标记空日。"""
    cursor = start_dt
    while cursor <= end_dt:
        if is_trading_day_fn is not None and not is_trading_day_fn(cursor):
            cursor += dt_mod.timedelta(days=1)
            continue
        d8 = cursor.strftime("%Y%m%d")
        has_file = any(
            layout.daily_partition(root, d8, m).exists()
            for m in ("sh", "sz", "bj", "hk")
        )
        marked_empty = catalog.get_meta(root, f"empty:{d8}") is not None
        if not has_file and not marked_empty:
            return False
        cursor += dt_mod.timedelta(days=1)
    return True


def _week_bounds(d8: str) -> tuple[str, dt_mod.date, dt_mod.date]:
    dt = dt_mod.datetime.strptime(d8, "%Y%m%d").date()
    friday = dt + dt_mod.timedelta(days=(4 - dt.weekday()))
    monday = friday - dt_mod.timedelta(days=4)
    return friday.strftime("%Y%m%d"), monday, friday


def _month_bounds(d8: str) -> tuple[str, dt_mod.date, dt_mod.date]:
    dt = dt_mod.datetime.strptime(d8, "%Y%m%d").date()
    start = dt_mod.date(dt.year, dt.month, 1)
    nxt = dt_mod.date(dt.year + 1, 1, 1) if dt.month == 12 else dt_mod.date(dt.year, dt.month + 1, 1)
    end = nxt - dt_mod.timedelta(days=1)
    return end.strftime("%Y%m%d"), start, end


def cascade_week_and_month_for_dates(
    root: Path,
    dates: Iterable[str],
    *,
    is_trading_day_fn: Callable[[dt_mod.date], bool] | None = None,
    advance_watermark: bool = False,
) -> dict[str, Any]:
    """针对新补齐的历史日线日期，自动检测并物化已完整的周线与月线分区。"""
    root = Path(root)
    norm_dates = sorted({layout.normalize_date(d) for d in dates})
    weeks: dict[str, tuple[dt_mod.date, dt_mod.date]] = {}
    months: dict[str, tuple[dt_mod.date, dt_mod.date]] = {}
    for d8 in norm_dates:
        w_end, w_start_dt, w_end_dt = _week_bounds(d8)
        weeks.setdefault(w_end, (w_start_dt, w_end_dt))
        m_end, m_start_dt, m_end_dt = _month_bounds(d8)
        months.setdefault(m_end, (m_start_dt, m_end_dt))

    weeks_written: list[str] = []
    weeks_skipped: list[str] = []
    for w_end in sorted(weeks):
        w_start_dt, w_end_dt = weeks[w_end]
        if _period_range_complete(root, w_start_dt, w_end_dt, is_trading_day_fn):
            res = sink.aggregate_weekly(root, w_end, advance_watermark=advance_watermark)
            if res.get("status") == "written":
                weeks_written.append(w_end)
            else:
                weeks_skipped.append(w_end)

    months_written: list[str] = []
    months_skipped: list[str] = []
    for m_end in sorted(months):
        m_start_dt, m_end_dt = months[m_end]
        if _period_range_complete(root, m_start_dt, m_end_dt, is_trading_day_fn):
            res = sink.aggregate_monthly(root, m_end, advance_watermark=advance_watermark)
            if res.get("status") == "written":
                months_written.append(m_end)
            else:
                months_skipped.append(m_end)

    return {
        "weeks_written": weeks_written,
        "weeks_skipped": weeks_skipped,
        "months_written": months_written,
        "months_skipped": months_skipped,
    }


def _read_validated_daily_staging(
    staging_path: Path,
    *,
    expected_row_count: int | None,
    expected_rows_digest: str | None,
) -> tuple[list[dict[str, Any]], int, str]:
    import duckdb

    sql_path = str(staging_path).replace("\\", "/")
    con = duckdb.connect()
    try:
        try:
            desc_rows = con.execute("DESCRIBE SELECT * FROM read_parquet(?)", [sql_path]).fetchall()
        except Exception as exc:
            raise bulk_import.BulkImportError(f"日线回填 Parquet 损坏或不可读: {exc}") from exc

        actual_cols = [(str(r[0]), str(r[1])) for r in desc_rows]
        expected_cols = [(n, "VARCHAR" if t == "TEXT" else t) for n, t in DAILY_BASE_COLUMNS]
        if actual_cols != expected_cols:
            raise bulk_import.BulkImportError(
                f"日线回填 Parquet schema 不匹配: expected={expected_cols}, actual={actual_cols}"
            )

        raw_tuples = con.execute(
            f"SELECT {', '.join(DAILY_BASE_COLUMN_NAMES)} FROM read_parquet(?) "
            f"ORDER BY date ASC, code ASC",
            [sql_path],
        ).fetchall()
    finally:
        con.close()

    row_count = len(raw_tuples)
    if row_count <= 0:
        raise bulk_import.BulkImportError("日线回填批次不能为空（0 行）")
    if expected_row_count is not None and row_count != int(expected_row_count):
        raise bulk_import.BulkImportError(
            f"日线回填行数不匹配: expected={expected_row_count}, actual={row_count}"
        )

    actual_digest = compute_daily_batch_digest(raw_tuples)
    if expected_rows_digest is not None and actual_digest != expected_rows_digest.lower():
        raise bulk_import.BulkImportError(
            f"日线回填 rows_digest_sha256 校验失败: expected={expected_rows_digest}, actual={actual_digest}"
        )

    rows_dicts: list[dict[str, Any]] = []
    seen_pairs: set[tuple[str, str]] = set()
    for tup in raw_tuples:
        ct = _canonical_tuple(tup)
        pair = (str(ct[1]), str(ct[0]))
        if pair in seen_pairs:
            raise bulk_import.BulkImportError(f"日线回填批内存在重复 (date, code): {pair}")
        seen_pairs.add(pair)
        row_dict = dict(zip(DAILY_BASE_COLUMN_NAMES, ct))
        row_dict["date"] = str(ct[1]).replace("-", "")
        rows_dicts.append(row_dict)
    return rows_dicts, row_count, actual_digest


def ingest_daily_backfill_parquet_batch(
    root: Path,
    payload: bytes,
    *,
    snapshot_id: str,
    source_db: str,
    batch_id: str,
    overwrite_existing: bool = False,
    parquet_sha256: str | None = None,
    expected_row_count: int | None = None,
    expected_rows_digest: str | None = None,
    code_predicate: Callable[[str], bool] | None = None,
    is_trading_day_fn: Callable[[dt_mod.date], bool] | None = None,
    refresh_views_cb: Callable[[], None] | None = None,
) -> dict[str, Any]:
    """NAS 侧接收、校验并按完整交易日原子回填标准 `facts/daily` 分区。"""
    root = Path(root)
    layout.validate_identifier(snapshot_id, "snapshot_id")
    layout.validate_identifier(source_db, "source_db")
    layout.validate_identifier(batch_id, "batch_id")
    if not payload:
        raise bulk_import.BulkImportError("上传请求体为空")

    actual_parquet_sha256 = hashlib.sha256(payload).hexdigest()
    if parquet_sha256 is not None and actual_parquet_sha256 != parquet_sha256.lower():
        raise bulk_import.BulkImportError(
            f"parquet_sha256 校验失败: expected={parquet_sha256}, actual={actual_parquet_sha256}"
        )

    staging_dir = root / ".staging" / "daily_backfill" / snapshot_id / source_db
    staging_dir.mkdir(parents=True, exist_ok=True)
    staging_path = staging_dir / f"{batch_id}.{uuid.uuid4().hex[:12]}.parquet.tmp"

    try:
        staging_path.write_bytes(payload)
        rows_dicts, row_count, actual_digest = _read_validated_daily_staging(
            staging_path,
            expected_row_count=expected_row_count,
            expected_rows_digest=expected_rows_digest,
        )
    finally:
        staging_path.unlink(missing_ok=True)

    rows_by_date: dict[str, list[dict[str, Any]]] = {}
    filtered_non_target = 0
    for row in rows_dicts:
        code = str(row.get("code") or "")
        if code_predicate is not None and not code_predicate(code):
            filtered_non_target += 1
            continue
        rows_by_date.setdefault(str(row["date"]), []).append(row)

    written_dates: list[str] = []
    skipped_dates: list[str] = []
    written_rows = 0

    with bulk_import._BULK_LOCK:
        series = load_factor_series(
            root,
            include_leveldb_adjust=True,
            code_predicate=code_predicate,
        )
        for d8 in sorted(rows_by_date):
            day_rows = rows_by_date[d8]
            fmap = factor_map_at(series, d8)
            if overwrite_existing:
                for mkt in {layout.market_of(str(r.get("code") or "")) for r in day_rows}:
                    layout.daily_partition(root, d8, mkt).unlink(missing_ok=True)
            res = sink.write_daily(root, d8, day_rows, factor_map=fmap, advance_watermark=False)
            if res.get("status") == "written":
                written_dates.append(d8)
                written_rows += int(res.get("rows") or 0)
            else:
                skipped_dates.append(d8)

        cascade = cascade_week_and_month_for_dates(
            root,
            rows_by_date.keys(),
            is_trading_day_fn=is_trading_day_fn,
            advance_watermark=False,
        )

        changed_any = bool(
            written_dates or cascade["weeks_written"] or cascade["months_written"]
        )
        if refresh_views_cb is not None and changed_any:
            refresh_views_cb()
        if changed_any:
            inventory.record_inventory_event(
                root,
                "daily_backfill",
                snapshot_id=snapshot_id,
                source_db=source_db,
                summary_prefix=f"日线回填 {source_db}：",
            )

    return {
        "ok": True,
        "snapshot_id": snapshot_id,
        "source_db": source_db,
        "batch_id": batch_id,
        "row_count": row_count,
        "filtered_non_stock_rows": filtered_non_target,
        "parquet_sha256": actual_parquet_sha256,
        "rows_digest_sha256": actual_digest,
        "written_dates": written_dates,
        "skipped_dates": skipped_dates,
        "written_rows": written_rows,
        "weeks_written": cascade["weeks_written"],
        "months_written": cascade["months_written"],
    }


def _daily_parquet_files_for_dates(root: Path, norm_dates: list[str]) -> list[str]:
    files: list[str] = []
    for d8 in norm_dates:
        yr = d8[:4]
        for market in ("sh", "sz", "bj", "hk"):
            p = layout.facts_dir(root) / "daily" / f"year={yr}" / f"market={market}" / f"date={d8}.parquet"
            if p.is_file():
                files.append(p.as_posix())
    return files


def readback_daily_parquet(
    root: Path,
    *,
    dates: Sequence[str],
    include_fq: bool = False,
) -> tuple[bytes, dict[str, Any]]:
    """从标准 `facts/daily` 按日期列表导出排序后的 Zstd Parquet 字节流供逐列对账。"""
    import duckdb

    root = Path(root)
    norm_dates = sorted({layout.normalize_date(d) for d in dates if str(d).strip()})
    if not norm_dates:
        raise bulk_import.BulkImportError("dates 不能为空")

    cols = sink._DAILY_COLUMNS if include_fq else DAILY_BASE_COLUMNS
    col_select = ", ".join(name for name, _ in cols)
    files = _daily_parquet_files_for_dates(root, norm_dates)

    tmp_dir = root / ".staging" / "daily_readback"
    tmp_dir.mkdir(parents=True, exist_ok=True)
    tmp_path = tmp_dir / f"rb_{uuid.uuid4().hex[:12]}.parquet.tmp"
    con = duckdb.connect()
    try:
        if not files:
            empty_select = ", ".join(f"CAST(NULL AS {ctype}) AS {name}" for name, ctype in cols)
            con.execute(
                f"COPY (SELECT {empty_select} WHERE 1=0) "
                f"TO '{tmp_path.as_posix()}' (FORMAT PARQUET, COMPRESSION ZSTD)"
            )
            returned_rows = 0
        else:
            iso_list = [layout.iso_date(d) for d in norm_dates]
            placeholders = ", ".join("CAST(? AS DATE)" for _ in iso_list)
            file_list_sql = ", ".join(f"'{f}'" for f in files)
            returned_rows = int(
                con.execute(
                    f"SELECT COUNT(*) FROM read_parquet([{file_list_sql}], union_by_name=true) "
                    f"WHERE date IN ({placeholders})",
                    iso_list,
                ).fetchone()[0]
            )
            con.execute(
                f"COPY ("
                f"  SELECT {col_select} FROM read_parquet([{file_list_sql}], union_by_name=true) "
                f"  WHERE date IN ({placeholders}) "
                f"  ORDER BY date ASC, code ASC"
                f") TO '{tmp_path.as_posix()}' (FORMAT PARQUET, COMPRESSION ZSTD)",
                iso_list,
            )
        payload = tmp_path.read_bytes()
    finally:
        con.close()
        tmp_path.unlink(missing_ok=True)

    return payload, {
        "returned_rows": returned_rows,
        "dates": norm_dates,
        "parquet_sha256": hashlib.sha256(payload).hexdigest(),
    }


def parse_daily_parquet_bytes(
    payload: bytes,
    *,
    staging_dir: Path,
) -> list[tuple[Any, ...]]:
    """解码 `readback_daily_parquet` 返回的 Parquet 二进制体为标准化 21 列元组列表。"""
    import duckdb

    staging_dir = Path(staging_dir)
    staging_dir.mkdir(parents=True, exist_ok=True)
    tmp_path = staging_dir / f"rb_dec_{uuid.uuid4().hex[:12]}.parquet"
    try:
        tmp_path.write_bytes(payload)
        con = duckdb.connect()
        try:
            rows = con.execute(
                f"SELECT {', '.join(DAILY_BASE_COLUMN_NAMES)} FROM read_parquet(?) "
                f"ORDER BY date ASC, code ASC",
                [tmp_path.as_posix()],
            ).fetchall()
        finally:
            con.close()
    finally:
        tmp_path.unlink(missing_ok=True)
    return [_canonical_tuple(r) for r in rows]


def _cell_equal(exp: Any, act: Any, col_type: str) -> bool:
    if exp is None or act is None:
        return exp is None and act is None
    if col_type == "DOUBLE":
        return math.isclose(float(exp), float(act), rel_tol=1e-12, abs_tol=1e-12)
    return exp == act


def compare_daily_rows(
    expected_rows: Sequence[dict[str, Any]],
    actual_tuples: Sequence[Sequence[Any]],
) -> dict[str, Any]:
    """逐 `(date, code)` 逐列比对期望日K行与 DuckDB/Parquet 读回元组，严格 0 差异通过。"""
    norm_exp, _ = sink._normalize_rows(list(expected_rows), DAILY_BASE_COLUMNS)
    exp_map: dict[tuple[str, str], tuple[Any, ...]] = {}
    for row in norm_exp:
        ct = _canonical_tuple(row)
        exp_map[(str(ct[1]), str(ct[0]))] = ct

    act_map: dict[tuple[str, str], tuple[Any, ...]] = {}
    duplicate_actual = 0
    for row in actual_tuples:
        ct = _canonical_tuple(row)
        k = (str(ct[1]), str(ct[0]))
        if k in act_map:
            duplicate_actual += 1
        act_map[k] = ct

    missing_keys = sorted(set(exp_map) - set(act_map))
    extra_keys = sorted(set(act_map) - set(exp_map))

    mismatched_values = 0
    sample_mismatches: list[dict[str, Any]] = []
    for k in sorted(set(exp_map) & set(act_map)):
        e_tup = exp_map[k]
        a_tup = act_map[k]
        bad_fields: list[str] = []
        for idx, (col_name, col_type) in enumerate(DAILY_BASE_COLUMNS):
            if not _cell_equal(e_tup[idx], a_tup[idx], col_type):
                bad_fields.append(col_name)
        if bad_fields:
            mismatched_values += 1
            if len(sample_mismatches) < 10:
                sample_mismatches.append({
                    "date": k[0],
                    "code": k[1],
                    "fields": bad_fields,
                })

    passed = (
        not missing_keys
        and not extra_keys
        and mismatched_values == 0
        and duplicate_actual == 0
    )
    return {
        "passed": passed,
        "expected_rows": len(exp_map),
        "actual_rows": len(act_map),
        "missing_rows": len(missing_keys),
        "extra_rows": len(extra_keys),
        "mismatched_values": mismatched_values,
        "duplicate_rows": duplicate_actual,
        "sample_missing": missing_keys[:10],
        "sample_extra": extra_keys[:10],
        "sample_mismatches": sample_mismatches,
    }


def _sorted_source_dirs_for_daily(db_dirs: Sequence[Path]) -> list[Path]:
    return sorted(
        db_dirs,
        key=lambda d: (_SOURCE_PRIORITY.get(d.name, 0), d.name),
    )


def iter_leveldb_kdaily_month_batches(
    snapshot_dirs: Sequence[Path],
    *,
    staging_dir: Path,
    snapshot_id: str,
    key_filter: leveldb_snapshot.KeyScopeFilter | None = None,
    code_predicate: Callable[[str], bool] | None = None,
    verify_checksums: bool = True,
) -> tuple[list[tuple[str, Path, dict[str, Any], list[dict[str, Any]]]], dict[str, Any]]:
    """线性扫描 LevelDB `k日k:` 并按完整自然月（`YYYYMM`）落临时桶并生成标准 21 列 Parquet 批次。

    `snapshot_dirs` 按来源优先级合并（如 `data1` 覆盖 `data`），保证每个自然月批次
    均囊括该月全部交易日及全市场目标股票，绝不因 LevelDB `code` 主序导致半个分区落盘。
    """
    staging_dir = Path(staging_dir)
    bucket_dir = staging_dir / "buckets"
    batch_dir = staging_dir / "parquet"
    shutil.rmtree(bucket_dir, ignore_errors=True)
    shutil.rmtree(batch_dir, ignore_errors=True)
    bucket_dir.mkdir(parents=True, exist_ok=True)
    batch_dir.mkdir(parents=True, exist_ok=True)

    kdaily_filter = leveldb_snapshot.KeyScopeFilter(
        since_date=key_filter.since_date if key_filter else None,
        until_date=key_filter.until_date if key_filter else None,
        include_prefixes=(b"k\xe6\x97\xa5k:",),
        exclude_prefixes=(),
        include_undated=False,
        max_rows=key_filter.max_rows if key_filter else None,
    )

    ordered_dirs = _sorted_source_dirs_for_daily(snapshot_dirs)
    ram_buffers: dict[str, list[str]] = {}
    ram_count = 0
    month_files: dict[str, Path] = {}
    scanned_rows = 0
    filtered_non_target = 0

    def _flush_ram() -> None:
        nonlocal ram_count
        for ym, lines in ram_buffers.items():
            if not lines:
                continue
            bp = bucket_dir / f"{ym}.ndjson"
            month_files[ym] = bp
            with bp.open("a", encoding="utf-8") as fh:
                fh.writelines(lines)
        ram_buffers.clear()
        ram_count = 0

    for db_priority, db_dir in enumerate(ordered_dirs):
        snap = leveldb_snapshot.open_leveldb_snapshot(db_dir, verify_checksums=verify_checksums)
        for rk, rv in snap.iter_entries(key_filter=kdaily_filter):
            scanned_rows += 1
            parsed = parse_kdaily_entry(rk, rv, code_predicate=code_predicate)
            if parsed is None:
                filtered_non_target += 1
                continue
            code, date8, row_dict = parsed
            ym = date8[:6]
            rec = {"_p": db_priority, "row": row_dict}
            ram_buffers.setdefault(ym, []).append(
                json.dumps(rec, ensure_ascii=False, separators=(",", ":")) + "\n"
            )
            ram_count += 1
            if ram_count >= 50000:
                _flush_ram()
    _flush_ram()

    batches: list[tuple[str, Path, dict[str, Any], list[dict[str, Any]]]] = []
    source_label = "__".join(d.name for d in ordered_dirs) or "leveldb"
    for ym in sorted(month_files):
        bp = month_files[ym]
        merged: dict[tuple[str, str], tuple[int, dict[str, Any]]] = {}
        with bp.open("r", encoding="utf-8") as fh:
            for line in fh:
                item = json.loads(line)
                prio = int(item["_p"])
                r = item["row"]
                key = (str(r["date"]), str(r["code"]))
                prev = merged.get(key)
                if prev is None or prio >= prev[0]:
                    merged[key] = (prio, r)
        bp.unlink(missing_ok=True)
        if not merged:
            continue
        month_rows = [merged[k][1] for k in sorted(merged)]
        batch_id = f"daily_{ym}"
        pq_path = batch_dir / f"batch_{batch_id}.parquet"
        meta = write_daily_backfill_batch_parquet(
            pq_path,
            month_rows,
            snapshot_id=snapshot_id,
            source_db=source_label,
            batch_id=batch_id,
        )
        batches.append((ym, pq_path, meta, month_rows))

    shutil.rmtree(bucket_dir, ignore_errors=True)
    return batches, {
        "scanned_kdaily_rows": scanned_rows,
        "filtered_non_stock_rows": filtered_non_target,
        "month_batches": len(batches),
    }


def backfill_daily_from_leveldb_warehouse(
    root: Path,
    *,
    source_dbs: Sequence[str] | None = None,
    dates: Sequence[str] | None = None,
    code_predicate: Callable[[str], bool] | None = None,
    is_trading_day_fn: Callable[[dt_mod.date], bool] | None = None,
    overwrite_existing: bool = False,
    batch_days: int = 20,
    verify_readback: bool = True,
    refresh_views_cb: Callable[[], None] | None = None,
) -> dict[str, Any]:
    """将已导入 NAS `facts/leveldb_raw` 的 `k日k` 数据回填补齐到标准 `facts/daily` 并验证 0 差异。"""
    root = Path(root)
    kdaily_pfx = "k日k:".encode("utf-8")
    kdaily_upper = leveldb_snapshot._prefix_upper_bound(kdaily_pfx)
    target_dates_filter = {layout.normalize_date(d) for d in dates} if dates else None

    with bulk_import._BULK_LOCK:
        con = catalog._connect(root)
        try:
            if not any(layout.leveldb_raw_dir(root).rglob("*.parquet")):
                return {
                    "ok": True,
                    "passed": True,
                    "total_source_dates": 0,
                    "written_dates": [],
                    "skipped_dates": [],
                    "written_rows": 0,
                    "filtered_non_stock_rows": 0,
                    "weeks_written": [],
                    "months_written": [],
                    "verify": {"passed": True, "checked_dates": 0, "checked_rows": 0},
                }
            bulk_import.ensure_leveldb_views(con, root)

            src_clause = ""
            params: list[Any] = [kdaily_pfx, kdaily_upper]
            if source_dbs:
                s_placeholders = ", ".join("?" for _ in source_dbs)
                src_clause = f" AND source_db IN ({s_placeholders})"
                params.extend(list(source_dbs))

            date_rows = con.execute(
                f"SELECT DISTINCT substr(key_utf8, length(key_utf8) - 7, 8) AS d8 "
                f"FROM v_leveldb_latest "
                f"WHERE raw_key >= ?::BLOB AND raw_key < ?::BLOB "
                f"AND key_utf8 IS NOT NULL{src_clause} "
                f"ORDER BY d8 ASC",
                params,
            ).fetchall()

            all_dates = [
                str(r[0]) for r in date_rows
                if r[0] and len(str(r[0])) == 8 and str(r[0]).isdigit()
            ]
            if target_dates_filter is not None:
                all_dates = [d for d in all_dates if d in target_dates_filter]

            if not all_dates:
                return {
                    "ok": True,
                    "passed": True,
                    "total_source_dates": 0,
                    "written_dates": [],
                    "skipped_dates": [],
                    "written_rows": 0,
                    "filtered_non_stock_rows": 0,
                    "weeks_written": [],
                    "months_written": [],
                    "verify": {"passed": True, "checked_dates": 0, "checked_rows": 0},
                }

            series = load_factor_series(
                root,
                include_leveldb_adjust=True,
                code_predicate=code_predicate,
            )

            written_dates: list[str] = []
            skipped_dates: list[str] = []
            written_rows = 0
            filtered_non_stock = 0
            checked_rows = 0
            chunk_size = max(1, int(batch_days))

            for offset in range(0, len(all_dates), chunk_size):
                chunk_dates = all_dates[offset : offset + chunk_size]
                chunk_pending = []
                for d8 in chunk_dates:
                    has_sh = layout.daily_partition(root, d8, "sh").exists()
                    has_sz = layout.daily_partition(root, d8, "sz").exists()
                    if not overwrite_existing and has_sh and has_sz:
                        skipped_dates.append(d8)
                    else:
                        chunk_pending.append(d8)
                if not chunk_pending:
                    continue

                d_placeholders = ", ".join("?" for _ in chunk_pending)
                q_params: list[Any] = [kdaily_pfx, kdaily_upper, *chunk_pending]
                if source_dbs:
                    s_placeholders = ", ".join("?" for _ in source_dbs)
                    q_src_clause = f" AND source_db IN ({s_placeholders})"
                    q_params.extend(list(source_dbs))
                else:
                    q_src_clause = ""

                # source_db 优先级：data1 > data > mydb；同库按 snapshot_id 升序覆盖取最新
                kv_rows = con.execute(
                    f"SELECT source_db, snapshot_id, raw_key, raw_value "
                    f"FROM v_leveldb_raw "
                    f"WHERE raw_key >= ?::BLOB AND raw_key < ?::BLOB "
                    f"AND key_utf8 IS NOT NULL "
                    f"AND substr(key_utf8, length(key_utf8) - 7, 8) IN ({d_placeholders})"
                    f"{q_src_clause}",
                    q_params,
                ).fetchall()

                # 按 (source_priority, snapshot_id) 覆盖合并 (date8, code)
                dedup_by_date: dict[str, dict[str, tuple[int, str, dict[str, Any]]]] = {}
                for src_db_val, snap_id_val, rk, rv in kv_rows:
                    parsed = parse_kdaily_entry(
                        bytes(rk),
                        bytes(rv),
                        code_predicate=code_predicate,
                    )
                    if parsed is None:
                        filtered_non_stock += 1
                        continue
                    code, d8, row_dict = parsed
                    rank = (_SOURCE_PRIORITY.get(str(src_db_val), 0), str(snap_id_val))
                    day_map = dedup_by_date.setdefault(d8, {})
                    prev = day_map.get(code)
                    if prev is None or rank >= (prev[0], prev[1]):
                        day_map[code] = (rank[0], rank[1], row_dict)

                expected_chunk_rows: list[dict[str, Any]] = []
                chunk_written_dates: list[str] = []
                for d8 in chunk_pending:
                    code_map = dedup_by_date.get(d8) or {}
                    if not code_map:
                        continue
                    day_rows = [code_map[c][2] for c in sorted(code_map)]
                    fmap = factor_map_at(series, d8)
                    if overwrite_existing:
                        for mkt in {layout.market_of(str(r["code"])) for r in day_rows}:
                            layout.daily_partition(root, d8, mkt).unlink(missing_ok=True)
                    res = sink.write_daily(
                        root, d8, day_rows, factor_map=fmap, advance_watermark=False
                    )
                    if res.get("status") == "written":
                        written_dates.append(d8)
                        chunk_written_dates.append(d8)
                        written_rows += int(res.get("rows") or 0)
                        expected_chunk_rows.extend(day_rows)
                    else:
                        skipped_dates.append(d8)

                if verify_readback and chunk_written_dates:
                    rb_bytes, _ = readback_daily_parquet(root, dates=chunk_written_dates)
                    rb_tuples = parse_daily_parquet_bytes(
                        rb_bytes,
                        staging_dir=root / ".staging" / "daily_verify",
                    )
                    cmp_res = compare_daily_rows(expected_chunk_rows, rb_tuples)
                    if not cmp_res["passed"]:
                        raise RuntimeError(
                            f"日线回填逐字段对账失败 (dates={chunk_written_dates[:3]}..): {cmp_res}"
                        )
                    checked_rows += cmp_res["actual_rows"]
        finally:
            con.close()

        cascade = cascade_week_and_month_for_dates(
            root,
            written_dates,
            is_trading_day_fn=is_trading_day_fn,
            advance_watermark=False,
        )
        changed = bool(written_dates or cascade["weeks_written"] or cascade["months_written"])
        if refresh_views_cb is not None and changed:
            refresh_views_cb()
        if changed:
            inventory.record_inventory_event(
                root,
                "daily_backfill",
                summary_prefix="日线回填标准层：",
            )

    return {
        "ok": True,
        "passed": True,
        "total_source_dates": len(all_dates),
        "written_dates": written_dates,
        "skipped_dates": skipped_dates,
        "written_rows": written_rows,
        "filtered_non_stock_rows": filtered_non_stock,
        "weeks_written": cascade["weeks_written"],
        "months_written": cascade["months_written"],
        "verify": {
            "passed": True,
            "checked_dates": len(written_dates),
            "checked_rows": checked_rows,
        },
    }


def _namespace_prefix_bounds(namespaces: Sequence[str]) -> list[tuple[bytes, bytes | None]]:
    bounds: list[tuple[bytes, bytes | None]] = []
    for ns in namespaces:
        text = str(ns or "").strip()
        if not text:
            continue
        if not text.endswith(":"):
            text = f"{text}:"
        pfx = text.encode("utf-8")
        bounds.append((pfx, leveldb_snapshot._prefix_upper_bound(pfx)))
    return bounds


def _exclude_sql_predicate(bounds: Sequence[tuple[bytes, bytes | None]]) -> tuple[str, list[Any]]:
    clauses: list[str] = []
    params: list[Any] = []
    for pfx, upper in bounds:
        if upper is None:
            clauses.append("(raw_key >= ?::BLOB)")
            params.append(pfx)
        else:
            clauses.append("(raw_key >= ?::BLOB AND raw_key < ?::BLOB)")
            params.extend([pfx, upper])
    return "(" + " OR ".join(clauses) + ")", params


def prune_leveldb_namespace(
    root: Path,
    *,
    namespaces: Sequence[str] = ("k日k",),
    batch_size: int = 50000,
    refresh_views_cb: Callable[[], None] | None = None,
    event_trigger: str | None = "dedup_daily",
    event_summary_prefix: str = "原库去重裁剪：",
) -> dict[str, Any]:
    """从 `facts/leveldb_raw` 无损剔除已规范化表（如 `k日k`），保持 `bulk_import` 校验全绿。

    策略：
    - 前序整批无匹配行时零重写直接保留（`k分钟k:` 位于 UTF-8 `k日k:` 之前，可 100% 跳过重写）；
    - 从首个包含目标前缀的批次起，顺序读取剩余存活 `(raw_key, raw_value)` 并按 `batch_size`
      重新打包（重置连续 `batch_index` / `seq_no` 及校验摘要）；
    - 原子替换文件与目录元数据，重算 `bulk_import_type_stats`，最后跑 `verify_bulk_import`
      自检并通过 `warehouse_inventory_history` 记录去重裁剪审计。
    """
    root = Path(root)
    bounds = _namespace_prefix_bounds(namespaces)
    if not bounds:
        raise bulk_import.BulkImportError("namespaces 不能为空")

    where_match_sql, match_params = _exclude_sql_predicate(bounds)
    total_removed_rows = 0
    per_job: list[dict[str, Any]] = []

    with bulk_import._BULK_LOCK:
        con = catalog._connect(root)
        try:
            jobs = con.execute(
                "SELECT snapshot_id, source_db, status, committed_rows "
                "FROM bulk_import_jobs ORDER BY snapshot_id ASC, source_db ASC"
            ).fetchall()

            for snapshot_id, source_db, job_status, old_rows in jobs:
                batch_rows = con.execute(
                    "SELECT batch_id, batch_index, seq_start, seq_end, row_count, "
                    "file_size, parquet_sha256, rows_digest_sha256, first_key_sha256, "
                    "last_key_sha256, parquet_path, committed_at "
                    "FROM bulk_import_batches "
                    "WHERE snapshot_id = ? AND source_db = ? "
                    "ORDER BY batch_index ASC",
                    [snapshot_id, source_db],
                ).fetchall()
                if not batch_rows:
                    continue

                first_hit_idx: int | None = None
                job_removed = 0
                for idx, brow in enumerate(batch_rows):
                    ppath = Path(brow[10])
                    if not ppath.is_file():
                        continue
                    cnt = int(
                        con.execute(
                            f"SELECT COUNT(*) FILTER (WHERE {where_match_sql}) "
                            f"FROM read_parquet(?)",
                            [*match_params, ppath.as_posix()],
                        ).fetchone()[0]
                    )
                    if cnt > 0:
                        job_removed += cnt
                        if first_hit_idx is None:
                            first_hit_idx = idx

                if first_hit_idx is None or job_removed == 0:
                    continue

                prefix_kept = list(batch_rows[:first_hit_idx])
                tail_batches = list(batch_rows[first_hit_idx:])
                next_batch_idx = len(prefix_kept)
                next_seq = (int(prefix_kept[-1][3]) + 1) if prefix_kept else 0

                stage_dir = root / ".staging" / "leveldb_prune" / snapshot_id / source_db
                shutil.rmtree(stage_dir, ignore_errors=True)
                stage_dir.mkdir(parents=True, exist_ok=True)
                new_tail_records: list[dict[str, Any]] = []
                now = _now_iso()
                buf: list[tuple[bytes, bytes]] = []

                def _flush_buf() -> None:
                    nonlocal next_batch_idx, next_seq
                    if not buf:
                        return
                    bid = f"b{next_batch_idx:06d}"
                    stage_pq = stage_dir / f"batch_{bid}.parquet"
                    meta = bulk_import.write_leveldb_batch_parquet(
                        stage_pq,
                        snapshot_id=snapshot_id,
                        source_db=source_db,
                        batch_id=bid,
                        start_seq=next_seq,
                        items=buf,
                    )
                    dest_pq = layout.leveldb_batch_partition(root, snapshot_id, source_db, bid)
                    new_tail_records.append(
                        {
                            "batch_id": bid,
                            "batch_index": next_batch_idx,
                            "seq_start": meta["seq_start"],
                            "seq_end": meta["seq_end"],
                            "row_count": meta["row_count"],
                            "file_size": meta["file_size"],
                            "parquet_sha256": meta["parquet_sha256"],
                            "rows_digest_sha256": meta["rows_digest_sha256"],
                            "first_key_sha256": meta["first_key_sha256"],
                            "last_key_sha256": meta["last_key_sha256"],
                            "stage_path": stage_pq,
                            "dest_path": dest_pq,
                            "committed_at": now,
                        }
                    )
                    next_batch_idx += 1
                    next_seq = int(meta["seq_end"]) + 1
                    buf.clear()

                for brow in tail_batches:
                    ppath = Path(brow[10])
                    surviving = con.execute(
                        f"SELECT raw_key, raw_value FROM read_parquet(?) "
                        f"WHERE NOT ({where_match_sql}) ORDER BY seq_no ASC",
                        [ppath.as_posix(), *match_params],
                    ).fetchall()
                    for rk, rv in surviving:
                        buf.append((bytes(rk), bytes(rv)))
                        if len(buf) >= batch_size:
                            _flush_buf()
                _flush_buf()

                # 先重命名受影响的旧分区为 .bak，再将新批移入并原子提交数据库目录
                bak_pairs: list[tuple[Path, Path]] = []
                moved_new: list[Path] = []
                try:
                    for brow in tail_batches:
                        old_p = Path(brow[10])
                        if old_p.is_file():
                            bak_p = old_p.with_name(f"{old_p.name}.{uuid.uuid4().hex[:8]}.bak")
                            os.replace(old_p, bak_p)
                            bak_pairs.append((old_p, bak_p))
                    for rec in new_tail_records:
                        rec["dest_path"].parent.mkdir(parents=True, exist_ok=True)
                        os.replace(rec["stage_path"], rec["dest_path"])
                        moved_new.append(rec["dest_path"])

                    final_batches: list[tuple[Any, ...]] = []
                    for brow in prefix_kept:
                        final_batches.append(brow)
                    for rec in new_tail_records:
                        final_batches.append(
                            (
                                rec["batch_id"],
                                rec["batch_index"],
                                rec["seq_start"],
                                rec["seq_end"],
                                rec["row_count"],
                                rec["file_size"],
                                rec["parquet_sha256"],
                                rec["rows_digest_sha256"],
                                rec["first_key_sha256"],
                                rec["last_key_sha256"],
                                str(rec["dest_path"]),
                                rec["committed_at"],
                            )
                        )

                    new_total_batches = len(final_batches)
                    new_total_rows = sum(int(b[4]) for b in final_batches)
                    new_total_bytes = sum(int(b[5]) for b in final_batches)
                    new_last_seq_end = max((int(b[3]) for b in final_batches), default=-1)

                    con.execute("BEGIN")
                    con.execute(
                        "DELETE FROM bulk_import_batches WHERE snapshot_id = ? AND source_db = ?",
                        [snapshot_id, source_db],
                    )
                    for fb in final_batches:
                        con.execute(
                            "INSERT INTO bulk_import_batches "
                            "(snapshot_id, source_db, batch_id, batch_index, seq_start, seq_end, "
                            "row_count, file_size, parquet_sha256, rows_digest_sha256, "
                            "first_key_sha256, last_key_sha256, parquet_path, committed_at) "
                            "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
                            [snapshot_id, source_db, *fb],
                        )
                    con.execute(
                        "UPDATE bulk_import_jobs SET "
                        "expected_batches = ?, committed_batches = ?, committed_rows = ?, "
                        "committed_bytes = ?, last_seq_end = ?, updated_at = ? "
                        "WHERE snapshot_id = ? AND source_db = ?",
                        [
                            new_total_batches,
                            new_total_batches,
                            new_total_rows,
                            new_total_bytes,
                            new_last_seq_end,
                            now,
                            snapshot_id,
                            source_db,
                        ],
                    )
                    con.execute(
                        "DELETE FROM bulk_import_type_stats WHERE snapshot_id = ? AND source_db = ?",
                        [snapshot_id, source_db],
                    )
                    # 重新基于保留 Parquet 精确重建该任务的 type_stats
                    merged_stats: dict[str, dict[str, Any]] = {}
                    for fb in final_batches:
                        p_str = str(fb[10]).replace("\\", "/")
                        t_rows = con.execute(
                            "SELECT raw_key, key_len + value_len FROM read_parquet(?)",
                            [p_str],
                        ).fetchall()
                        chunk = inventory.accumulate_batch_type_stats(
                            [(bytes(rk), int(rl)) for rk, rl in t_rows]
                        )
                        for tk, item in chunk.items():
                            cur = merged_stats.get(tk)
                            if cur is None:
                                merged_stats[tk] = dict(item)
                            else:
                                cur["row_count"] += item["row_count"]
                                cur["raw_bytes"] += item["raw_bytes"]
                                if item["date_min"] and (cur["date_min"] is None or item["date_min"] < cur["date_min"]):
                                    cur["date_min"] = item["date_min"]
                                if item["date_max"] and (cur["date_max"] is None or item["date_max"] > cur["date_max"]):
                                    cur["date_max"] = item["date_max"]
                    if merged_stats:
                        inventory.upsert_bulk_type_stats(con, snapshot_id, source_db, merged_stats, now)
                    con.execute("COMMIT")
                except Exception:
                    try:
                        con.execute("ROLLBACK")
                    except Exception:
                        pass
                    for mp in moved_new:
                        mp.unlink(missing_ok=True)
                    for old_p, bak_p in bak_pairs:
                        if bak_p.is_file():
                            os.replace(bak_p, old_p)
                    raise
                finally:
                    shutil.rmtree(stage_dir, ignore_errors=True)

                for _, bak_p in bak_pairs:
                    bak_p.unlink(missing_ok=True)

                total_removed_rows += job_removed
                per_job.append(
                    {
                        "snapshot_id": snapshot_id,
                        "source_db": source_db,
                        "before_rows": int(old_rows),
                        "after_rows": new_total_rows,
                        "removed_rows": job_removed,
                        "prefix_batches_untouched": len(prefix_kept),
                        "tail_batches_rewritten": len(new_tail_records),
                    }
                )
        finally:
            con.close()

        verify_results: list[dict[str, Any]] = []
        all_passed = True
        for item in per_job:
            vres = bulk_import.verify_bulk_import(
                root,
                snapshot_id=item["snapshot_id"],
                source_db=item["source_db"],
                update_status=True,
            )
            item["verify"] = vres
            verify_results.append(vres)
            if not vres.get("passed"):
                all_passed = False

        if refresh_views_cb is not None and per_job:
            refresh_views_cb()
        if per_job and event_trigger:
            inventory.record_inventory_event(
                root,
                event_trigger,
                summary_prefix=event_summary_prefix,
            )

    return {
        "ok": True,
        "passed": all_passed,
        "namespaces": list(namespaces),
        "removed_rows": total_removed_rows,
        "updated_jobs": per_job,
    }
