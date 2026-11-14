"""storage.warehouse.sink — 事实写入（0.10.0 W2；facts/ 的唯一写入口）。

不变量（docs/design/warehouse.md）：
  - facts/ 只增不改：分区文件已存在即跳过（幂等），不存在"改写历史"路径
  - 原子可见：临时文件 → COPY → 原子 rename；失败清理临时文件，绝不留半文件
  - 护栏：数值列非有限（NaN/Inf）或缺失的行拒写并计数（沿 research_store 教训）
  - watermark 随写推进（经 catalog，只前进）

数值语义：字段值按引擎原样镜像（不换算单位），仓库不做任何口径加工。
"""
from __future__ import annotations

from collections.abc import Callable, Iterable
from datetime import datetime as _dt
import math
import os
from pathlib import Path

from . import catalog, layout

# 日K列定义（引擎日K字段原样：date 转 DATE 类型便于 SQL 区间/年份运算）
# 0.10.10：末尾追加物化复权伴随列（adj_factor + 4 fq 价格）——沉淀时一次计算、
# 查询零计算（取代查询时 ASOF JOIN）；事件未就绪时 5 列为 NULL（原价）
_DAILY_COLUMNS = (
    # 引擎日K原生字段（21 列，0.10.7 起原样镜像：不改名/不裁剪）。
    # ⚠️ 列名以引擎快照键为准（prev_close，0.10.6 写入即此名）；0.10.7 扩 26 列时
    # 此行曾误写 pre_close——快照键 prev_close 取不到值 → 该列全 NULL，且聚合 SQL
    # 同步引用旧名致 Binder Error（0.10.14 纠正；fixture 守护断言防再脱节）。
    ("code", "TEXT"), ("date", "DATE"), ("name", "TEXT"), ("is_st", "BOOLEAN"),
    ("open", "DOUBLE"), ("high", "DOUBLE"), ("low", "DOUBLE"), ("close", "DOUBLE"),
    ("prev_close", "DOUBLE"), ("volume", "DOUBLE"), ("amount", "DOUBLE"),
    ("turnover", "DOUBLE"), ("pct_chg", "DOUBLE"), ("amplitude", "DOUBLE"),
    ("vol_ratio", "DOUBLE"), ("pb", "DOUBLE"), ("pe_ttm", "DOUBLE"),
    ("total_share", "DOUBLE"), ("float_share", "DOUBLE"),
    ("total_mv", "DOUBLE"), ("float_mv", "DOUBLE"),
    # 物化复权伴随列（0.10.10）：adj_factor=当日累计因子；*_fq = 原价 × factor
    ("adj_factor", "DOUBLE"), ("open_fq", "DOUBLE"), ("high_fq", "DOUBLE"),
    ("low_fq", "DOUBLE"), ("close_fq", "DOUBLE"),
)
DAILY_BASE_COLUMNS = _DAILY_COLUMNS[:21]
_NUMERIC_FIELDS = ("open", "high", "low", "close", "prev_close", "volume", "amount")

# 分钟K 标准化列定义（0.17.x：首期仅存 period=1m 原始量价 9 列，不物化复权列）
_MINUTE_COLUMNS = (
    ("code", "TEXT"),
    ("date", "DATE"),
    ("datetime", "TIMESTAMP"),
    ("open", "DOUBLE"),
    ("high", "DOUBLE"),
    ("low", "DOUBLE"),
    ("close", "DOUBLE"),
    ("volume", "DOUBLE"),
    ("amount", "DOUBLE"),
)

_PARQUET_COMPRESSIONS = frozenset({
    "UNCOMPRESSED", "SNAPPY", "GZIP", "ZSTD", "BROTLI", "LZ4", "LZ4_RAW",
})


def _finite(value) -> bool:
    """数值字段护栏：None / NaN / Inf 拒写。"""
    if value is None:
        return False
    if isinstance(value, float) and not math.isfinite(value):
        return False
    return True


