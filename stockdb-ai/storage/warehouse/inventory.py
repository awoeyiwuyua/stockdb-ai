"""storage.warehouse.inventory — 分析数仓资产分类、覆盖范围与导入变化监视器。

核心职责：
1. 将标准分析层（日线/周线/月线/分钟线/复权/码表）与 LevelDB 批量导入原生层按业务类型归类，
   精确提取各自时间覆盖范围（date_min ~ date_max）、行数与存储体量。
2. 在批量导入落盘与盘后沉淀节点持久化类型统计与变更审计（warehouse_inventory_history），
   使驾驶舱与详情抽屉在容器重启、增量导入期间都能零全表扫描地展示「当前有什么」和
   「相比之前变了多少」。
"""
from __future__ import annotations

import json
import re
import threading
import uuid
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from . import catalog, layout

_INVENTORY_LOCK = threading.RLock()

# 与 leveldb_snapshot._DATE_IN_KEY_RE 保持同源口径（兼容 YYYYMMDD 与 YYYYMMDDHHMMSS）
_DATE_IN_KEY_RE = re.compile(
    rb"(?:^|[:|\x1f_/])((?:19|20)\d{2}(?:0[1-9]|1[0-2])(?:0[1-9]|[12]\d|3[01]))(?:\d{6})?(?:$|[:|\x1f_/])"
)

# 业务类型映射表（raw prefix -> (规范类型键, 中文业务名)）
_LEVELDB_TYPE_LABELS: dict[str, tuple[str, str]] = {
    "k日k": ("k日k", "日线（原库）"),
    "日k": ("k日k", "日线（原库）"),
    "khk日k": ("khk日k", "港股日线"),
    "hk日k": ("khk日k", "港股日线"),
    "k分钟k": ("k分钟k", "分钟线"),
    "分钟k": ("k分钟k", "分钟线"),
    "k资金流": ("k资金流", "资金流向"),
    "资金流": ("k资金流", "资金流向"),
    "k打板指标": ("k打板指标", "打板指标"),
    "打板指标": ("k打板指标", "打板指标"),
    "k打板序列": ("k打板序列", "打板序列"),
    "打板序列": ("k打板序列", "打板序列"),
    "k竞价快照": ("k竞价快照", "竞价快照"),
    "竞价快照": ("k竞价快照", "竞价快照"),
    "k退市": ("k退市", "退市信息"),
    "退市": ("k退市", "退市信息"),
    "k周k": ("k周k", "周线（原库）"),
    "周k": ("k周k", "周线（原库）"),
    "k月k": ("k月k", "月线（原库）"),
    "月k": ("k月k", "月线（原库）"),
    "k复权": ("k复权", "复权（原库）"),
    "复权": ("k复权", "复权（原库）"),
    "k股票代码": ("k股票代码", "标的码表（原库）"),
    "股票代码": ("k股票代码", "标的码表（原库）"),
    "k自定义": ("k自定义", "自定义数据"),
    "自定义": ("k自定义", "自定义数据"),
    "k清单": ("k清单", "标的清单"),
    "清单": ("k清单", "标的清单"),
    "k板块": ("k板块", "板块指数"),
    "板块": ("k板块", "板块指数"),
    "k行业": ("k行业", "行业分类"),
    "行业": ("k行业", "行业分类"),
    "k概念": ("k概念", "概念分类"),
    "概念": ("k概念", "概念分类"),
}

# 标准表展示优先级 & 缓存：key=(root_str, dataset, file_count, max_mtime) -> (rows, codes)
_PARQUET_SHAPE_CACHE: dict[tuple[str, str, int, float], tuple[int, int | None]] = {}


def _now_iso() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%S.%fZ")


def _fmt_iso_date(d8: str | None) -> str | None:
    if not d8:
        return None
    s = str(d8).strip()
    if len(s) == 8 and s.isdigit():
        return f"{s[:4]}-{s[4:6]}-{s[6:8]}"
    return s


