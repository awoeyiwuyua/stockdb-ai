"""Vectorized LevelDB -> Parquet 1m minute-bar backfill, verification, and safe pruning."""

from __future__ import annotations

import hashlib
import json
import math
import os
import re
import shutil
import uuid
from contextlib import contextmanager
from datetime import date as dt_date, datetime as dt_datetime, timezone
from pathlib import Path
from typing import Any, Callable, Iterable, Iterator, Sequence

import duckdb

from . import bulk_import, catalog, daily_backfill, inventory, layout, sink

KMINUTE_NAMESPACE = "k分钟k"
KMINUTE_PREFIX = "k分钟k:"
DEFAULT_PERIOD = "1m"
DEFAULT_ALLOWED_MARKETS: tuple[str, ...] = ("sh", "sz")
_VALUE_TEXT_TRUNCATION_LIMIT = 4096
_DEFAULT_BATCH_FETCH = 50000

_PREFIX_LOWER_BYTES, _PREFIX_UPPER_BYTES = daily_backfill._namespace_prefix_bounds(
    (KMINUTE_NAMESPACE,)
)[0]
_PREFIX_LOWER_HEX = _PREFIX_LOWER_BYTES.hex()
_PREFIX_UPPER_HEX = _PREFIX_UPPER_BYTES.hex()

_MINUTE_JSON_SPEC = (
    '{"code":"VARCHAR","date":"VARCHAR",'
    '"open":"DOUBLE","high":"DOUBLE","low":"DOUBLE",'
    '"close":"DOUBLE","volume":"DOUBLE","amount":"DOUBLE"}'
)

_FAST_PATH_PREDICATE_SQL = (
    "("
    "key_utf8 IS NOT NULL "
    "AND starts_with(key_utf8, 'k分钟k:') "
    "AND length(split_part(key_utf8, ':', 2)) > 0 "
    "AND regexp_matches(split_part(key_utf8, ':', 3), '^[0-9]{14}$') "
    "AND value_text IS NOT NULL "
    "AND value_kind IN ('msgpack', 'json') "
    f"AND length(value_text) < {_VALUE_TEXT_TRUNCATION_LIMIT} "
    "AND starts_with(ltrim(value_text), '{') "
    "AND json_valid(value_text) "
    f"AND try(from_json(value_text, '{_MINUTE_JSON_SPEC}')) IS NOT NULL"
    ")"
)

_SOURCE_PRIORITY = dict(daily_backfill._SOURCE_PRIORITY)
_CHECKPOINT_FILENAME = ".minute_backfill_checkpoint.json"
_MONTH_RE = re.compile(r"^[0-9]{6}$")
_DATE8_RE = re.compile(r"^[0-9]{8}$")
_EXPECTED_MINUTE_COL_NAMES = tuple(col for col, _ in sink._MINUTE_COLUMNS)


def _now_iso() -> str:
    return dt_datetime.now(timezone.utc).isoformat(timespec="seconds")


def _sql_quote_path(path: Path) -> str:
    return str(path).replace("\\", "/").replace("'", "''")


def _sql_quote_str(text: str) -> str:
    return str(text).replace("'", "''")


def _cleanup_dir_and_empty_staging(path: Path, root: Path) -> None:
    shutil.rmtree(path, ignore_errors=True)
    staging = Path(root) / ".staging"
    try:
        if staging.is_dir() and not any(staging.iterdir()):
            staging.rmdir()
    except OSError:
        pass


def _normalize_markets(
    allowed_markets: tuple[str, ...] | Iterable[str] | None,
) -> tuple[str, ...]:
    if allowed_markets is None:
        return DEFAULT_ALLOWED_MARKETS
    seen: list[str] = []
    for item in allowed_markets:
        m = str(item or "").strip().lower()
        if m and m not in seen:
            seen.append(m)
    return tuple(seen)


def _normalize_source_dbs(source_dbs: Sequence[str] | None) -> tuple[str, ...] | None:
    if source_dbs is None:
        return None
    out: list[str] = []
    for s in source_dbs:
        text = str(s or "").strip()
        if text and text not in out:
            out.append(text)
    return tuple(out)


def _normalize_months(months: Sequence[str] | None) -> list[str] | None:
    if months is None:
        return None
    out: list[str] = []
    for item in months:
        digits = "".join(ch for ch in str(item or "").strip() if ch.isdigit())
        if len(digits) == 8:
            digits = digits[:6]
        if not _MONTH_RE.match(digits):
            raise ValueError(f"非法月份格式（应为 YYYYMM）: {item!r}")
        if digits not in out:
            out.append(digits)
    out.sort()
    return out


def _safe_float_finite(value: Any) -> float | None:
    val = daily_backfill._safe_float(value)
    if val is None or not math.isfinite(val):
        return None
    return val


def _parse_datetime_any(value: Any) -> dt_datetime | None:
    if value is None:
        return None
    if isinstance(value, dt_datetime):
        return value.replace(tzinfo=None)
    if isinstance(value, dt_date):
        return None
    text = str(value).strip()
    if not text:
        return None
    if len(text) == 14 and text.isdigit():
        try:
            return dt_datetime.strptime(text, "%Y%m%d%H%M%S")
        except ValueError:
            return None
    for fmt in (
        "%Y-%m-%d %H:%M:%S",
        "%Y-%m-%dT%H:%M:%S",
        "%Y-%m-%d %H:%M:%S.%f",
        "%Y-%m-%dT%H:%M:%S.%f",
        "%Y/%m/%d %H:%M:%S",
    ):
        try:
            return dt_datetime.strptime(text, fmt)
        except ValueError:
            continue
    return None


def parse_kminute_key(
    raw_key_or_utf8: bytes | bytearray | memoryview | str | None,
) -> tuple[str, str] | None:
    """解析 `k分钟k:<code>:<14-char YYYYMMDDHHMMSS>` 键，返回 `(code, ts14)`。"""
    if raw_key_or_utf8 is None:
        return None
    if isinstance(raw_key_or_utf8, (bytes, bytearray, memoryview)):
        try:
            text = bytes(raw_key_or_utf8).decode("utf-8")
        except UnicodeDecodeError:
            return None
    else:
        text = str(raw_key_or_utf8)
    if not text.startswith(KMINUTE_PREFIX):
        return None
    parts = text.split(":")
    if len(parts) < 2:
        return None
    code = parts[1].strip()
    ts_part = parts[2].strip() if len(parts) >= 3 else ""
    if not code and not ts_part:
        return None
    return code, ts_part


def _normalize_payload_dict(payload: Any) -> dict[str, Any] | None:
    if not isinstance(payload, dict):
        return None
    if all(isinstance(k, str) for k in payload.keys()):
        return payload
    out: dict[str, Any] = {}
    for k, v in payload.items():
        if isinstance(k, (bytes, bytearray, memoryview)):
            out[bytes(k).decode("utf-8", errors="ignore")] = v
        else:
            out[str(k)] = v
    return out


def _source_where_clause(
    source_dbs: tuple[str, ...] | None, *, alias: str = ""
) -> tuple[str, list[Any]]:
    pfx = f"{alias}." if alias else ""
    base = (
        f"{pfx}raw_key >= unhex('{_PREFIX_LOWER_HEX}') "
        f"AND {pfx}raw_key < unhex('{_PREFIX_UPPER_HEX}')"
    )
    if source_dbs is None:
        return base, []
    if not source_dbs:
        return f"{base} AND 1 = 0", []
    placeholders = ", ".join("?" for _ in source_dbs)
    return f"{base} AND {pfx}source_db IN ({placeholders})", list(source_dbs)


