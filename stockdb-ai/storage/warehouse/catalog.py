"""storage.warehouse.catalog — 仓库元数据（0.10.0 W2；收敛点 C4：唯一存于 warehouse.duckdb meta 表）。

watermark 语义：dataset 已沉淀到的最新日期（YYYYMMDD），**只前进不回退**——
查询侧 known_at 由 watermark 派生（W3/W5），是仓库可信度的唯一时点声明。

连接策略：短生命周期（用完即关）。DuckDB 同进程按路径缓存数据库实例，
sink/engine 的多个连接共享同一实例，元数据即时互见；写入频率为日级，无争用。
"""
from __future__ import annotations

from pathlib import Path

from . import layout

_SCHEMA = """
CREATE TABLE IF NOT EXISTS meta (
    key   TEXT PRIMARY KEY,
    value TEXT NOT NULL
)
"""

# 0.11.0：复权事件审计（warehouse_run 刷新时仅插入首见事件；source_ts=发现时间）。
# 主键 (code, date)——引擎侧同日多事件以末条 cum 为准，审计表记录首次发现的那条。
_ADJUST_EVENTS_SCHEMA = """
CREATE TABLE IF NOT EXISTS adjust_events (
    code TEXT NOT NULL,
    date TEXT NOT NULL,
    div DOUBLE,
    give DOUBLE,
    trans DOUBLE,
    mult DOUBLE,
    cum DOUBLE,
    source_ts TEXT NOT NULL,
    PRIMARY KEY (code, date)
)
"""

_BULK_IMPORT_JOBS_SCHEMA = """
CREATE TABLE IF NOT EXISTS bulk_import_jobs (
    snapshot_id TEXT NOT NULL,
    source_db TEXT NOT NULL,
    status TEXT NOT NULL,
    source_fingerprint TEXT,
    expected_batches BIGINT,
    committed_batches BIGINT NOT NULL DEFAULT 0,
    committed_rows BIGINT NOT NULL DEFAULT 0,
    committed_bytes BIGINT NOT NULL DEFAULT 0,
    last_seq_end BIGINT NOT NULL DEFAULT -1,
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL,
    completed_at TEXT,
    verified_at TEXT,
    verify_summary TEXT,
    PRIMARY KEY (snapshot_id, source_db)
)
"""

_BULK_IMPORT_BATCHES_SCHEMA = """
CREATE TABLE IF NOT EXISTS bulk_import_batches (
    snapshot_id TEXT NOT NULL,
    source_db TEXT NOT NULL,
    batch_id TEXT NOT NULL,
    batch_index BIGINT NOT NULL,
    seq_start BIGINT NOT NULL,
    seq_end BIGINT NOT NULL,
    row_count BIGINT NOT NULL,
    file_size BIGINT NOT NULL,
    parquet_sha256 TEXT NOT NULL,
    rows_digest_sha256 TEXT NOT NULL,
    first_key_sha256 TEXT NOT NULL,
    last_key_sha256 TEXT NOT NULL,
    parquet_path TEXT NOT NULL,
    committed_at TEXT NOT NULL,
    PRIMARY KEY (snapshot_id, source_db, batch_id)
)
"""

_BULK_IMPORT_TYPE_STATS_SCHEMA = """
CREATE TABLE IF NOT EXISTS bulk_import_type_stats (
    snapshot_id TEXT NOT NULL,
    source_db TEXT NOT NULL,
    type_key TEXT NOT NULL,
    type_label TEXT NOT NULL,
    is_index BOOLEAN NOT NULL DEFAULT FALSE,
    row_count BIGINT NOT NULL DEFAULT 0,
    raw_bytes BIGINT NOT NULL DEFAULT 0,
    date_min TEXT,
    date_max TEXT,
    updated_at TEXT NOT NULL,
    PRIMARY KEY (snapshot_id, source_db, type_key)
)
"""