def _normalize_timestamp(value) -> str:
    """时间戳输入 → ISO TIMESTAMP 文本（YYYY-MM-DD HH:MM:SS[.ffffff]）。"""
    if isinstance(value, _dt):
        dt = value.replace(tzinfo=None) if value.tzinfo is not None else value
        return dt.strftime("%Y-%m-%d %H:%M:%S.%f") if dt.microsecond else dt.strftime("%Y-%m-%d %H:%M:%S")
    s = str(value).strip()
    if not s:
        raise ValueError(f"invalid timestamp: {value!r}")
    if len(s) == 14 and s.isdigit():
        return _dt.strptime(s, "%Y%m%d%H%M%S").strftime("%Y-%m-%d %H:%M:%S")
    if len(s) == 12 and s.isdigit():
        return _dt.strptime(s, "%Y%m%d%H%M").strftime("%Y-%m-%d %H:%M:%S")
    iso_s = s[:-1] if s.endswith("Z") else s
    try:
        dt = _dt.fromisoformat(iso_s)
    except ValueError as exc:
        raise ValueError(f"invalid timestamp: {value!r}") from exc
    if dt.tzinfo is not None:
        dt = dt.replace(tzinfo=None)
    return dt.strftime("%Y-%m-%d %H:%M:%S.%f") if dt.microsecond else dt.strftime("%Y-%m-%d %H:%M:%S")


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
            elif ctype == "DOUBLE" and v is not None:
                if isinstance(v, bool):
                    v, sanitized = None, sanitized + 1
                else:
                    try:
                        fv = float(v)
                    except (TypeError, ValueError):
                        v, sanitized = None, sanitized + 1
                    else:
                        if not _math.isfinite(fv):
                            v, sanitized = None, sanitized + 1
            elif ctype == "DATE" and v is not None:
                if isinstance(v, _dt):
                    v = v.strftime("%Y-%m-%d")
                else:
                    sv = str(v).strip()
                    if len(sv) in (12, 14) and sv.isdigit():
                        v = layout.iso_date(sv[:8])
                    else:
                        v = layout.iso_date(v)  # 8 位/ISO → ISO（DATE 列统一：date/snapshot 等）
            elif ctype == "TIMESTAMP" and v is not None:
                v = _normalize_timestamp(v)
            elif name == "is_st":
                v = bool(v) if v is not None else None
            row.append(v)
        out.append(tuple(row))
    return out, sanitized


def _load_rows(con, table: str, rows: list[tuple], columns) -> None:
    """元组行批量入表（0.10.34）。

    0.10.33 优化：改用「临时 CSV → read_csv 批量导入」，替代逐行
    `executemany(INSERT ... VALUES (?,...))`——后者在 fnOS ARM NAS 上对
    5176×26 行实测 16.6s/天（栈上逐行绑定参数，CPU 单核 ~9%，是 backfill
    每天 ~40s+ 的主因）。同数据 CSV 通道实测 0.10s（≈160×）；全量日K写入
    在回补/日常沉淀都走此路径。

    语义保持：None → 空字段（read_csv 默认空 = NULL）；bool → true/false；
    DATE 列 ISO 文本；值内含逗号/引号/换行由 csv 模块加引号、read_csv 还原。
    """
    import csv
    import tempfile

    col_defs = ", ".join(f"{n} {t}" for n, t in columns)
    con.execute(f"CREATE TABLE IF NOT EXISTS {table} ({col_defs})")
    if not rows:
        return

    def _cell(v):
        if v is None:
            return ""
        if isinstance(v, bool):
            return "true" if v else "false"
        return v

    fd, csv_path = tempfile.mkstemp(suffix=".csv")
    try:
        with os.fdopen(fd, "w", newline="", encoding="utf-8") as f:
            writer = csv.writer(f)
            for row in rows:
                writer.writerow([_cell(v) for v in row])
        colspec = ", ".join(f"'{n}': '{t}'" for n, t in columns)
        con.execute(
            f"INSERT INTO {table} SELECT * FROM read_csv("
            f"'{csv_path.replace(chr(92), '/')}', header=false, columns={{{colspec}}})"
        )
    finally:
        try:
            os.unlink(csv_path)
        except OSError:
            pass