def _inspect_catalog_batches(
    root: Path,
    *,
    source_dbs: tuple[str, ...] | None,
) -> tuple[list[str] | None, dict[tuple[str, str, str], Path]]:
    """Return `(narrowed_kminute_files_or_None, batch_path_map)`.

    Only restricts LevelDB parquet files via `bulk_import_type_stats` / `bulk_import_batches`
    when every active job has populated type_stats and all parquet files under
    `facts/leveldb_raw` match registered batches; otherwise returns `None` to scan `v_leveldb_raw`.
    """
    batch_path_map: dict[tuple[str, str, str], Path] = {}
    if not layout.duckdb_path(root).is_file():
        return None, batch_path_map

    cat_con = catalog._connect(root)
    try:
        job_rows = cat_con.execute(
            "SELECT snapshot_id, source_db, COALESCE(committed_rows, 0) FROM bulk_import_jobs"
        ).fetchall()
        stats_jobs = {
            (str(r[0]), str(r[1]))
            for r in cat_con.execute(
                "SELECT DISTINCT snapshot_id, source_db FROM bulk_import_type_stats"
            ).fetchall()
        }
        kminute_jobs = {
            (str(r[0]), str(r[1]))
            for r in cat_con.execute(
                "SELECT DISTINCT snapshot_id, source_db FROM bulk_import_type_stats "
                "WHERE type_key = ? AND row_count > 0",
                [KMINUTE_NAMESPACE],
            ).fetchall()
        }
        batch_rows = cat_con.execute(
            "SELECT snapshot_id, source_db, batch_id, parquet_path FROM bulk_import_batches"
        ).fetchall()
    except duckdb.Error:
        return None, batch_path_map
    finally:
        cat_con.close()

    registered_resolved: set[Path] = set()
    selected_paths: list[str] = []
    for snap_id, sdb, bid, p_str in batch_rows:
        snap = str(snap_id)
        db_name = str(sdb)
        p = Path(str(p_str))
        batch_path_map[(snap, db_name, str(bid))] = p
        if p.is_file():
            registered_resolved.add(p.resolve())
            if (snap, db_name) in kminute_jobs and (
                source_dbs is None or db_name in source_dbs
            ):
                selected_paths.append(_sql_quote_path(p))

    non_empty_jobs = {(str(s), str(d)) for s, d, rows in job_rows if int(rows) > 0}
    if not non_empty_jobs or not non_empty_jobs.issubset(stats_jobs):
        return None, batch_path_map

    raw_dir = layout.leveldb_raw_dir(root)
    disk_resolved = (
        {p.resolve() for p in raw_dir.rglob("*.parquet") if p.is_file()}
        if raw_dir.is_dir()
        else set()
    )
    if disk_resolved != registered_resolved:
        return None, batch_path_map

    return selected_paths, batch_path_map


@contextmanager
def _work_connection(
    root: Path,
    *,
    source_dbs: tuple[str, ...] | None = None,
) -> Iterator[tuple[duckdb.DuckDBPyConnection, dict[tuple[str, str, str], Path]]]:
    """Open DuckDB work connection with `.staging` spill `temp_directory` & narrowed `_kminute_raw`."""
    root = Path(root)
    duck_tmp = root / ".staging" / f"minute_ducktmp_{uuid.uuid4().hex[:10]}"
    duck_tmp.mkdir(parents=True, exist_ok=True)
    con = duckdb.connect(":memory:")
    try:
        try:
            con.execute(f"SET temp_directory = '{_sql_quote_path(duck_tmp)}'")
        except duckdb.Error:
            pass
        try:
            con.execute("SET preserve_insertion_order = false")
        except duckdb.Error:
            pass

        bulk_import.ensure_leveldb_views(con, root)
        narrowed_files, batch_path_map = _inspect_catalog_batches(
            root, source_dbs=source_dbs
        )
        if narrowed_files is None:
            con.execute("CREATE OR REPLACE TEMP VIEW _kminute_raw AS SELECT * FROM v_leveldb_raw")
        elif narrowed_files:
            files_sql = ", ".join(f"'{p}'" for p in narrowed_files)
            con.execute(
                f"CREATE OR REPLACE TEMP VIEW _kminute_raw AS "
                f"SELECT * FROM read_parquet([{files_sql}], hive_partitioning=false)"
            )
        else:
            con.execute(
                "CREATE OR REPLACE TEMP VIEW _kminute_raw AS "
                "SELECT * FROM v_leveldb_raw WHERE 1 = 0"
            )
        yield con, batch_path_map
    finally:
        try:
            con.close()
        finally:
            _cleanup_dir_and_empty_staging(duck_tmp, root)


def _source_view_name(con: duckdb.DuckDBPyConnection) -> str:
    row = con.execute(
        "SELECT 1 FROM duckdb_views() WHERE view_name = '_kminute_raw' LIMIT 1"
    ).fetchone()
    return "_kminute_raw" if row is not None else "v_leveldb_raw"


def _evaluate_code_allowed(
    code: str,
    *,
    code_predicate: Callable[[str], bool] | None,
    allowed_markets: tuple[str, ...],
) -> str | None:
    clean = str(code or "").strip()
    if not clean:
        return None
    market = layout.market_of(clean)
    if market not in allowed_markets:
        return None
    if code_predicate is not None and not bool(code_predicate(clean)):
        return None
    return market


def _build_allowed_codes_table(
    con: duckdb.DuckDBPyConnection,
    candidate_codes: Iterable[str],
    *,
    code_predicate: Callable[[str], bool] | None,
    allowed_markets: tuple[str, ...],
) -> dict[str, str]:
    allowed_map: dict[str, str] = {}
    seen: set[str] = set()
    for raw_code in candidate_codes:
        clean = str(raw_code or "").strip()
        if not clean or clean in seen:
            continue
        seen.add(clean)
        market = _evaluate_code_allowed(
            clean,
            code_predicate=code_predicate,
            allowed_markets=allowed_markets,
        )
        if market is not None:
            allowed_map[clean] = market

    con.execute("DROP TABLE IF EXISTS _allowed_codes")
    con.execute("CREATE TEMP TABLE _allowed_codes (code VARCHAR PRIMARY KEY, market VARCHAR)")
    if allowed_map:
        con.executemany(
            "INSERT INTO _allowed_codes (code, market) VALUES (?, ?)",
            sorted(allowed_map.items()),
        )
    return allowed_map