def format_date_range(date_min: str | None, date_max: str | None, *, fallback: str = "无日期") -> str:
    a = _fmt_iso_date(date_min)
    b = _fmt_iso_date(date_max)
    if a and b:
        return a if a == b else f"{a} ~ {b}"
    return a or b or fallback


def extract_key_date_str(raw_key: bytes) -> str | None:
    matches = _DATE_IN_KEY_RE.findall(raw_key)
    if not matches:
        return None
    return matches[-1].decode("ascii", errors="ignore")


def resolve_leveldb_type(raw_key: bytes, existing_namespace: str | None = None) -> tuple[str, str, bool]:
    """把原始 LevelDB 键映射为 `(type_key, type_label, is_index)`。"""
    head = raw_key.split(b"\x1f", 1)[0]
    prefix_text = ""
    if b"\x00" not in head:
        try:
            prefix_text = head.decode("utf-8")
        except UnicodeDecodeError:
            prefix_text = ""

    if prefix_text:
        if prefix_text.startswith("kidx:"):
            rest = prefix_text[5:]
            sub = rest.split(":", 1)[0]
            base_pair = _LEVELDB_TYPE_LABELS.get(f"k{sub}") or _LEVELDB_TYPE_LABELS.get(sub)
            base_label = base_pair[1].replace("（原库）", "") if base_pair else sub
            sub_key = base_pair[0].lstrip("k") if base_pair else sub
            return f"kidx:{sub_key}", f"{base_label}索引", True

        first_tok = prefix_text.split(":", 1)[0]
        if first_tok in _LEVELDB_TYPE_LABELS:
            tk, label = _LEVELDB_TYPE_LABELS[first_tok]
            return tk, label, False
        for pfx, (tk, label) in _LEVELDB_TYPE_LABELS.items():
            if prefix_text.startswith(pfx + ":") or prefix_text == pfx:
                return tk, label, False
        if first_tok and first_tok.isprintable() and len(first_tok) <= 32:
            return first_tok, first_tok, False

    if existing_namespace and existing_namespace not in ("binary", "raw_utf8"):
        mapped = _LEVELDB_TYPE_LABELS.get(existing_namespace)
        if mapped:
            return mapped[0], mapped[1], False
        return existing_namespace, existing_namespace, existing_namespace.startswith("kidx:")

    return "binary", "二进制原始键", False


def accumulate_batch_type_stats(
    rows: list[tuple[bytes, int]],
) -> dict[str, dict[str, Any]]:
    """按批内 `(raw_key, raw_bytes_len)` 聚合同批次各数据类型计数与时间范围。"""
    out: dict[str, dict[str, Any]] = {}
    for rk, rbytes in rows:
        tkey, tlabel, is_idx = resolve_leveldb_type(rk)
        dt = extract_key_date_str(rk)
        rec = out.get(tkey)
        if rec is None:
            rec = {
                "type_key": tkey,
                "type_label": tlabel,
                "is_index": is_idx,
                "row_count": 0,
                "raw_bytes": 0,
                "date_min": dt,
                "date_max": dt,
            }
            out[tkey] = rec
        rec["row_count"] += 1
        rec["raw_bytes"] += int(rbytes)
        if dt is not None:
            if rec["date_min"] is None or dt < rec["date_min"]:
                rec["date_min"] = dt
            if rec["date_max"] is None or dt > rec["date_max"]:
                rec["date_max"] = dt
    return out