def _write_parquet_atomic(
    rows: list[tuple],
    columns,
    target: Path,
    compression: str | None = None,
) -> None:
    """元组行 → 排序写入临时文件 → 原子 rename。失败清理临时文件。"""
    import duckdb

    fmt_opts = "FORMAT PARQUET"
    if compression is not None:
        comp = str(compression).strip().upper()
        if comp not in _PARQUET_COMPRESSIONS:
            raise ValueError(f"unsupported parquet compression: {compression!r}")
        fmt_opts = f"FORMAT PARQUET, COMPRESSION {comp}"

    target.parent.mkdir(parents=True, exist_ok=True)
    tmp = target.with_name(target.name + ".tmp")
    names = [n for n, _ in columns]
    try:
        con = duckdb.connect()
        try:
            _load_rows(con, "t", rows, columns)
            if "code" in names and "datetime" in names:
                order_by = "code, datetime"
            elif "code" in names:
                order_by = "code"
            else:
                order_by = names[0]
            con.execute(
                f"COPY (SELECT * FROM t ORDER BY {order_by}) TO '{tmp.as_posix()}' ({fmt_opts})"
            )
        finally:
            con.close()
        os.replace(tmp, target)
    finally:
        if tmp.exists():
            tmp.unlink()


def write_daily(root: Path, date, rows: list[dict],
                factor_map: dict[str, float] | None = None,
                *, advance_watermark: bool = True) -> dict:
    """沉淀一个交易日的全市场日K（按市场分分区文件，幂等）。

    factor_map（0.10.10）：{code: 当日累计因子}——沉淀时一次计算物化复权列
    （adj_factor + open_fq/high_fq/low_fq/close_fq = 原价 × factor）；
    None/缺码 → 复权列 NULL（原价，事件未就绪）。查询层零计算。
    advance_watermark=False 允许历史回填只补分区而不推进 watermark:daily。

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
            def _mul(val):
                return val * factor if isinstance(val, (int, float)) and not isinstance(val, bool) and math.isfinite(float(val)) else None

            row.update(adj_factor=factor,
                       open_fq=_mul(row.get("open")),
                       high_fq=_mul(row.get("high")),
                       low_fq=_mul(row.get("low")),
                       close_fq=_mul(row.get("close")))
        normalized, d = _normalize_rows([row], _DAILY_COLUMNS)
        dropped += d  # 0.10.7 起为消毒单元格计数（不再丢行）
        if normalized:
            by_market.setdefault(market, []).append(normalized[0])

    if not by_market:
        # 完全无有效行也推进 watermark（空交易日不阻塞后续任务判断）
        advanced = catalog.set_watermark(root, "daily", date) if advance_watermark else False
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

    advanced = catalog.set_watermark(root, "daily", date) if advance_watermark else False
    return {
        "status": "written" if written_markets else "skipped",
        "markets": written_markets,
        "rows": total,
        "dropped_nonfinite": dropped,
        "watermark_advanced": advanced,
    }


def write_minute(
    root: Path,
    date,
    rows: list[dict],
    *,
    period: str = "1m",
    code_predicate: Callable[[str], bool] | None = None,
    allowed_markets: tuple[str, ...] | Iterable[str] | None = ("sh", "sz"),
    overwrite: bool = False,
    advance_watermark: bool = True,
) -> dict:
    """沉淀一个交易日的标准分钟K（默认 period=1m，按市场 sh/sz 分区，ZSTD 压缩，(code, datetime) 排序）。

    - 不冗余物化 adj_factor / *_fq（查询时按 (code, date) 关联 v_daily）。
    - NaN/Inf 数值单元格消毒为 NULL 并计入 dropped_nonfinite。
    - 默认幂等跳过已存在分区；overwrite=True 时原子覆盖目标分区。
    - advance_watermark=False 允许历史回填而不推进 watermark:minute。

    返回 {status: written|skipped|empty, markets, rows, dropped_nonfinite, filtered, watermark_advanced}。
    """
    root = Path(root)
    date8 = layout.normalize_date(date)
    period_norm = str(period).strip().lower()
    if period_norm not in layout.MINUTE_PERIODS:
        raise ValueError(f"invalid minute period: {period!r}（须在 {layout.MINUTE_PERIODS}）")
    allowed = set(allowed_markets) if allowed_markets is not None else None

    rows_by_market: dict[str, list[dict]] = {}
    filtered = 0
    for r in rows:
        code = str(r.get("code") or "").strip()
        if not code:
            filtered += 1
            continue
        if code_predicate is not None and not code_predicate(code):
            filtered += 1
            continue
        market = layout.market_of(code)
        if allowed is not None and market not in allowed:
            filtered += 1
            continue

        raw_dt = r.get("datetime")
        raw_d = r.get("date")
        if raw_dt is None and raw_d is not None:
            if isinstance(raw_d, _dt):
                raw_dt = raw_d
            else:
                sd = str(raw_d).strip()
                if (len(sd) in (12, 14) and sd.isdigit()) or ":" in sd or "T" in sd or " " in sd:
                    raw_dt = raw_d

        row = dict(r)
        row["code"] = code
        if raw_dt is not None:
            dt_iso = _normalize_timestamp(raw_dt)
            row["datetime"] = dt_iso
            if raw_d is None or raw_d is raw_dt or str(raw_d).strip() == str(raw_dt).strip():
                row["date"] = dt_iso[:10]
        if row.get("date") is None:
            row["date"] = date8

        rows_by_market.setdefault(market, []).append(row)

    by_market: dict[str, list[tuple]] = {}
    dropped = 0
    for market, mkt_dicts in rows_by_market.items():
        normalized, d = _normalize_rows(mkt_dicts, _MINUTE_COLUMNS)
        dropped += d
        if normalized:
            by_market[market] = normalized

    if not by_market:
        advanced = catalog.set_watermark(root, "minute", date8) if advance_watermark else False
        return {
            "status": "empty",
            "markets": [],
            "rows": 0,
            "dropped_nonfinite": dropped,
            "filtered": filtered,
            "watermark_advanced": advanced,
        }

    written_markets = []
    total = 0
    for market, market_rows in sorted(by_market.items()):
        target = layout.minute_partition(root, period_norm, date8, market)
        if target.exists() and not overwrite:
            continue
        _write_parquet_atomic(market_rows, _MINUTE_COLUMNS, target, compression="ZSTD")
        written_markets.append(market)
        total += len(market_rows)

    advanced = catalog.set_watermark(root, "minute", date8) if advance_watermark else False
    return {
        "status": "written" if written_markets else "skipped",
        "markets": written_markets,
        "rows": total,
        "dropped_nonfinite": dropped,
        "filtered": filtered,
        "watermark_advanced": advanced,
    }


def write_codes(root: Path, rows: list[dict]) -> dict:
    """代码表全量刷新（warehouse.duckdb 内表，非 facts——它是"当前状态"不是"事实"）。

    0.10.34：改用 _load_rows 的 CSV 批量导入（0.10.33 优化）替代逐行 executemany——
    5176 行实测 24s → 亚秒级。含主键表同样走批量导入后逐行建索引。
    """
    import duckdb

    data = [(str(r.get("code", "")).strip(), r.get("name")) for r in rows
            if str(r.get("code", "")).strip()]
    con = duckdb.connect(str(layout.duckdb_path(root)))
    try:
        con.execute("CREATE TABLE IF NOT EXISTS codes (code TEXT PRIMARY KEY, name TEXT)")
        con.execute("DELETE FROM codes")
        if data:
            _load_rows(con, "codes", data, (("code", "TEXT"), ("name", "TEXT")))
    finally:
        con.close()
    return {"status": "written", "rows": len(data)}


def _kline_aggregate_sql(period_start, period_end, glob: str) -> str:
    """周/月K 聚合 SQL（0.10.12 抽取公共：周期边界参数化，week/month 同口径）。

    period_start/period_end：8 位日期（"20260817"）或 SQL 日期表达式字符串
    （如 "date_trunc('week', current_date)::DATE"，供派生视图用）。
    聚合语义（K 线周期口径，与 docs/design/warehouse.md §2.4 一致）：
      open/close      = 周期内首/末日开盘/收盘（first/last by date）
      high/low        = 周期内最高/最低（max/min）
      volume/amount/turnover = 周期内求和
      pct_chg         = (末 close - 首日 prev_close) / 首日 prev_close（周期涨跌幅，重算）
      amplitude       = (high - low) / 首日 prev_close（周期振幅，重算）
      vol_ratio       = NULL（周期级无定义）
      is_st/name/pb/pe_ttm/市值类 = 周期末日（last by date）
      adj_factor      = 周期末日因子；open_fq/high_fq/low_fq/close_fq = 物化列同规则
    """
    def _expr(v):
        s = str(v).strip()
        # 8 位日期 → DATE 'YYYY-MM-DD' 字面量；否则视为 SQL 表达式原样嵌入
        if len(s) == 8 and s.isdigit():
            return f"DATE '{layout.iso_date(s)}'"
        return s
    start_expr, end_expr = _expr(period_start), _expr(period_end)
    # 数据源：glob 以 read_parquet 加载（分区聚合）；视图名（v_daily）直接 FROM
    source = f"read_parquet('{glob}', hive_partitioning=true)" if not glob.startswith("v_") else glob
    return f"""
        WITH w AS (
            SELECT * FROM {source}
            WHERE date BETWEEN {start_expr} AND {end_expr}
        )
        SELECT
            code,
            {end_expr} AS date,
            arg_max(name, date) AS name,
            arg_max(is_st, date) AS is_st,
            arg_min(open, date) AS open,
            max(high) AS high,
            min(low) AS low,
            arg_max(close, date) AS close,
            arg_min(prev_close, date) AS prev_close,
            sum(volume) AS volume,
            sum(amount) AS amount,
            sum(turnover) AS turnover,
            CASE WHEN arg_min(prev_close, date) > 0
                 THEN (arg_max(close, date) - arg_min(prev_close, date))
                      / arg_min(prev_close, date) * 100 END AS pct_chg,
            CASE WHEN arg_min(prev_close, date) > 0
                 THEN (max(high) - min(low)) / arg_min(prev_close, date) * 100 END AS amplitude,
            NULL AS vol_ratio,
            arg_max(pb, date) AS pb,
            arg_max(pe_ttm, date) AS pe_ttm,
            arg_max(total_share, date) AS total_share,
            arg_max(float_share, date) AS float_share,
            arg_max(total_mv, date) AS total_mv,
            arg_max(float_mv, date) AS float_mv,
            arg_max(adj_factor, date) AS adj_factor,
            arg_min(open_fq, date) AS open_fq,
            max(high_fq) AS high_fq,
            min(low_fq) AS low_fq,
            arg_max(close_fq, date) AS close_fq
        FROM w
        GROUP BY code
    """


def _write_kline_aggregate(root: Path, dataset: str, period_end, rows,
                           *, advance_watermark: bool = True) -> dict:
    """周/月K 聚合落盘公共（0.10.12）：按市场分分区文件（幂等；other 孤码跳过）。"""
    by_market: dict[str, list[tuple]] = {}
    for r in rows:
        code = str(r[0])
        market = layout.market_of(code)
        if market in ("sh", "sz", "bj", "hk"):
            by_market.setdefault(market, []).append(tuple(r))
    if not by_market:
        return {"status": "empty", "markets": [], "rows": 0}
    written_markets = []
    total = 0
    for market, market_rows in sorted(by_market.items()):
        if dataset == "week":
            target = layout.week_partition(root, period_end, market)
        else:
            target = layout.month_partition(root, period_end, market)
        if target.exists():
            continue  # 只增不改：已存在分区跳过（幂等）
        _write_parquet_atomic(market_rows, _DAILY_COLUMNS, target)
        written_markets.append(market)
        total += len(market_rows)
    if written_markets and advance_watermark:
        catalog.set_watermark(root, dataset, period_end)
    return {"status": "written" if written_markets else "skipped",
            "markets": written_markets, "rows": total}


def aggregate_weekly(root: Path, week_end, market: str | None = None,
                     *, advance_watermark: bool = True) -> dict:
    """周K聚合物化（0.10.10 粒度阶梯：facts/week/year=YYYY/market=xx/date=YYYYMMDD.parquet）。

    聚合源 = 该自然周（周一~周五）的 daily 分区（read_parquet glob，不重复拉引擎）；
    周K列与 daily 完全同构（26 列，含物化复权列）——查询面 v_daily 同模式复用。
    聚合语义见 _kline_aggregate_sql；幂等：周分区已存在即跳过；watermark:week 只前进。
    返回 {status: written|skipped|empty, markets, rows}。
    """
    import duckdb

    root = Path(root)
    week_end = layout.normalize_date(week_end)
    # 自然周起点 = week_end - 4 天（周一）；含节假日时该周交易日 < 5 也照常聚合
    from datetime import datetime, timedelta
    end_dt = datetime.strptime(week_end, "%Y%m%d")
    start_dt = end_dt - timedelta(days=4)
    start8 = start_dt.strftime("%Y%m%d")

    facts = layout.facts_dir(root)
    daily_glob = (facts / "daily" / "*" / "*" / "date=*.parquet").as_posix()
    con = duckdb.connect()
    try:
        rows = con.execute(_kline_aggregate_sql(start8, week_end, daily_glob)).fetchall()
    finally:
        con.close()
    if not rows:
        return {"status": "empty", "markets": [], "rows": 0}
    return _write_kline_aggregate(root, "week", week_end, rows, advance_watermark=advance_watermark)


def aggregate_monthly(root: Path, month_end, market: str | None = None,
                      *, advance_watermark: bool = True) -> dict:
    """月K聚合物化（0.10.12 粒度阶梯：facts/month/year=YYYY/market=xx/date=YYYYMMDD.parquet）。

    聚合源 = 该自然月（1 日~月末）的 daily 分区；月K列与 daily 完全同构（26 列，
    含物化复权列），聚合语义与周K同口径（见 _kline_aggregate_sql）；
    幂等：月分区已存在即跳过；watermark:month 只前进。
    返回 {status: written|skipped|empty, markets, rows}。
    """
    import duckdb

    root = Path(root)
    month_end = layout.normalize_date(month_end)
    # 自然月起点 = 当月 1 日
    month_start = month_end[:6] + "01"

    facts = layout.facts_dir(root)
    daily_glob = (facts / "daily" / "*" / "*" / "date=*.parquet").as_posix()
    con = duckdb.connect()
    try:
        rows = con.execute(_kline_aggregate_sql(month_start, month_end, daily_glob)).fetchall()
    finally:
        con.close()
    if not rows:
        return {"status": "empty", "markets": [], "rows": 0}
    return _write_kline_aggregate(root, "month", month_end, rows, advance_watermark=advance_watermark)