def _probe_and_stage_fallback(
    con: duckdb.DuckDBPyConnection,
    *,
    source_dbs: tuple[str, ...] | None,
    code_predicate: Callable[[str], bool] | None,
    allowed_markets: tuple[str, ...],
    stage_fallback_rows: bool,
) -> dict[str, Any]:
    """Single-pass probe over `k分钟k` plus Python MsgPack/JSON fallback staging."""
    src_view = _source_view_name(con)
    where_sql, params = _source_where_clause(source_dbs)
    grouped_rows = con.execute(
        f"""
        SELECT
            CASE WHEN key_utf8 IS NOT NULL AND starts_with(key_utf8, 'k分钟k:')
                 THEN split_part(key_utf8, ':', 2)
                 ELSE NULL END AS key_code,
            CASE WHEN key_utf8 IS NOT NULL
                      AND starts_with(key_utf8, 'k分钟k:')
                      AND regexp_matches(split_part(key_utf8, ':', 3), '^[0-9]{{14}}$')
                 THEN substr(split_part(key_utf8, ':', 3), 1, 6)
                 ELSE NULL END AS key_ym,
            COUNT(*) AS total_rows,
            COUNT(*) FILTER (WHERE COALESCE({_FAST_PATH_PREDICATE_SQL}, FALSE)) AS fast_path_rows,
            COUNT(*) FILTER (WHERE value_text IS NULL) AS null_value_text_rows,
            COUNT(*) FILTER (
                WHERE value_kind IS NULL OR value_kind NOT IN ('msgpack', 'json')
            ) AS non_json_kind_rows,
            COUNT(*) FILTER (
                WHERE value_text IS NOT NULL AND length(value_text) >= {_VALUE_TEXT_TRUNCATION_LIMIT}
            ) AS truncated_value_text_rows,
            COUNT(*) FILTER (
                WHERE value_text IS NOT NULL
                  AND value_kind IN ('msgpack', 'json')
                  AND length(value_text) < {_VALUE_TEXT_TRUNCATION_LIMIT}
                  AND (
                      NOT starts_with(ltrim(value_text), '{{')
                      OR NOT json_valid(value_text)
                      OR try(from_json(value_text, '{_MINUTE_JSON_SPEC}')) IS NULL
                  )
            ) AS invalid_json_rows,
            COUNT(*) FILTER (
                WHERE key_utf8 IS NULL
                   OR NOT starts_with(key_utf8, 'k分钟k:')
                   OR length(split_part(key_utf8, ':', 2)) = 0
                   OR NOT regexp_matches(split_part(key_utf8, ':', 3), '^[0-9]{{14}}$')
            ) AS invalid_key_rows,
            COUNT(*) FILTER (WHERE NOT COALESCE({_FAST_PATH_PREDICATE_SQL}, FALSE)) AS fallback_rows
        FROM {src_view}
        WHERE {where_sql}
        GROUP BY 1, 2
        """,
        params,
    ).fetchall()

    total_rows = 0
    fast_path_rows = 0
    null_value_text_rows = 0
    non_json_kind_rows = 0
    truncated_value_text_rows = 0
    invalid_json_rows = 0
    invalid_key_rows = 0
    fallback_rows = 0
    candidate_codes: set[str] = set()
    fast_months: set[str] = set()

    for (
        key_code,
        key_ym,
        g_total,
        g_fast,
        g_null_vt,
        g_non_kind,
        g_trunc,
        g_bad_json,
        g_bad_key,
        g_fb,
    ) in grouped_rows:
        total_rows += int(g_total)
        fast_path_rows += int(g_fast)
        null_value_text_rows += int(g_null_vt)
        non_json_kind_rows += int(g_non_kind)
        truncated_value_text_rows += int(g_trunc)
        invalid_json_rows += int(g_bad_json)
        invalid_key_rows += int(g_bad_key)
        fallback_rows += int(g_fb)
        if key_code:
            candidate_codes.add(str(key_code).strip())
        if int(g_fast) > 0 and key_ym and _MONTH_RE.match(str(key_ym)):
            fast_months.add(str(key_ym))

    con.execute("DROP TABLE IF EXISTS _fallback_minute_rows")
    con.execute(
        """
        CREATE TEMP TABLE _fallback_minute_rows (
            source_db VARCHAR,
            snapshot_id VARCHAR,
            batch_id VARCHAR,
            seq_no BIGINT,
            source_priority INTEGER,
            code VARCHAR,
            market VARCHAR,
            ym VARCHAR,
            date8 VARCHAR,
            date DATE,
            dt TIMESTAMP,
            open DOUBLE,
            high DOUBLE,
            low DOUBLE,
            close DOUBLE,
            volume DOUBLE,
            amount DOUBLE
        )
        """
    )

    fallback_staged_rows = 0
    fallback_skipped_rows = 0
    fallback_months: set[str] = set()

    if stage_fallback_rows and fallback_rows > 0:
        cur = con.execute(
            f"""
            SELECT source_db, snapshot_id, batch_id, seq_no, raw_key, key_utf8, raw_value
            FROM {src_view}
            WHERE {where_sql}
              AND NOT COALESCE({_FAST_PATH_PREDICATE_SQL}, FALSE)
            """,
            params,
        )
        batch_buf: list[tuple[Any, ...]] = []
        while True:
            rows = cur.fetchmany(_DEFAULT_BATCH_FETCH)
            if not rows:
                break
            for sdb, snap_id, bid, seq_no, raw_key, key_utf8, raw_val in rows:
                parsed_key = parse_kminute_key(key_utf8 if key_utf8 is not None else raw_key)
                key_code = parsed_key[0] if parsed_key else ""
                key_ts14 = parsed_key[1] if parsed_key else ""

                payload = _normalize_payload_dict(
                    daily_backfill._decode_payload(bytes(raw_val or b""))
                )
                if payload is None:
                    fallback_skipped_rows += 1
                    continue

                eff_code = str(payload.get("code") or key_code or "").strip()
                market = _evaluate_code_allowed(
                    eff_code,
                    code_predicate=code_predicate,
                    allowed_markets=allowed_markets,
                )
                if market is None:
                    fallback_skipped_rows += 1
                    continue

                dt_val = _parse_datetime_any(payload.get("date"))
                if dt_val is None:
                    dt_val = _parse_datetime_any(key_ts14)
                if dt_val is None:
                    fallback_skipped_rows += 1
                    continue

                ym_str = dt_val.strftime("%Y%m")
                date8_str = dt_val.strftime("%Y%m%d")
                candidate_codes.add(eff_code)
                fallback_months.add(ym_str)
                fallback_staged_rows += 1
                batch_buf.append(
                    (
                        str(sdb),
                        str(snap_id),
                        str(bid),
                        int(seq_no),
                        int(_SOURCE_PRIORITY.get(str(sdb), 0)),
                        eff_code,
                        market,
                        ym_str,
                        date8_str,
                        dt_val.date(),
                        dt_val,
                        _safe_float_finite(payload.get("open")),
                        _safe_float_finite(payload.get("high")),
                        _safe_float_finite(payload.get("low")),
                        _safe_float_finite(payload.get("close")),
                        _safe_float_finite(payload.get("volume")),
                        _safe_float_finite(payload.get("amount")),
                    )
                )
                if len(batch_buf) >= 10000:
                    con.executemany(
                        "INSERT INTO _fallback_minute_rows VALUES "
                        "(?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
                        batch_buf,
                    )
                    batch_buf.clear()
        if batch_buf:
            con.executemany(
                "INSERT INTO _fallback_minute_rows VALUES "
                "(?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
                batch_buf,
            )

    allowed_map = _build_allowed_codes_table(
        con,
        candidate_codes,
        code_predicate=code_predicate,
        allowed_markets=allowed_markets,
    )

    active_fast_months: set[str] = set()
    if fast_months and allowed_map:
        r_where_sql, r_params = _source_where_clause(source_dbs, alias="r")
        month_rows = con.execute(
            f"""
            SELECT DISTINCT substr(split_part(r.key_utf8, ':', 3), 1, 6) AS ym
            FROM {src_view} r
            INNER JOIN _allowed_codes ac
                ON split_part(r.key_utf8, ':', 2) = ac.code
            WHERE {r_where_sql}
              AND r.key_utf8 IS NOT NULL
              AND starts_with(r.key_utf8, 'k分钟k:')
              AND regexp_matches(split_part(r.key_utf8, ':', 3), '^[0-9]{{14}}$')
            """,
            r_params,
        ).fetchall()
        for (ym_val,) in month_rows:
            if ym_val and _MONTH_RE.match(str(ym_val)):
                active_fast_months.add(str(ym_val))

    snapshots: list[str] = []
    if total_rows > 0:
        snap_rows = con.execute(
            f"""
            SELECT DISTINCT source_db || ':' || snapshot_id AS tag
            FROM {src_view}
            WHERE {where_sql}
            ORDER BY 1 ASC
            """,
            params,
        ).fetchall()
        snapshots = [str(r[0]) for r in snap_rows]

    all_months = sorted(active_fast_months | fallback_months)
    return {
        "total_rows": total_rows,
        "fast_path_rows": fast_path_rows,
        "fallback_rows": fallback_rows,
        "fallback_staged_rows": fallback_staged_rows,
        "fallback_skipped_rows": fallback_skipped_rows,
        "null_value_text_rows": null_value_text_rows,
        "non_json_kind_rows": non_json_kind_rows,
        "truncated_value_text_rows": truncated_value_text_rows,
        "invalid_json_rows": invalid_json_rows,
        "invalid_key_rows": invalid_key_rows,
        "allowed_code_count": len(allowed_map),
        "allowed_codes": sorted(allowed_map.keys()),
        "months": all_months,
        "snapshots": snapshots,
    }


def probe_minute_completeness(
    root: Path,
    *,
    source_dbs: Sequence[str] | None = None,
    con: duckdb.DuckDBPyConnection | None = None,
) -> dict[str, Any]:
    """Probe `facts/leveldb_raw` completeness for `k分钟k` (`value_text` vs `raw_value` fallback)."""
    root = Path(root)
    clean_sources = _normalize_source_dbs(source_dbs)

    def _run_probe(work_con: duckdb.DuckDBPyConnection) -> dict[str, Any]:
        src_view = _source_view_name(work_con)
        where_sql, params = _source_where_clause(clean_sources)
        row = work_con.execute(
            f"""
            SELECT
                COUNT(*) AS total_rows,
                COUNT(*) FILTER (WHERE COALESCE({_FAST_PATH_PREDICATE_SQL}, FALSE)) AS fast_path_rows,
                COUNT(*) FILTER (WHERE value_text IS NULL) AS null_value_text_rows,
                COUNT(*) FILTER (
                    WHERE value_kind IS NULL OR value_kind NOT IN ('msgpack', 'json')
                ) AS non_json_kind_rows,
                COUNT(*) FILTER (
                    WHERE value_text IS NOT NULL AND length(value_text) >= {_VALUE_TEXT_TRUNCATION_LIMIT}
                ) AS truncated_value_text_rows,
                COUNT(*) FILTER (
                    WHERE value_text IS NOT NULL
                      AND value_kind IN ('msgpack', 'json')
                      AND length(value_text) < {_VALUE_TEXT_TRUNCATION_LIMIT}
                      AND (
                          NOT starts_with(ltrim(value_text), '{{')
                          OR NOT json_valid(value_text)
                          OR try(from_json(value_text, '{_MINUTE_JSON_SPEC}')) IS NULL
                      )
                ) AS invalid_json_rows,
                COUNT(*) FILTER (
                    WHERE key_utf8 IS NULL
                       OR NOT starts_with(key_utf8, 'k分钟k:')
                       OR length(split_part(key_utf8, ':', 2)) = 0
                       OR NOT regexp_matches(split_part(key_utf8, ':', 3), '^[0-9]{{14}}$')
                ) AS invalid_key_rows,
                COUNT(*) FILTER (WHERE NOT COALESCE({_FAST_PATH_PREDICATE_SQL}, FALSE)) AS fallback_rows
            FROM {src_view}
            WHERE {where_sql}
            """,
            params,
        ).fetchone()
        return {
            "total_rows": int(row[0]),
            "fast_path_rows": int(row[1]),
            "null_value_text_rows": int(row[2]),
            "non_json_kind_rows": int(row[3]),
            "truncated_value_text_rows": int(row[4]),
            "invalid_json_rows": int(row[5]),
            "invalid_key_rows": int(row[6]),
            "fallback_rows": int(row[7]),
        }

    if con is not None:
        return _run_probe(con)
    with _work_connection(root, source_dbs=clean_sources) as (work_con, _):
        return _run_probe(work_con)