def upsert_bulk_type_stats(
    con,
    snapshot_id: str,
    source_db: str,
    stats_map: dict[str, dict[str, Any]],
    now: str,
) -> None:
    """将单批次按类型统计增量合并到 `bulk_import_type_stats`（同事务执行）。"""
    for item in stats_map.values():
        con.execute(
            "INSERT INTO bulk_import_type_stats "
            "(snapshot_id, source_db, type_key, type_label, is_index, row_count, raw_bytes, "
            "date_min, date_max, updated_at) "
            "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?) "
            "ON CONFLICT (snapshot_id, source_db, type_key) DO UPDATE SET "
            "type_label = excluded.type_label, "
            "is_index = excluded.is_index, "
            "row_count = bulk_import_type_stats.row_count + excluded.row_count, "
            "raw_bytes = bulk_import_type_stats.raw_bytes + excluded.raw_bytes, "
            "date_min = CASE "
            "  WHEN bulk_import_type_stats.date_min IS NULL THEN excluded.date_min "
            "  WHEN excluded.date_min IS NULL THEN bulk_import_type_stats.date_min "
            "  ELSE LEAST(bulk_import_type_stats.date_min, excluded.date_min) "
            "END, "
            "date_max = CASE "
            "  WHEN bulk_import_type_stats.date_max IS NULL THEN excluded.date_max "
            "  WHEN excluded.date_max IS NULL THEN bulk_import_type_stats.date_max "
            "  ELSE GREATEST(bulk_import_type_stats.date_max, excluded.date_max) "
            "END, "
            "updated_at = excluded.updated_at",
            [
                snapshot_id,
                source_db,
                item["type_key"],
                item["type_label"],
                bool(item["is_index"]),
                int(item["row_count"]),
                int(item["raw_bytes"]),
                item["date_min"],
                item["date_max"],
                now,
            ],
        )


def _backfill_missing_type_stats(con) -> None:
    """为升级前已入仓但尚未写入 type_stats 的历史导入任务补齐一次分类统计。"""
    missing_jobs = con.execute(
        "SELECT j.snapshot_id, j.source_db "
        "FROM bulk_import_jobs j "
        "WHERE j.committed_batches > 0 "
        "AND NOT EXISTS ("
        "  SELECT 1 FROM bulk_import_type_stats s "
        "  WHERE s.snapshot_id = j.snapshot_id AND s.source_db = j.source_db"
        ")"
    ).fetchall()
    if not missing_jobs:
        return

    now = _now_iso()
    for sid, sdb in missing_jobs:
        batch_paths = con.execute(
            "SELECT parquet_path FROM bulk_import_batches "
            "WHERE snapshot_id = ? AND source_db = ? ORDER BY batch_index ASC",
            [sid, sdb],
        ).fetchall()
        merged: dict[str, dict[str, Any]] = {}
        for (ppath_str,) in batch_paths:
            p = Path(ppath_str)
            if not p.is_file():
                continue
            sql_path = str(p).replace("\\", "/")
            rows = con.execute(
                "SELECT raw_key, key_len + value_len FROM read_parquet(?)",
                [sql_path],
            ).fetchall()
            chunk = accumulate_batch_type_stats([(bytes(rk), int(rl)) for rk, rl in rows])
            for tk, item in chunk.items():
                cur = merged.get(tk)
                if cur is None:
                    merged[tk] = dict(item)
                else:
                    cur["row_count"] += item["row_count"]
                    cur["raw_bytes"] += item["raw_bytes"]
                    if item["date_min"] and (cur["date_min"] is None or item["date_min"] < cur["date_min"]):
                        cur["date_min"] = item["date_min"]
                    if item["date_max"] and (cur["date_max"] is None or item["date_max"] > cur["date_max"]):
                        cur["date_max"] = item["date_max"]
        if merged:
            upsert_bulk_type_stats(con, sid, sdb, merged, now)


def _scan_parquet_partition(root: Path, sub: str) -> dict[str, Any]:
    """扫描标准分区目录 `facts/<sub>/**/date=*.parquet`（兼容 2 级与 3 级 period 分区），返回日期范围与文件体量。"""
    sub_dir = layout.facts_dir(root) / sub
    if not sub_dir.is_dir():
        return {"files": [], "dates": [], "bytes": 0, "max_mtime": 0.0}

    files: list[Path] = []
    dates: set[str] = set()
    total_bytes = 0
    max_mtime = 0.0
    try:
        for p in sub_dir.rglob("date=*.parquet"):
            if not p.is_file():
                continue
            files.append(p)
            stem = p.stem
            if stem.startswith("date=") and len(stem) == 13 and stem[5:].isdigit():
                dates.add(stem[5:])
            try:
                st = p.stat()
                total_bytes += st.st_size
                if st.st_mtime > max_mtime:
                    max_mtime = st.st_mtime
            except OSError:
                continue
    except OSError:
        pass
    sorted_dates = sorted(dates)
    return {
        "files": files,
        "dates": sorted_dates,
        "bytes": total_bytes,
        "max_mtime": max_mtime,
    }


