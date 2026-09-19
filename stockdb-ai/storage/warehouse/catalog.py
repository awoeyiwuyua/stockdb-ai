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


def _connect(root: Path):
    import duckdb

    root = Path(root)
    root.mkdir(parents=True, exist_ok=True)
    con = duckdb.connect(str(layout.duckdb_path(root)))
    con.execute(_SCHEMA)
    con.execute(_ADJUST_EVENTS_SCHEMA)
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