def _source_candidate_sql(
    con: duckdb.DuckDBPyConnection,
    source_dbs: tuple[str, ...] | None,
    *,
    months_filter: Sequence[str] | None,
) -> tuple[str, list[Any]]:
    """Build single-pass SQL projecting candidate bars filtered early by code & month."""
    src_view = _source_view_name(con)
    r_where, r_params = _source_where_clause(source_dbs, alias="r")
    fast_cond = (
        _FAST_PATH_PREDICATE_SQL.replace("key_utf8", "r.key_utf8")
        .replace("value_text", "r.value_text")
        .replace("value_kind", "r.value_kind")
    )

    con.execute("DROP TABLE IF EXISTS _filter_months")
    fast_month_join = ""
    fallback_month_join = ""
    if months_filter is not None:
        con.execute("CREATE TEMP TABLE _filter_months (ym VARCHAR PRIMARY KEY)")
        if months_filter:
            con.executemany(
                "INSERT INTO _filter_months (ym) VALUES (?)",
                [(m,) for m in months_filter],
            )
        fast_month_join = (
            "INNER JOIN _filter_months fm_fast "
            "ON substr(split_part(r.key_utf8, ':', 3), 1, 6) = fm_fast.ym"
        )
        fallback_month_join = "INNER JOIN _filter_months fm_fb ON fb.ym = fm_fb.ym"

    sql = f"""
    WITH fast_raw AS (
        SELECT
            r.source_db,
            r.snapshot_id,
            r.batch_id,
            r.seq_no,
            CASE r.source_db
                WHEN 'data1' THEN 3
                WHEN 'data' THEN 2
                WHEN 'mydb' THEN 1
                ELSE 0
            END AS source_priority,
            split_part(r.key_utf8, ':', 2) AS key_code,
            split_part(r.key_utf8, ':', 3) AS key_ts14,
            try(from_json(r.value_text, '{_MINUTE_JSON_SPEC}')) AS parsed
        FROM {src_view} r
        INNER JOIN _allowed_codes ac_key
            ON split_part(r.key_utf8, ':', 2) = ac_key.code
        {fast_month_join}
        WHERE {r_where}
          AND COALESCE({fast_cond}, FALSE)
    ),
    fast_norm AS (
        SELECT
            fr.source_db,
            fr.snapshot_id,
            fr.batch_id,
            fr.seq_no,
            fr.source_priority,
            COALESCE(NULLIF(TRIM(fr.parsed.code), ''), fr.key_code) AS code,
            ac_eff.market AS market,
            COALESCE(
                try_strptime(NULLIF(TRIM(fr.parsed.date), ''), '%Y%m%d%H%M%S'),
                try_strptime(NULLIF(TRIM(fr.parsed.date), ''), '%Y-%m-%d %H:%M:%S'),
                try_strptime(NULLIF(TRIM(fr.parsed.date), ''), '%Y-%m-%dT%H:%M:%S'),
                try_strptime(fr.key_ts14, '%Y%m%d%H%M%S')
            ) AS dt,
            CASE WHEN isfinite(fr.parsed.open) THEN fr.parsed.open ELSE NULL END AS open,
            CASE WHEN isfinite(fr.parsed.high) THEN fr.parsed.high ELSE NULL END AS high,
            CASE WHEN isfinite(fr.parsed.low) THEN fr.parsed.low ELSE NULL END AS low,
            CASE WHEN isfinite(fr.parsed.close) THEN fr.parsed.close ELSE NULL END AS close,
            CASE WHEN isfinite(fr.parsed.volume) THEN fr.parsed.volume ELSE NULL END AS volume,
            CASE WHEN isfinite(fr.parsed.amount) THEN fr.parsed.amount ELSE NULL END AS amount
        FROM fast_raw fr
        INNER JOIN _allowed_codes ac_eff
            ON COALESCE(NULLIF(TRIM(fr.parsed.code), ''), fr.key_code) = ac_eff.code
        WHERE fr.parsed IS NOT NULL
    )
    SELECT
        source_db,
        snapshot_id,
        batch_id,
        seq_no,
        source_priority,
        code,
        market,
        strftime(dt, '%Y%m%d') AS date8,
        CAST(dt AS DATE) AS date,
        dt,
        open,
        high,
        low,
        close,
        volume,
        amount,
        strftime(dt, '%Y%m') AS ym
    FROM fast_norm
    WHERE dt IS NOT NULL
    UNION ALL
    SELECT
        fb.source_db,
        fb.snapshot_id,
        fb.batch_id,
        fb.seq_no,
        fb.source_priority,
        fb.code,
        fb.market,
        fb.date8,
        fb.date,
        fb.dt,
        fb.open,
        fb.high,
        fb.low,
        fb.close,
        fb.volume,
        fb.amount,
        fb.ym
    FROM _fallback_minute_rows fb
    {fallback_month_join}
    """
    return sql, r_params


def _spill_candidates_by_month(
    con: duckdb.DuckDBPyConnection,
    root: Path,
    source_dbs: tuple[str, ...] | None,
    *,
    tag: str,
    months_filter: Sequence[str] | None,
) -> tuple[Path, list[str]]:
    """Stream candidate `k分钟k` bars once into month-partitioned ZSTD Parquet under `.staging`."""
    spill_dir = Path(root) / ".staging" / f"{tag}_{uuid.uuid4().hex[:10]}"
    spill_dir.mkdir(parents=True, exist_ok=True)
    try:
        src_sql, src_params = _source_candidate_sql(
            con, source_dbs, months_filter=months_filter
        )
        con.execute(
            f"COPY ({src_sql}) TO '{_sql_quote_path(spill_dir)}' "
            "(FORMAT PARQUET, COMPRESSION ZSTD, PARTITION_BY (ym))",
            src_params,
        )
        spilled_months = sorted(
            d.name[3:]
            for d in spill_dir.iterdir()
            if d.is_dir() and d.name.startswith("ym=") and _MONTH_RE.match(d.name[3:])
        )
        return spill_dir, spilled_months
    except Exception:
        _cleanup_dir_and_empty_staging(spill_dir, root)
        raise


def _scope_fingerprint(
    *,
    source_dbs: tuple[str, ...] | None,
    allowed_markets: tuple[str, ...],
    allowed_codes: list[str],
    snapshots: list[str],
    total_source_rows: int,
) -> dict[str, Any]:
    codes_digest = hashlib.sha256("\n".join(allowed_codes).encode("utf-8")).hexdigest()
    return {
        "source_dbs": list(source_dbs) if source_dbs is not None else None,
        "allowed_markets": list(allowed_markets),
        "codes_sha256": codes_digest,
        "snapshots": list(snapshots),
        "total_source_rows": int(total_source_rows),
    }


def _load_checkpoint(cp_path: Path) -> dict[str, Any] | None:
    if not cp_path.is_file():
        return None
    try:
        data = json.loads(cp_path.read_text(encoding="utf-8"))
        return data if isinstance(data, dict) else None
    except Exception:
        return None


def _save_checkpoint_atomic(cp_path: Path, payload: dict[str, Any]) -> None:
    cp_path.parent.mkdir(parents=True, exist_ok=True)
    tmp = cp_path.with_name(f".{cp_path.name}.{uuid.uuid4().hex[:8]}.tmp")
    try:
        tmp.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")
        os.replace(tmp, cp_path)
    except Exception:
        tmp.unlink(missing_ok=True)
        raise