def _cached_parquet_shape(con, root: Path, sub: str, scan: dict[str, Any], *, count_codes: bool) -> tuple[int, int | None]:
    files = scan["files"]
    if not files:
        return 0, (0 if count_codes else None)
    ck = (str(root), sub, len(files), scan["max_mtime"])
    cached = _PARQUET_SHAPE_CACHE.get(ck)
    if cached is not None:
        return cached

    file_paths = [p.as_posix() for p in files]
    try:
        if count_codes:
            row = con.execute(
                "SELECT COUNT(*), COUNT(DISTINCT code) FROM read_parquet(?, hive_partitioning=true)",
                [file_paths],
            ).fetchone()
            res = (int(row[0]), int(row[1]))
        else:
            # 仅读取 Parquet Footer 元数据汇总行数，严禁扫描数据页或跑 COUNT(DISTINCT code)
            row = con.execute(
                "SELECT COALESCE(SUM(num_rows), 0) FROM parquet_file_metadata(?)",
                [file_paths],
            ).fetchone()
            res = (int(row[0]), None)
    except Exception:
        res = (0, None)
    _PARQUET_SHAPE_CACHE[ck] = res
    return res


def _build_standard_datasets(con, root: Path) -> list[dict[str, Any]]:
    datasets: list[dict[str, Any]] = []
    specs = (
        ("minute", "分钟线", "日", False),
        ("daily", "日线", "日", True),
        ("week", "周线", "周", False),
        ("month", "月线", "月", False),
    )
    for key, label, unit, count_codes in specs:
        scan = _scan_parquet_partition(root, key)
        if not scan["files"]:
            continue
        rows_cnt, codes_cnt = _cached_parquet_shape(con, root, key, scan, count_codes=count_codes)
        dmin = scan["dates"][0] if scan["dates"] else None
        dmax = scan["dates"][-1] if scan["dates"] else None
        periods = len(scan["dates"])
        datasets.append(
            {
                "key": f"std:{key}",
                "type_key": key,
                "label": label,
                "layer": "standard",
                "layer_label": "标准分析层",
                "source": f"facts/{key}",
                "is_index": False,
                "date_min": _fmt_iso_date(dmin),
                "date_max": _fmt_iso_date(dmax),
                "range_text": format_date_range(dmin, dmax),
                "periods": periods,
                "units_text": f"{periods} 个交易{unit}" if key in ("daily", "minute") else f"{periods} {unit}",
                "rows": rows_cnt,
                "codes": codes_cnt,
                "bytes": int(scan["bytes"]),
                "status": "ready",
            }
        )

    # 复权事件审计表 & 标的码表
    try:
        adj_row = con.execute(
            "SELECT COUNT(*), COUNT(DISTINCT code), MIN(date), MAX(date) FROM adjust_events"
        ).fetchone()
        if adj_row and int(adj_row[0]) > 0:
            datasets.append(
                {
                    "key": "std:adjust_events",
                    "type_key": "adjust_events",
                    "label": "复权事件",
                    "layer": "standard",
                    "layer_label": "标准分析层",
                    "source": "duckdb:adjust_events",
                    "is_index": False,
                    "date_min": _fmt_iso_date(adj_row[2]),
                    "date_max": _fmt_iso_date(adj_row[3]),
                    "range_text": format_date_range(adj_row[2], adj_row[3]),
                    "periods": None,
                    "units_text": f"{int(adj_row[1]):,} 标的",
                    "rows": int(adj_row[0]),
                    "codes": int(adj_row[1]),
                    "bytes": 0,
                    "status": "ready",
                }
            )
    except Exception:
        pass

    try:
        code_row = con.execute("SELECT COUNT(*), MAX(updated_at) FROM codes").fetchone()
        if code_row and int(code_row[0]) > 0:
            upd = str(code_row[1] or "")[:10] or None
            datasets.append(
                {
                    "key": "std:codes",
                    "type_key": "codes",
                    "label": "标的码表",
                    "layer": "standard",
                    "layer_label": "标准分析层",
                    "source": "duckdb:codes",
                    "is_index": False,
                    "date_min": upd,
                    "date_max": upd,
                    "range_text": f"更新于 {upd}" if upd else "最新快照",
                    "periods": None,
                    "units_text": f"{int(code_row[0]):,} 标的",
                    "rows": int(code_row[0]),
                    "codes": int(code_row[0]),
                    "bytes": 0,
                    "status": "ready",
                }
            )
    except Exception:
        pass
    return datasets


