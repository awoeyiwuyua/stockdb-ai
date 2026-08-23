"""storage.warehouse.sink — 事实写入（0.10.0 W2；facts/ 的唯一写入口）。

不变量（docs/design/warehouse.md）：
  - facts/ 只增不改：分区文件已存在即跳过（幂等），不存在"改写历史"路径
  - 原子可见：临时文件 → COPY → 原子 rename；失败清理临时文件，绝不留半文件
  - 护栏：数值列非有限（NaN/Inf）或缺失的行拒写并计数（沿 research_store 教训）
  - watermark 随写推进（经 catalog，只前进）

数值语义：字段值按引擎原样镜像（不换算单位），仓库不做任何口径加工。
"""
from __future__ import annotations

import math
import os
from pathlib import Path

from . import catalog, layout

# 日K列定义（引擎日K字段原样：date 转 DATE 类型便于 SQL 区间/年份运算）
# 0.10.10：末尾追加物化复权伴随列（adj_factor + 4 fq 价格）——沉淀时一次计算、
# 查询零计算（取代查询时 ASOF JOIN）；事件未就绪时 5 列为 NULL（原价）
_DAILY_COLUMNS = (
    # 引擎日K原生字段（21 列，0.10.7 起原样镜像：不改名/不裁剪——此前 11 列且
    # pre_close 被改名 prev_close，直连通道全量被护栏误拒的根因）
    ("code", "TEXT"), ("date", "DATE"), ("name", "TEXT"), ("is_st", "BOOLEAN"),
    ("open", "DOUBLE"), ("high", "DOUBLE"), ("low", "DOUBLE"), ("close", "DOUBLE"),
    ("pre_close", "DOUBLE"), ("volume", "DOUBLE"), ("amount", "DOUBLE"),
    ("turnover", "DOUBLE"), ("pct_chg", "DOUBLE"), ("amplitude", "DOUBLE"),
    ("vol_ratio", "DOUBLE"), ("pb", "DOUBLE"), ("pe_ttm", "DOUBLE"),
    ("total_share", "DOUBLE"), ("float_share", "DOUBLE"),
    ("total_mv", "DOUBLE"), ("float_mv", "DOUBLE"),
    # 物化复权伴随列（0.10.10）：adj_factor=当日累计因子；*_fq = 原价 × factor
    ("adj_factor", "DOUBLE"), ("open_fq", "DOUBLE"), ("high_fq", "DOUBLE"),
    ("low_fq", "DOUBLE"), ("close_fq", "DOUBLE"),
)
_NUMERIC_FIELDS = ("open", "high", "low", "close", "prev_close", "volume", "amount")


def _finite(value) -> bool:
    """数值字段护栏：None / NaN / Inf 拒写。"""
    if value is None:
        return False
    if isinstance(value, float) and not math.isfinite(value):
        return False
    return True


def _normalize_rows(rows: list[dict], columns):
    """dict 行 → 元组行（镜像语义，0.10.7）：字段按列名原样取值，缺字段=NULL；
    NaN/Inf 消毒为 NULL（存脏浮点会毒化查询；拒绝整行则违背"读到什么写什么"）。
    返回 (rows, sanitized_cells)。"""
    import math as _math
    out, sanitized = [], 0
    for r in rows:
        row = []
        for name, ctype in columns:
            v = r.get(name)
            if isinstance(v, float) and not _math.isfinite(v):
                v, sanitized = None, sanitized + 1
            elif ctype == "DATE" and v is not None:
                v = layout.iso_date(v)  # 8 位/ISO → ISO（DATE 列统一：date/snapshot 等）
            elif name == "is_st":
                v = bool(v) if v is not None else None
            row.append(v)
        out.append(tuple(row))
    return out, sanitized