def _write_minute_partition_atomic(
    con: duckdb.DuckDBPyConnection,
    *,
    target_path: Path,
    market: str,
    date8: str,
) -> None:
    target_path.parent.mkdir(parents=True, exist_ok=True)
    tmp_path = target_path.with_name(f".{target_path.name}.{uuid.uuid4().hex[:8]}.tmp")
    try:
        sql_path = _sql_quote_path(tmp_path)
        sql_market = _sql_quote_str(market)
        sql_date8 = _sql_quote_str(date8)
        con.execute(
            f"""
            COPY (
                SELECT
                    CAST(code AS VARCHAR) AS code,
                    CAST(date AS DATE) AS date,
                    CAST(dt AS TIMESTAMP) AS datetime,
                    CAST(open AS DOUBLE) AS open,
                    CAST(high AS DOUBLE) AS high,
                    CAST(low AS DOUBLE) AS low,
                    CAST(close AS DOUBLE) AS close,
                    CAST(volume AS DOUBLE) AS volume,
                    CAST(amount AS DOUBLE) AS amount
                FROM _month_minute_dedup
                WHERE market = '{sql_market}' AND date8 = '{sql_date8}'
                ORDER BY code ASC, datetime ASC
            ) TO '{sql_path}' (FORMAT PARQUET, COMPRESSION ZSTD)
            """
        )
        os.replace(tmp_path, target_path)
    except Exception:
        tmp_path.unlink(missing_ok=True)
        raise


def backfill_minute_from_leveldb_warehouse(
    root: Path,
    *,
    source_dbs: Sequence[str] | None = None,
    months: Sequence[str] | None = None,
    code_predicate: Callable[[str], bool] | None = None,
    allowed_markets: tuple[str, ...] | Iterable[str] | None = DEFAULT_ALLOWED_MARKETS,
    overwrite_existing: bool = False,
    resume: bool = True,
    checkpoint_path: Path | None = None,
    advance_watermark: bool = True,
    refresh_views_cb: Callable[[], None] | None = None,
    on_month_complete: Callable[[dict[str, Any]], None] | None = None,
) -> dict[str, Any]:
    """Vectorized single-pass DuckDB backfill from `facts/leveldb_raw` (`k分钟k`) to `facts/minute/period=1m`."""
    root = Path(root)
    clean_sources = _normalize_source_dbs(source_dbs)
    clean_markets = _normalize_markets(allowed_markets)
    requested_months = _normalize_months(months)
    cp_path = Path(checkpoint_path) if checkpoint_path is not None else (root / _CHECKPOINT_FILENAME)

    with bulk_import._BULK_LOCK:
        with _work_connection(root, source_dbs=clean_sources) as (con, _):
            probe = _probe_and_stage_fallback(
                con,
                source_dbs=clean_sources,
                code_predicate=code_predicate,
                allowed_markets=clean_markets,
                stage_fallback_rows=True,
            )

            discovered_months: list[str] = list(probe["months"])
            if requested_months is not None:
                target_months = [m for m in requested_months if m in set(discovered_months)]
            else:
                target_months = discovered_months

            if probe["total_rows"] == 0 or not target_months:
                return {
                    "ok": True,
                    "passed": True,
                    "period": DEFAULT_PERIOD,
                    "probe": {k: v for k, v in probe.items() if k != "allowed_codes"},
                    "months_discovered": discovered_months,
                    "months_target": target_months,
                    "months_written": [],
                    "months_skipped": [],
                    "written_rows": 0,
                    "written_files": 0,
                    "max_date8": None,
                    "watermark_updated": False,
                    "checkpoint_path": str(cp_path),
                }

            scope = _scope_fingerprint(
                source_dbs=clean_sources,
                allowed_markets=clean_markets,
                allowed_codes=probe["allowed_codes"],
                snapshots=probe["snapshots"],
                total_source_rows=probe["total_rows"],
            )

            completed_months: set[str] = set()
            month_stats: dict[str, dict[str, Any]] = {}
            if resume and not overwrite_existing:
                existing_cp = _load_checkpoint(cp_path)
                if existing_cp and existing_cp.get("scope") == scope:
                    completed_months = {
                        str(m) for m in (existing_cp.get("completed_months") or [])
                    }
                    raw_mstats = existing_cp.get("month_stats")
                    if isinstance(raw_mstats, dict):
                        month_stats = {
                            str(k): dict(v)
                            for k, v in raw_mstats.items()
                            if isinstance(v, dict)
                        }

            written_months: list[str] = []
            skipped_months: list[str] = []
            pending_months: list[str] = []
            max_date8: str | None = None

            for ym in target_months:
                if resume and not overwrite_existing and ym in completed_months:
                    skipped_months.append(ym)
                    prev_max = (month_stats.get(ym) or {}).get("max_date8")
                    if prev_max and (max_date8 is None or str(prev_max) > max_date8):
                        max_date8 = str(prev_max)
                else:
                    pending_months.append(ym)

            written_rows = 0
            written_files = 0

            if pending_months:
                spill_dir, _ = _spill_candidates_by_month(
                    con,
                    root,
                    clean_sources,
                    tag="minute_spill_backfill",
                    months_filter=pending_months,
                )
                try:
                    for ym in pending_months:
                        month_dir = spill_dir / f"ym={ym}"
                        m_written_rows = 0
                        m_written_files = 0
                        m_max_date8: str | None = None

                        if month_dir.is_dir():
                            month_glob = _sql_quote_path(month_dir / "*.parquet")
                            con.execute("DROP TABLE IF EXISTS _month_minute_dedup")
                            con.execute(
                                f"""
                                CREATE TEMP TABLE _month_minute_dedup AS
                                SELECT
                                    code,
                                    market,
                                    date8,
                                    date,
                                    dt,
                                    open,
                                    high,
                                    low,
                                    close,
                                    volume,
                                    amount
                                FROM read_parquet('{month_glob}', hive_partitioning=false)
                                QUALIFY ROW_NUMBER() OVER (
                                    PARTITION BY code, dt
                                    ORDER BY source_priority DESC, snapshot_id DESC, batch_id DESC, seq_no DESC
                                ) = 1
                                """
                            )
                            part_rows = con.execute(
                                """
                                SELECT market, date8, COUNT(*) AS cnt
                                FROM _month_minute_dedup
                                GROUP BY market, date8
                                ORDER BY date8 ASC, market ASC
                                """
                            ).fetchall()

                            for market, date8, cnt in part_rows:
                                target_path = layout.minute_partition(
                                    root, DEFAULT_PERIOD, str(date8), str(market)
                                )
                                _write_minute_partition_atomic(
                                    con,
                                    target_path=target_path,
                                    market=str(market),
                                    date8=str(date8),
                                )
                                m_written_rows += int(cnt)
                                m_written_files += 1
                                if m_max_date8 is None or str(date8) > m_max_date8:
                                    m_max_date8 = str(date8)

                            con.execute("DROP TABLE IF EXISTS _month_minute_dedup")

                        written_months.append(ym)
                        written_rows += m_written_rows
                        written_files += m_written_files
                        if m_max_date8 and (max_date8 is None or m_max_date8 > max_date8):
                            max_date8 = m_max_date8

                        completed_months.add(ym)
                        month_stats[ym] = {
                            "rows": m_written_rows,
                            "files": m_written_files,
                            "max_date8": m_max_date8,
                        }
                        _save_checkpoint_atomic(
                            cp_path,
                            {
                                "version": 1,
                                "period": DEFAULT_PERIOD,
                                "scope": scope,
                                "completed_months": sorted(completed_months),
                                "month_stats": month_stats,
                                "max_date8": max_date8,
                                "updated_at": _now_iso(),
                            },
                        )
                        if on_month_complete is not None:
                            on_month_complete(
                                {
                                    "month": ym,
                                    "rows": m_written_rows,
                                    "files": m_written_files,
                                    "max_date8": m_max_date8,
                                }
                            )
                finally:
                    _cleanup_dir_and_empty_staging(spill_dir, root)

            watermark_updated = False
            if advance_watermark and max_date8 is not None and (written_files > 0 or skipped_months):
                watermark_updated = bool(catalog.set_watermark(root, "minute", max_date8))

            if written_files > 0:
                if refresh_views_cb is not None:
                    refresh_views_cb()
                inventory.record_inventory_event(
                    root,
                    "minute_backfill",
                    summary_prefix="分钟线回填：",
                )

            return {
                "ok": True,
                "passed": True,
                "period": DEFAULT_PERIOD,
                "probe": {k: v for k, v in probe.items() if k != "allowed_codes"},
                "months_discovered": discovered_months,
                "months_target": target_months,
                "months_written": written_months,
                "months_skipped": skipped_months,
                "written_rows": written_rows,
                "written_files": written_files,
                "max_date8": max_date8,
                "watermark_updated": watermark_updated,
                "checkpoint_path": str(cp_path),
            }