def _build_leveldb_datasets(con) -> tuple[list[dict[str, Any]], list[dict[str, Any]]]:
    _backfill_missing_type_stats(con)
    job_rows = con.execute(
        "SELECT snapshot_id, source_db, status, expected_batches, committed_batches, "
        "committed_rows, committed_bytes, created_at, updated_at, completed_at, verified_at, verify_summary "
        "FROM bulk_import_jobs ORDER BY updated_at DESC, snapshot_id ASC, source_db ASC"
    ).fetchall()
    jobs: list[dict[str, Any]] = []
    job_status_map: dict[tuple[str, str], str] = {}
    for r in job_rows:
        job_status_map[(r[0], r[1])] = r[2]
        jobs.append(
            {
                "snapshot_id": r[0],
                "source_db": r[1],
                "status": r[2],
                "expected_batches": int(r[3]) if r[3] is not None else None,
                "committed_batches": int(r[4]),
                "committed_rows": int(r[5]),
                "committed_bytes": int(r[6]),
                "created_at": r[7],
                "updated_at": r[8],
                "completed_at": r[9],
                "verified_at": r[10],
                "verify_summary": json.loads(r[11]) if r[11] else None,
            }
        )

    stat_rows = con.execute(
        "SELECT snapshot_id, source_db, type_key, type_label, is_index, "
        "row_count, raw_bytes, date_min, date_max, updated_at "
        "FROM bulk_import_type_stats"
    ).fetchall()

    # 按 (type_key, source_db) 汇总；若同一源库有多个快照，合并显示总览范围（若同键重叠计数由最新快照口径标示）
    grouped: dict[tuple[str, str], dict[str, Any]] = {}
    for sid, sdb, tkey, tlabel, is_idx, rcnt, rbytes, dmin, dmax, upd in stat_rows:
        gk = (tkey, sdb)
        st = job_status_map.get((sid, sdb), "committed")
        cur = grouped.get(gk)
        if cur is None:
            grouped[gk] = {
                "key": f"ldb:{sdb}:{tkey}",
                "type_key": tkey,
                "label": tlabel,
                "layer": "leveldb",
                "layer_label": f"原库 {sdb}",
                "source": sdb,
                "is_index": bool(is_idx),
                "date_min": dmin,
                "date_max": dmax,
                "rows": int(rcnt),
                "codes": None,
                "bytes": int(rbytes),
                "periods": None,
                "units_text": f"来源 {sdb}",
                "status": st,
                "snapshot_ids": [sid],
                "updated_at": upd,
            }
        else:
            cur["rows"] += int(rcnt)
            cur["bytes"] += int(rbytes)
            if dmin and (cur["date_min"] is None or dmin < cur["date_min"]):
                cur["date_min"] = dmin
            if dmax and (cur["date_max"] is None or dmax > cur["date_max"]):
                cur["date_max"] = dmax
            if sid not in cur["snapshot_ids"]:
                cur["snapshot_ids"].append(sid)
            if upd and upd > str(cur["updated_at"]):
                cur["updated_at"] = upd
                cur["status"] = st

    datasets: list[dict[str, Any]] = []
    for item in grouped.values():
        dmin = item["date_min"]
        dmax = item["date_max"]
        item["date_min"] = _fmt_iso_date(dmin)
        item["date_max"] = _fmt_iso_date(dmax)
        item["range_text"] = format_date_range(dmin, dmax)
        datasets.append(item)

    datasets.sort(key=lambda x: (x["is_index"], -x["rows"], x["label"]))
    return datasets, jobs