def _write_parquet_atomic(rows: list[tuple], columns, target: Path) -> None:
    """元组行 → 排序写入临时文件 → 原子 rename。失败清理临时文件。"""
    import duckdb

    target.parent.mkdir(parents=True, exist_ok=True)
    tmp = target.with_name(target.name + ".tmp")
    col_defs = ", ".join(f"{n} {t}" for n, t in columns)
    names = [n for n, _ in columns]
    try:
        con = duckdb.connect()
        try:
            con.execute(f"CREATE TABLE t ({col_defs})")
            if rows:
                placeholders = ", ".join("?" for _ in names)
                con.executemany(f"INSERT INTO t VALUES ({placeholders})", rows)
            order_by = "code" if "code" in names else names[0]
            con.execute(
                f"COPY (SELECT * FROM t ORDER BY {order_by}) TO '{tmp.as_posix()}' (FORMAT PARQUET)"
            )
        finally:
            con.close()
        os.replace(tmp, target)
    finally:
        if tmp.exists():
            tmp.unlink()


def write_daily(root: Path, date, rows: list[dict],
                factor_map: dict[str, float] | None = None) -> dict:
    """沉淀一个交易日的全市场日K（按市场分分区文件，幂等）。

    factor_map（0.10.10）：{code: 当日累计因子}——沉淀时一次计算物化复权列
    （adj_factor + open_fq/high_fq/low_fq/close_fq = 原价 × factor）；
    None/缺码 → 复权列 NULL（原价，事件未就绪）。查询层零计算。

    返回 {status: written|skipped|empty, markets, rows, dropped_nonfinite, watermark}。
    """
    date = layout.normalize_date(date)
    by_market: dict[str, list[tuple]] = {}
    dropped = 0
    for r in rows:
        market = layout.market_of(r.get("code", ""))
        row = {"date": date, **r}  # 快照行不带日期，由任务层日期注入
        code = str(r.get("code") or "")
        factor = (factor_map or {}).get(code)
        if factor is not None:
            try:
                factor = float(factor)
            except (TypeError, ValueError):
                factor = None
            if factor is not None and not math.isfinite(factor):
                factor = None
        if factor is not None:
            row.update(adj_factor=factor,
                       open_fq=row.get("open") * factor,
                       high_fq=row.get("high") * factor,
                       low_fq=row.get("low") * factor,
                       close_fq=row.get("close") * factor)
        normalized, d = _normalize_rows([row], _DAILY_COLUMNS)
        dropped += d  # 0.10.7 起为消毒单元格计数（不再丢行）
        if normalized:
            by_market.setdefault(market, []).append(normalized[0])

    if not by_market:
        # 完全无有效行也推进 watermark（空交易日不阻塞后续任务判断）
        advanced = catalog.set_watermark(root, "daily", date)
        return {"status": "empty", "markets": [], "rows": 0,
                "dropped_nonfinite": dropped, "watermark_advanced": advanced}

    written_markets = []
    total = 0
    for market, market_rows in sorted(by_market.items()):
        target = layout.daily_partition(root, date, market)
        if target.exists():
            continue  # 只增不改：已存在分区跳过（幂等）
        _write_parquet_atomic(market_rows, _DAILY_COLUMNS, target)
        written_markets.append(market)
        total += len(market_rows)

    advanced = catalog.set_watermark(root, "daily", date)
    return {
        "status": "written" if written_markets else "skipped",
        "markets": written_markets,
        "rows": total,
        "dropped_nonfinite": dropped,
        "watermark_advanced": advanced,
    }


def write_codes(root: Path, rows: list[dict]) -> dict:
    """代码表全量刷新（warehouse.duckdb 内表，非 facts——它是"当前状态"不是"事实"）。"""
    import duckdb

    con = duckdb.connect(str(layout.duckdb_path(root)))
    try:
        con.execute("CREATE TABLE IF NOT EXISTS codes (code TEXT PRIMARY KEY, name TEXT)")
        con.execute("DELETE FROM codes")
        data = [(str(r.get("code", "")).strip(), r.get("name")) for r in rows
                if str(r.get("code", "")).strip()]
        if data:
            con.executemany("INSERT INTO codes VALUES (?, ?)", data)
    finally:
        con.close()
    return {"status": "written", "rows": len(data)}