def _minute_parquet_files(root: Path, period: str = DEFAULT_PERIOD) -> list[Path]:
    base = layout.facts_dir(Path(root)) / "minute" / f"period={period}"
    if not base.is_dir():
        return []
    return sorted(p for p in base.rglob("date=*.parquet") if p.is_file())


def _extract_date8_from_filename(path: Path) -> str | None:
    stem = path.stem
    if not stem.startswith("date="):
        return None
    date8 = stem[5:].strip()
    return date8 if _DATE8_RE.match(date8) else None


def _pick_spread_indices(length: int, limit: int) -> list[int]:
    if length <= 0 or limit <= 0:
        return []
    if length <= limit:
        return list(range(length))
    if limit == 1:
        return [0]
    idxs: list[int] = []
    for i in range(limit):
        pos = (i * (length - 1)) // (limit - 1)
        if pos not in idxs:
            idxs.append(pos)
    return idxs


def _float_equal(a: float | None, b: float | None) -> bool:
    if a is None and b is None:
        return True
    if a is None or b is None:
        return False
    return abs(a - b) <= 1e-6 * max(1.0, abs(a), abs(b))


def _decode_raw_ohlcva(
    *,
    value_text: str | None,
    raw_value: bytes | bytearray | memoryview | None,
) -> dict[str, float | None] | None:
    payload: dict[str, Any] | None = None
    if raw_value is not None:
        payload = _normalize_payload_dict(daily_backfill._decode_payload(bytes(raw_value)))
    if payload is None and value_text is not None:
        try:
            loaded = json.loads(value_text)
            payload = _normalize_payload_dict(loaded)
        except Exception:
            payload = None
    if payload is None:
        return None
    return {
        col: _safe_float_finite(payload.get(col))
        for col in ("open", "high", "low", "close", "volume", "amount")
    }


def _create_empty_expected_month(con: duckdb.DuckDBPyConnection) -> None:
    con.execute("DROP TABLE IF EXISTS _expected_month")
    con.execute(
        """
        CREATE TEMP TABLE _expected_month (
            source_db VARCHAR,
            snapshot_id VARCHAR,
            batch_id VARCHAR,
            seq_no BIGINT,
            source_priority INTEGER,
            code VARCHAR,
            market VARCHAR,
            date8 VARCHAR,
            date DATE,
            dt TIMESTAMP,
            open DOUBLE,
            high DOUBLE,
            low DOUBLE,
            close DOUBLE,
            volume DOUBLE,
            amount DOUBLE,
            source_hits BIGINT,
            has_data1 BOOLEAN,
            has_data BOOLEAN
        )
        """
    )


def _fetch_raw_payloads_for_samples(
    con: duckdb.DuckDBPyConnection,
    batch_path_map: dict[tuple[str, str, str], Path],
    samples: list[dict[str, Any]],
) -> dict[tuple[str, str, str, int], tuple[str | None, bytes | None]]:
    """Fetch raw `(value_text, raw_value)` reading ONLY batch Parquet files matching the sampled keys."""
    out: dict[tuple[str, str, str, int], tuple[str | None, bytes | None]] = {}
    grouped_direct: dict[tuple[str, str, str], list[int]] = {}
    fallback_keys: list[tuple[str, str, str, int]] = []

    for item in samples:
        sdb = item.get("source_db")
        snap = item.get("snapshot_id")
        bid = item.get("batch_id")
        seq = item.get("seq_no")
        if sdb is None or snap is None or bid is None or seq is None:
            continue
        key = (str(snap), str(sdb), str(bid))
        seq_i = int(seq)
        bpath = batch_path_map.get(key)
        if bpath is not None and bpath.is_file():
            grouped_direct.setdefault(key, []).append(seq_i)
        else:
            fallback_keys.append((str(sdb), str(snap), str(bid), seq_i))

    for (snap, sdb, bid), seqs in grouped_direct.items():
        bpath = batch_path_map[(snap, sdb, bid)]
        ph = ", ".join("?" for _ in seqs)
        rows = con.execute(
            f"SELECT seq_no, value_text, raw_value "
            f"FROM read_parquet('{_sql_quote_path(bpath)}', hive_partitioning=false) "
            f"WHERE seq_no IN ({ph})",
            seqs,
        ).fetchall()
        for seq_no, vt, rv in rows:
            out[(sdb, snap, bid, int(seq_no))] = (
                vt,
                bytes(rv) if rv is not None else None,
            )

    if fallback_keys:
        src_view = _source_view_name(con)
        for sdb, snap, bid, seq_i in fallback_keys:
            row = con.execute(
                f"SELECT value_text, raw_value FROM {src_view} "
                f"WHERE raw_key >= unhex('{_PREFIX_LOWER_HEX}') "
                f"AND raw_key < unhex('{_PREFIX_UPPER_HEX}') "
                f"AND source_db = ? AND snapshot_id = ? AND batch_id = ? AND seq_no = ? LIMIT 1",
                [sdb, snap, bid, seq_i],
            ).fetchone()
            if row is not None:
                out[(sdb, snap, bid, seq_i)] = (
                    row[0],
                    bytes(row[1]) if row[1] is not None else None,
                )

    return out