def save_daily_reconcile(root: Path, payload: dict[str, Any]) -> None:
    """持久化最近一次日频对账结论到 DuckDB meta，重启不回退为未监控。"""
    catalog.set_meta(root, "last_daily_reconcile", json.dumps(payload, ensure_ascii=False))


def get_daily_reconcile(root: Path) -> dict[str, Any] | None:
    raw = catalog.get_meta(root, "last_daily_reconcile")
    if not raw:
        return None
    try:
        return json.loads(raw)
    except ValueError:
        return None


def _dir_size_bytes(path: Path) -> int:
    if not path.exists():
        return 0
    total = 0
    try:
        for f in path.rglob("*"):
            try:
                if f.is_file():
                    total += f.stat().st_size
            except OSError:
                continue
    except OSError:
        pass
    return total


def _snapshot_summary_payload(con, root: Path) -> dict[str, Any]:
    std = _build_standard_datasets(con, root)
    ldb, jobs = _build_leveldb_datasets(con)
    all_items = std + ldb

    std_rows = sum(int(x["rows"]) for x in std)
    ldb_rows = sum(int(x["rows"]) for x in ldb)
    std_bytes = sum(int(x["bytes"]) for x in std)
    ldb_disk_bytes = _dir_size_bytes(layout.leveldb_raw_dir(root))
    duckdb_bytes = layout.duckdb_path(root).stat().st_size if layout.duckdb_path(root).is_file() else 0

    type_map: dict[str, dict[str, Any]] = {}
    for x in all_items:
        type_map[x["key"]] = {
            "label": x["label"],
            "source": x["source"],
            "rows": int(x["rows"]),
            "bytes": int(x["bytes"]),
            "range_text": x["range_text"],
            "is_index": bool(x["is_index"]),
        }

    return {
        "standard": std,
        "leveldb": ldb,
        "jobs": jobs,
        "total_rows": std_rows + ldb_rows,
        "standard_rows": std_rows,
        "leveldb_rows": ldb_rows,
        "standard_bytes": std_bytes,
        "leveldb_disk_bytes": ldb_disk_bytes,
        "leveldb_raw_bytes": sum(int(x["bytes"]) for x in ldb),
        "duckdb_bytes": duckdb_bytes,
        "total_bytes": std_bytes + ldb_disk_bytes + duckdb_bytes,
        "type_map": type_map,
    }


def _describe_delta(prev_detail: dict[str, Any] | None, cur_detail: dict[str, Any], *, prefix: str = "") -> str:
    if prev_detail is None:
        return f"{prefix}初始化监控基线（{cur_detail['total_rows']:,} 行）".strip()
    prev_map = prev_detail.get("types") or {}
    cur_map = cur_detail.get("types") or {}
    highlights: list[str] = []
    for k, cur_v in cur_map.items():
        if cur_v.get("is_index"):
            continue
        old_v = prev_map.get(k)
        if old_v is None:
            highlights.append(f"新增 {cur_v['label']}({cur_v['rows']:,})")
        else:
            dr = int(cur_v["rows"]) - int(old_v.get("rows", 0))
            if dr != 0:
                sign = "+" if dr > 0 else ""
                highlights.append(f"{cur_v['label']} {sign}{dr:,}")
            elif cur_v.get("range_text") != old_v.get("range_text"):
                highlights.append(f"{cur_v['label']} 范围->{cur_v['range_text']}")
    if not highlights:
        dr_all = int(cur_detail["total_rows"]) - int(prev_detail.get("total_rows", 0))
        if dr_all != 0:
            sign = "+" if dr_all > 0 else ""
            highlights.append(f"总记录 {sign}{dr_all:,}")
        else:
            highlights.append("结构校验更新（行数不变）")
    head = "、".join(highlights[:4])
    if len(highlights) > 4:
        head += f" 等 {len(highlights)} 项"
    return f"{prefix}{head}"