_WAREHOUSE_INVENTORY_HISTORY_SCHEMA = """
CREATE TABLE IF NOT EXISTS warehouse_inventory_history (
    event_id TEXT PRIMARY KEY,
    recorded_at TEXT NOT NULL,
    trigger_kind TEXT NOT NULL,
    snapshot_id TEXT,
    source_db TEXT,
    total_rows BIGINT NOT NULL DEFAULT 0,
    total_bytes BIGINT NOT NULL DEFAULT 0,
    delta_rows BIGINT NOT NULL DEFAULT 0,
    delta_bytes BIGINT NOT NULL DEFAULT 0,
    summary_text TEXT NOT NULL,
    detail_json TEXT NOT NULL
)
"""


def _connect(root: Path):
    import duckdb

    root = Path(root)
    root.mkdir(parents=True, exist_ok=True)
    con = duckdb.connect(str(layout.duckdb_path(root)))
    con.execute(_SCHEMA)
    con.execute(_ADJUST_EVENTS_SCHEMA)
    con.execute(_BULK_IMPORT_JOBS_SCHEMA)
    con.execute(_BULK_IMPORT_BATCHES_SCHEMA)
    con.execute(_BULK_IMPORT_TYPE_STATS_SCHEMA)
    con.execute(_WAREHOUSE_INVENTORY_HISTORY_SCHEMA)
    return con


def record_adjust_events(root: Path, events: list[dict]) -> int:
    """复权事件审计落盘（0.11.0）：仅插入首见的 (code, date)，返回新增条数。

    审计语义 = 事件首次发现时间（source_ts）；重复刷新不覆盖已有行。
    量级：首轮 ~14 万行（全市场上市以来事件），此后每日增量数条——
    executemany 批量导入，秒级。
    """
    from datetime import datetime

    con = _connect(root)
    try:
        existing = {r[0] for r in
                    con.execute("SELECT code || ':' || date FROM adjust_events").fetchall()}
        ts = datetime.now().isoformat(timespec="seconds")

        def _num(v):
            try:
                return float(v)
            except (TypeError, ValueError):
                return None

        rows = []
        for ev in events or []:
            code = str(ev.get("code") or "").strip()
            date = str(ev.get("date") or "").strip()
            if not code or len(date) != 8 or not date.isdigit():
                continue
            if f"{code}:{date}" in existing:
                continue
            rows.append((code, date, _num(ev.get("div")), _num(ev.get("give")),
                         _num(ev.get("trans")), _num(ev.get("mult")),
                         _num(ev.get("cum")), ts))
        if rows:
            con.executemany(
                "INSERT INTO adjust_events "
                "(code, date, div, give, trans, mult, cum, source_ts) "
                "VALUES (?, ?, ?, ?, ?, ?, ?, ?)", rows)
        return len(rows)
    finally:
        con.close()


def get_meta(root: Path, key: str, default=None):
    con = _connect(root)
    try:
        rows = con.execute("SELECT value FROM meta WHERE key = ?", [key]).fetchall()
        return rows[0][0] if rows else default
    finally:
        con.close()


def set_meta(root: Path, key: str, value) -> None:
    con = _connect(root)
    try:
        con.execute(
            "INSERT OR REPLACE INTO meta (key, value) VALUES (?, ?)",
            [key, str(value)],
        )
    finally:
        con.close()


def get_watermark(root: Path, dataset: str = "daily"):
    """dataset 已沉淀到的最新日期（YYYYMMDD；未沉淀返回 None）。"""
    return get_meta(root, f"watermark:{dataset}")


def set_watermark(root: Path, dataset: str, date) -> bool:
    """推进 watermark；仅当新值更新（字典序比较 8 位日期）才写。返回是否推进。"""
    d = layout.normalize_date(date)
    current = get_watermark(root, dataset)
    if current is not None and d <= current:
        return False
    set_meta(root, f"watermark:{dataset}", d)
    return True