def verify_minute_backfill(
    root: Path,
    *,
    source_dbs: Sequence[str] | None = None,
    code_predicate: Callable[[str], bool] | None = None,
    allowed_markets: tuple[str, ...] | Iterable[str] | None = DEFAULT_ALLOWED_MARKETS,
    sample_days: int = 5,
    sample_per_day: int = 50,
    check_overlap: bool = True,
) -> dict[str, Any]:
    """Verify 1m Parquet partitions month-by-month against single-pass LevelDB `k分钟k` shards."""
    root = Path(root)
    clean_sources = _normalize_source_dbs(source_dbs)
    clean_markets = _normalize_markets(allowed_markets)
    parquet_files = _minute_parquet_files(root, DEFAULT_PERIOD)
    errors: list[str] = []

    actual_files_by_month: dict[str, list[Path]] = {}
    actual_files_by_date8: dict[str, list[Path]] = {}
    bad_partition_filenames: list[str] = []
    for p in parquet_files:
        date8 = _extract_date8_from_filename(p)
        if date8 is None:
            bad_partition_filenames.append(str(p))
            continue
        actual_files_by_date8.setdefault(date8, []).append(p)
        actual_files_by_month.setdefault(date8[:6], []).append(p)
    if bad_partition_filenames:
        errors.append(f"存在非法分钟线分区文件名: {bad_partition_filenames[:3]}")

    expected_rows = 0
    actual_rows = 0
    distinct_keys = 0
    duplicate_keys = 0
    missing_keys = 0
    extra_keys = 0
    disallowed_code_count = 0
    overlap_checked = 0
    data1_over_data_count = 0
    overlap_mismatches = 0
    sampled_days_count = 0
    sampled_rows_count = 0
    value_mismatches = 0
    min_date: str | None = None
    max_date: str | None = None
    schema_mismatch_months: list[str] = []

    with _work_connection(root, source_dbs=clean_sources) as (con, batch_path_map):
        probe = _probe_and_stage_fallback(
            con,
            source_dbs=clean_sources,
            code_predicate=code_predicate,
            allowed_markets=clean_markets,
            stage_fallback_rows=True,
        )

        # Sample day files directly from filenames (reads only chosen day files, never all period=1m)
        sampled_actual_by_month: dict[str, list[dict[str, Any]]] = {}
        sorted_day8s = sorted(actual_files_by_date8.keys())
        if sorted_day8s and sample_days > 0 and sample_per_day > 0:
            chosen_days = [
                sorted_day8s[i]
                for i in _pick_spread_indices(len(sorted_day8s), int(sample_days))
            ]
            sampled_days_count = len(chosen_days)
            for day8 in chosen_days:
                day_files_sql = ", ".join(
                    f"'{_sql_quote_path(p)}'" for p in actual_files_by_date8[day8]
                )
                picked_rows = con.execute(
                    f"""
                    SELECT
                        code,
                        datetime,
                        open,
                        high,
                        low,
                        close,
                        volume,
                        amount
                    FROM read_parquet([{day_files_sql}], hive_partitioning=false)
                    ORDER BY hash(code, datetime), code, datetime
                    LIMIT ?
                    """,
                    [int(sample_per_day)],
                ).fetchall()
                ym = day8[:6]
                bucket = sampled_actual_by_month.setdefault(ym, [])
                for code, dt_val, a_o, a_h, a_l, a_c, a_v, a_a in picked_rows:
                    bucket.append(
                        {
                            "code": code,
                            "datetime": dt_val,
                            "open": a_o,
                            "high": a_h,
                            "low": a_l,
                            "close": a_c,
                            "volume": a_v,
                            "amount": a_a,
                        }
                    )

        spill_dir: Path | None = None
        spilled_months: list[str] = []
        if probe["total_rows"] > 0 and probe["allowed_code_count"] > 0:
            spill_dir, spilled_months = _spill_candidates_by_month(
                con,
                root,
                clean_sources,
                tag="minute_spill_verify",
                months_filter=None,
            )

        try:
            expected_month_set = set(spilled_months)
            actual_month_set = set(actual_files_by_month.keys())
            missing_months = sorted(expected_month_set - actual_month_set)
            extra_months = sorted(actual_month_set - expected_month_set)
            if missing_months:
                errors.append(f"缺少目标月份分区: {missing_months}")
            if extra_months:
                errors.append(f"存在来源库外的多余月份分区: {extra_months}")

            all_months = sorted(expected_month_set | actual_month_set)
            all_sample_records: list[dict[str, Any]] = []

            for ym in all_months:
                month_spill_dir = (spill_dir / f"ym={ym}") if spill_dir is not None else None
                if month_spill_dir is not None and month_spill_dir.is_dir():
                    month_glob = _sql_quote_path(month_spill_dir / "*.parquet")
                    con.execute("DROP TABLE IF EXISTS _expected_month")
                    con.execute(
                        f"""
                        CREATE TEMP TABLE _expected_month AS
                        WITH scored AS (
                            SELECT
                                source_db,
                                snapshot_id,
                                batch_id,
                                seq_no,
                                source_priority,
                                code,
                                market,
                                date8,
                                date,
                                dt,
                                open,
                                high,
                                low,
                                close,
                                volume,
                                amount,
                                ROW_NUMBER() OVER (
                                    PARTITION BY code, dt
                                    ORDER BY source_priority DESC, snapshot_id DESC, batch_id DESC, seq_no DESC
                                ) AS rn,
                                COUNT(*) OVER (PARTITION BY code, dt) AS source_hits,
                                BOOL_OR(source_db = 'data1') OVER (PARTITION BY code, dt) AS has_data1,
                                BOOL_OR(source_db = 'data') OVER (PARTITION BY code, dt) AS has_data
                            FROM read_parquet('{month_glob}', hive_partitioning=false)
                        )
                        SELECT
                            source_db, snapshot_id, batch_id, seq_no, source_priority,
                            code, market, date8, date, dt,
                            open, high, low, close, volume, amount,
                            source_hits, has_data1, has_data
                        FROM scored
                        WHERE rn = 1
                        """
                    )
                else:
                    _create_empty_expected_month(con)

                exp_m = int(con.execute("SELECT COUNT(*) FROM _expected_month").fetchone()[0])
                expected_rows += exp_m

                month_pq_files = actual_files_by_month.get(ym, [])
                if month_pq_files:
                    files_sql = ", ".join(
                        f"'{_sql_quote_path(p)}'" for p in month_pq_files
                    )
                    cols_desc = tuple(
                        str(r[0])
                        for r in con.execute(
                            f"DESCRIBE SELECT * FROM read_parquet([{files_sql}], hive_partitioning=false)"
                        ).fetchall()
                    )
                    if cols_desc != _EXPECTED_MINUTE_COL_NAMES:
                        schema_mismatch_months.append(ym)

                    con.execute("DROP VIEW IF EXISTS _actual_month")
                    con.execute(
                        f"CREATE TEMP VIEW _actual_month AS "
                        f"SELECT code, date, datetime, open, high, low, close, volume, amount "
                        f"FROM read_parquet([{files_sql}], hive_partitioning=false)"
                    )
                    act_m = int(con.execute("SELECT COUNT(*) FROM _actual_month").fetchone()[0])
                    dist_m = int(
                        con.execute(
                            "SELECT COUNT(DISTINCT (code, datetime)) FROM _actual_month"
                        ).fetchone()[0]
                    )
                    dup_m = int(
                        con.execute(
                            "SELECT COUNT(*) FROM ("
                            "SELECT code, datetime FROM _actual_month "
                            "GROUP BY code, datetime HAVING COUNT(*) > 1"
                            ")"
                        ).fetchone()[0]
                    )
                    actual_codes = [
                        None if r[0] is None else str(r[0])
                        for r in con.execute("SELECT DISTINCT code FROM _actual_month").fetchall()
                    ]
                    has_null_code = any(c is None for c in actual_codes)
                    disallowed_codes = [
                        c
                        for c in actual_codes
                        if c is not None
                        and _evaluate_code_allowed(
                            c, code_predicate=code_predicate, allowed_markets=clean_markets
                        )
                        is None
                    ]
                    disallowed_m = 0
                    if has_null_code or disallowed_codes:
                        conds: list[str] = []
                        sql_args: list[Any] = []
                        if has_null_code:
                            conds.append("code IS NULL")
                        if disallowed_codes:
                            ph = ", ".join("?" for _ in disallowed_codes)
                            conds.append(f"code IN ({ph})")
                            sql_args.extend(disallowed_codes)
                        disallowed_m = int(
                            con.execute(
                                f"SELECT COUNT(*) FROM _actual_month WHERE {' OR '.join(conds)}",
                                sql_args,
                            ).fetchone()[0]
                        )

                    miss_m = int(
                        con.execute(
                            "SELECT COUNT(*) FROM ("
                            "SELECT code, dt AS datetime FROM _expected_month "
                            "EXCEPT "
                            "SELECT code, datetime FROM _actual_month"
                            ")"
                        ).fetchone()[0]
                    )
                    extra_m = int(
                        con.execute(
                            "SELECT COUNT(*) FROM ("
                            "SELECT code, datetime FROM _actual_month "
                            "EXCEPT "
                            "SELECT code, dt AS datetime FROM _expected_month"
                            ")"
                        ).fetchone()[0]
                    )
                    mm_row = con.execute(
                        "SELECT MIN(strftime(date, '%Y%m%d')), MAX(strftime(date, '%Y%m%d')) "
                        "FROM _actual_month"
                    ).fetchone()
                    if mm_row and mm_row[0]:
                        m_min = str(mm_row[0])
                        if min_date is None or m_min < min_date:
                            min_date = m_min
                    if mm_row and mm_row[1]:
                        m_max = str(mm_row[1])
                        if max_date is None or m_max > max_date:
                            max_date = m_max

                    d1_over_d_m = int(
                        con.execute(
                            "SELECT COUNT(*) FROM _expected_month WHERE has_data1 AND has_data"
                        ).fetchone()[0]
                    )
                    data1_over_data_count += d1_over_d_m
                    if check_overlap:
                        ov_m = int(
                            con.execute(
                                "SELECT COUNT(*) FROM _expected_month WHERE source_hits > 1"
                            ).fetchone()[0]
                        )
                        overlap_checked += ov_m
                        if ov_m > 0:
                            ov_mis_m = int(
                                con.execute(
                                    """
                                    SELECT COUNT(*)
                                    FROM _expected_month e
                                    LEFT JOIN _actual_month a
                                        ON e.code = a.code AND e.dt = a.datetime
                                    WHERE e.source_hits > 1
                                      AND (
                                          a.code IS NULL
                                          OR (e.open IS DISTINCT FROM a.open AND (e.open IS NULL OR a.open IS NULL OR abs(a.open - e.open) > 1e-6 * greatest(1.0, abs(a.open), abs(e.open))))
                                          OR (e.high IS DISTINCT FROM a.high AND (e.high IS NULL OR a.high IS NULL OR abs(a.high - e.high) > 1e-6 * greatest(1.0, abs(a.high), abs(e.high))))
                                          OR (e.low IS DISTINCT FROM a.low AND (e.low IS NULL OR a.low IS NULL OR abs(a.low - e.low) > 1e-6 * greatest(1.0, abs(a.low), abs(e.low))))
                                          OR (e.close IS DISTINCT FROM a.close AND (e.close IS NULL OR a.close IS NULL OR abs(a.close - e.close) > 1e-6 * greatest(1.0, abs(a.close), abs(e.close))))
                                          OR (e.volume IS DISTINCT FROM a.volume AND (e.volume IS NULL OR a.volume IS NULL OR abs(a.volume - e.volume) > 1e-6 * greatest(1.0, abs(a.volume), abs(e.volume))))
                                          OR (e.amount IS DISTINCT FROM a.amount AND (e.amount IS NULL OR a.amount IS NULL OR abs(a.amount - e.amount) > 1e-6 * greatest(1.0, abs(a.amount), abs(e.amount))))
                                      )
                                    """
                                ).fetchone()[0]
                            )
                            overlap_mismatches += ov_mis_m

                    actual_rows += act_m
                    distinct_keys += dist_m
                    duplicate_keys += dup_m
                    disallowed_code_count += disallowed_m
                    missing_keys += miss_m
                    extra_keys += extra_m
                else:
                    missing_keys += exp_m

                ym_samples = sampled_actual_by_month.get(ym)
                if ym_samples:
                    con.execute("DROP TABLE IF EXISTS _ym_samples")
                    con.execute(
                        "CREATE TEMP TABLE _ym_samples "
                        "(idx INTEGER, code VARCHAR, dt TIMESTAMP)"
                    )
                    con.executemany(
                        "INSERT INTO _ym_samples VALUES (?, ?, ?)",
                        [(idx, s["code"], s["datetime"]) for idx, s in enumerate(ym_samples)],
                    )
                    matched = con.execute(
                        """
                        SELECT
                            s.idx,
                            e.source_db,
                            e.snapshot_id,
                            e.batch_id,
                            e.seq_no
                        FROM _ym_samples s
                        LEFT JOIN _expected_month e
                            ON s.code = e.code AND s.dt = e.dt
                        ORDER BY s.idx ASC
                        """
                    ).fetchall()
                    for idx, sdb, snap, bid, seq in matched:
                        rec = dict(ym_samples[int(idx)])
                        rec["source_db"] = sdb
                        rec["snapshot_id"] = snap
                        rec["batch_id"] = bid
                        rec["seq_no"] = seq
                        all_sample_records.append(rec)
                    con.execute("DROP TABLE IF EXISTS _ym_samples")

                con.execute("DROP VIEW IF EXISTS _actual_month")
                con.execute("DROP TABLE IF EXISTS _expected_month")

            sampled_rows_count = len(all_sample_records)
            if all_sample_records:
                raw_payloads = _fetch_raw_payloads_for_samples(
                    con, batch_path_map, all_sample_records
                )
                for rec in all_sample_records:
                    sdb = rec.get("source_db")
                    snap = rec.get("snapshot_id")
                    bid = rec.get("batch_id")
                    seq = rec.get("seq_no")
                    if sdb is None or snap is None or bid is None or seq is None:
                        value_mismatches += 1
                        continue
                    pair = raw_payloads.get((str(sdb), str(snap), str(bid), int(seq)))
                    if pair is None:
                        value_mismatches += 1
                        continue
                    raw_decoded = _decode_raw_ohlcva(
                        value_text=pair[0], raw_value=pair[1]
                    )
                    if raw_decoded is None:
                        value_mismatches += 1
                        continue
                    actual_map = {
                        col: _safe_float_finite(rec.get(col))
                        for col in ("open", "high", "low", "close", "volume", "amount")
                    }
                    if not all(
                        _float_equal(actual_map[k], raw_decoded[k])
                        for k in ("open", "high", "low", "close", "volume", "amount")
                    ):
                        value_mismatches += 1
        finally:
            if spill_dir is not None:
                _cleanup_dir_and_empty_staging(spill_dir, root)

    if expected_rows != actual_rows:
        errors.append(f"行数不一致: expected={expected_rows}, actual={actual_rows}")
    if duplicate_keys > 0:
        errors.append(f"存在重复 (code, datetime) 主键: duplicate_keys={duplicate_keys}")
    if disallowed_code_count > 0:
        errors.append(f"存在非目标市场或非A股代码行: disallowed_rows={disallowed_code_count}")
    if missing_keys > 0 or extra_keys > 0:
        errors.append(f"主键集合不一致: missing_keys={missing_keys}, extra_keys={extra_keys}")
    if schema_mismatch_months:
        errors.append(f"存在列结构不匹配 9 列标准 schema 的月份: {schema_mismatch_months}")
    if overlap_mismatches > 0:
        errors.append(
            f"跨库重叠优先级核对失败 (data1 > data > mydb): mismatches={overlap_mismatches}"
        )
    if value_mismatches > 0:
        errors.append(
            f"抽样 OHLCVA 与原始 LevelDB 负载值不一致: mismatches={value_mismatches}"
        )

    passed = len(errors) == 0
    return {
        "ok": passed,
        "passed": passed,
        "period": DEFAULT_PERIOD,
        "expected_rows": expected_rows,
        "actual_rows": actual_rows,
        "distinct_key_count": distinct_keys,
        "duplicate_key_count": duplicate_keys,
        "missing_key_count": missing_keys,
        "extra_key_count": extra_keys,
        "disallowed_code_count": disallowed_code_count,
        "overlap_checked": overlap_checked,
        "data1_over_data_count": data1_over_data_count,
        "overlap_mismatches": overlap_mismatches,
        "sampled_days": sampled_days_count,
        "sampled_rows": sampled_rows_count,
        "value_mismatches": value_mismatches,
        "partition_files": len(parquet_files),
        "min_date": min_date,
        "max_date": max_date,
        "probe": {k: v for k, v in probe.items() if k != "allowed_codes"},
        "errors": errors,
    }