def record_inventory_event(
    root: Path,
    trigger_kind: str,
    *,
    snapshot_id: str | None = None,
    source_db: str | None = None,
    event_id_override: str | None = None,
    summary_prefix: str = "",
    con=None,
) -> dict[str, Any]:
    """记录一次数仓资产水位事件，计算相对前序基线的行/体量/类型增量。"""
    root = Path(root)
    own_con = con is None
    if own_con:
        con = catalog._connect(root)
    try:
        snap = _snapshot_summary_payload(con, root)
        cur_detail = {
            "total_rows": snap["total_rows"],
            "total_bytes": snap["total_bytes"],
            "types": snap["type_map"],
        }
        ev_id = event_id_override or f"{trigger_kind}:{_now_iso()}:{uuid.uuid4().hex[:8]}"
        if trigger_kind != "bulk_batch" and snapshot_id and source_db:
            con.execute(
                "DELETE FROM warehouse_inventory_history WHERE event_id = ?",
                [f"import:{snapshot_id}:{source_db}:progress"],
            )

        prev_row = con.execute(
            "SELECT total_rows, total_bytes, detail_json FROM warehouse_inventory_history "
            "WHERE event_id != ? ORDER BY recorded_at DESC LIMIT 1",
            [ev_id],
        ).fetchone()

        prev_rows = int(prev_row[0]) if prev_row else 0
        prev_bytes = int(prev_row[1]) if prev_row else 0
        prev_detail = json.loads(prev_row[2]) if (prev_row and prev_row[2]) else None

        delta_rows = snap["total_rows"] - prev_rows if prev_row else 0
        delta_bytes = snap["total_bytes"] - prev_bytes if prev_row else 0
        summary_text = _describe_delta(prev_detail, cur_detail, prefix=summary_prefix)
        now = _now_iso()

        con.execute(
            "INSERT INTO warehouse_inventory_history "
            "(event_id, recorded_at, trigger_kind, snapshot_id, source_db, "
            "total_rows, total_bytes, delta_rows, delta_bytes, summary_text, detail_json) "
            "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?) "
            "ON CONFLICT (event_id) DO UPDATE SET "
            "recorded_at = excluded.recorded_at, "
            "total_rows = excluded.total_rows, "
            "total_bytes = excluded.total_bytes, "
            "delta_rows = excluded.delta_rows, "
            "delta_bytes = excluded.delta_bytes, "
            "summary_text = excluded.summary_text, "
            "detail_json = excluded.detail_json",
            [
                ev_id,
                now,
                trigger_kind,
                snapshot_id,
                source_db,
                int(snap["total_rows"]),
                int(snap["total_bytes"]),
                int(delta_rows),
                int(delta_bytes),
                summary_text,
                json.dumps(cur_detail, ensure_ascii=False),
            ],
        )
        return {
            "event_id": ev_id,
            "recorded_at": now,
            "trigger_kind": trigger_kind,
            "delta_rows": delta_rows,
            "delta_bytes": delta_bytes,
            "summary_text": summary_text,
        }
    finally:
        if own_con:
            con.close()