def consolidate_and_prune_minute(
    root: Path,
    *,
    source_dbs: Sequence[str] | None = None,
    months: Sequence[str] | None = None,
    code_predicate: Callable[[str], bool] | None = None,
    allowed_markets: tuple[str, ...] | Iterable[str] | None = DEFAULT_ALLOWED_MARKETS,
    overwrite_existing: bool = False,
    resume: bool = True,
    checkpoint_path: Path | None = None,
    advance_watermark: bool = True,
    sample_days: int = 5,
    sample_per_day: int = 50,
    prune: bool = True,
    batch_size: int = 50000,
    run_backfill: bool = True,
    refresh_views_cb: Callable[[], None] | None = None,
    on_month_complete: Callable[[dict[str, Any]], None] | None = None,
) -> dict[str, Any]:
    """Run minute backfill (optional), verify 100%, then safely prune ONLY `k分钟k`."""
    root = Path(root)
    with bulk_import._BULK_LOCK:
        backfill_res = None
        if run_backfill:
            backfill_res = backfill_minute_from_leveldb_warehouse(
                root,
                source_dbs=source_dbs,
                months=months,
                code_predicate=code_predicate,
                allowed_markets=allowed_markets,
                overwrite_existing=overwrite_existing,
                resume=resume,
                checkpoint_path=checkpoint_path,
                advance_watermark=advance_watermark,
                refresh_views_cb=refresh_views_cb,
                on_month_complete=on_month_complete,
            )

        verify_source_dbs = None if prune else source_dbs
        verify_res = verify_minute_backfill(
            root,
            source_dbs=verify_source_dbs,
            code_predicate=code_predicate,
            allowed_markets=allowed_markets,
            sample_days=sample_days,
            sample_per_day=sample_per_day,
        )

        prune_res = None
        surviving_verify: list[dict[str, Any]] = []
        if prune:
            if not verify_res.get("ok"):
                raise RuntimeError(
                    "分钟线回填核对未通过，拒绝裁剪 k分钟k 原库键: "
                    + "; ".join(verify_res.get("errors") or ["unknown verification failure"])
                )
            prune_res = daily_backfill.prune_leveldb_namespace(
                root,
                namespaces=(KMINUTE_NAMESPACE,),
                batch_size=batch_size,
                refresh_views_cb=refresh_views_cb,
                event_trigger=None,
            )
            if not prune_res.get("passed"):
                raise RuntimeError("裁剪 k分钟k 后 bulk_import 完整性校验未通过")

            cat_con = catalog._connect(root)
            try:
                all_jobs = cat_con.execute(
                    "SELECT snapshot_id, source_db FROM bulk_import_jobs "
                    "ORDER BY snapshot_id ASC, source_db ASC"
                ).fetchall()
            finally:
                cat_con.close()
            for snap_id, sdb in all_jobs:
                vres = bulk_import.verify_bulk_import(
                    root,
                    snapshot_id=str(snap_id),
                    source_db=str(sdb),
                    update_status=True,
                )
                surviving_verify.append(vres)
                if not vres.get("passed"):
                    raise RuntimeError(
                        f"裁剪后 bulk_import 任务校验失败: {snap_id}/{sdb}"
                    )

            if prune_res.get("removed_rows", 0) > 0:
                inventory.record_inventory_event(
                    root,
                    "dedup_minute",
                    summary_prefix="分钟线去重裁剪：",
                )

        passed = bool(verify_res.get("ok")) and (
            prune_res is None or bool(prune_res.get("passed"))
        )
        return {
            "ok": True,
            "passed": passed,
            "backfill": backfill_res,
            "verify": verify_res,
            "prune": prune_res,
            "bulk_import_verify": surviving_verify,
        }