def _build_primary_highlights(std: list[dict[str, Any]], ldb: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """生成驾驶舱卡片「按数据类型 + 范围」简洁摘要（主类型优先，索引折叠）。"""
    out: list[dict[str, Any]] = []
    for item in std:
        if item["type_key"] in ("minute", "daily", "week", "month"):
            out.append(
                {
                    "key": item["key"],
                    "label": item["label"],
                    "range_text": item["range_text"],
                    "units_text": item["units_text"],
                    "rows": item["rows"],
                    "bytes": item["bytes"],
                    "source": "标准数仓",
                    "status": item["status"],
                }
            )
    for item in ldb:
        if item["is_index"]:
            continue
        out.append(
            {
                "key": item["key"],
                "label": item["label"],
                "range_text": item["range_text"],
                "units_text": f"{item['source']} · {item['rows']:,} 行",
                "rows": item["rows"],
                "bytes": item["bytes"],
                "source": item["source"],
                "status": item["status"],
            }
        )
    return out


def get_warehouse_inventory(root: Path, *, history_limit: int = 30) -> dict[str, Any]:
    """返回分析数仓完整按类型清单、覆盖范围、对账状态、活跃任务与导入变更记录。"""
    root = Path(root)
    with _INVENTORY_LOCK:
        con = catalog._connect(root)
        try:
            snap = _snapshot_summary_payload(con, root)

            hist_count = int(
                con.execute("SELECT COUNT(*) FROM warehouse_inventory_history").fetchone()[0]
            )
            if hist_count == 0 and snap["total_rows"] > 0:
                record_inventory_event(root, "baseline", con=con)

            hist_rows = con.execute(
                "SELECT event_id, recorded_at, trigger_kind, snapshot_id, source_db, "
                "total_rows, total_bytes, delta_rows, delta_bytes, summary_text "
                "FROM warehouse_inventory_history "
                "ORDER BY recorded_at DESC LIMIT ?",
                [max(1, int(history_limit))],
            ).fetchall()
            changes = [
                {
                    "event_id": r[0],
                    "recorded_at": r[1],
                    "trigger_kind": r[2],
                    "snapshot_id": r[3],
                    "source_db": r[4],
                    "total_rows": int(r[5]),
                    "total_bytes": int(r[6]),
                    "delta_rows": int(r[7]),
                    "delta_bytes": int(r[8]),
                    "summary_text": r[9],
                }
                for r in hist_rows
            ]

            daily_rec_raw = con.execute(
                "SELECT value FROM meta WHERE key = 'last_daily_reconcile'"
            ).fetchone()
            daily_reconcile = None
            if daily_rec_raw and daily_rec_raw[0]:
                try:
                    daily_reconcile = json.loads(daily_rec_raw[0])
                except ValueError:
                    daily_reconcile = None
        finally:
            con.close()

    jobs = snap["jobs"]
    active_jobs = [j for j in jobs if j["status"] in ("uploading", "completing")]
    verified_jobs = [j for j in jobs if j["status"] == "verified"]
    latest_job = jobs[0] if jobs else None
    latest_change = next((c for c in changes if c["delta_rows"] != 0 or c["trigger_kind"] != "baseline"), changes[0] if changes else None)

    highlights = _build_primary_highlights(snap["standard"], snap["leveldb"])
    index_rows = sum(int(x["rows"]) for x in snap["leveldb"] if x["is_index"])

    return {
        "ok": True,
        "generated_at": _now_iso(),
        "datasets": snap["standard"] + snap["leveldb"],
        "highlights": highlights,
        "index_rows": index_rows,
        "jobs": jobs,
        "active_jobs": active_jobs,
        "changes": changes,
        "latest_change": latest_change,
        "reconcile": {
            "daily": daily_reconcile,
            "bulk": {
                "job_count": len(jobs),
                "verified_count": len(verified_jobs),
                "active_count": len(active_jobs),
                "latest_job": latest_job,
            },
        },
        "totals": {
            "total_rows": snap["total_rows"],
            "standard_rows": snap["standard_rows"],
            "leveldb_rows": snap["leveldb_rows"],
            "standard_bytes": snap["standard_bytes"],
            "leveldb_disk_bytes": snap["leveldb_disk_bytes"],
            "leveldb_raw_bytes": snap["leveldb_raw_bytes"],
            "duckdb_bytes": snap["duckdb_bytes"],
            "total_bytes": snap["total_bytes"],
        },
    }
