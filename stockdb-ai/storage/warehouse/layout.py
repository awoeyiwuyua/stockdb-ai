"""storage.warehouse.layout — 分区路径与市场归类（0.10.0 W2，D12；0.10.10 粒度阶梯定稿）。

磁盘布局（七级粒度阶梯，docs/design/warehouse.md §2.2）：
  <root>/facts/tick/code=xxxxxx/date=YYYYMMDD/part=HHMM.parquet  逐笔（唯一按码：流式写入）
  <root>/facts/minute/period=5m/year=YYYY/market=xx/date=YYYYMMDD.parquet  分钟K（时间分层）
  <root>/facts/hour/year=YYYY/market=xx/date=YYYYMMDD.parquet    小时K（60m，时间分层）
  <root>/facts/daily/year=YYYY/market=xx/date=YYYYMMDD.parquet   日K（时间分层，现役）
  <root>/facts/week/year=YYYY/market=xx/date=YYYYMMDD.parquet    周K（daily 聚合物化）
  <root>/facts/month/year=YYYY/market=xx/date=YYYYMMDD.parquet   月K（daily 聚合物化）
  <root>/facts/year/year=YYYY/market=xx/date=YYYYMMDD.parquet    年K（daily 聚合物化）
  <root>/warehouse.duckdb                                        视图/宏 + research schema + meta 表
  <root>/backups/                                                warehouse.duckdb 备份

分层规则（0.10.10 用户拍板）：
  - 粒度阶梯：tick → minute → hour → daily → week → month → year，每级独立 dataset + watermark
  - 只有 tick 按 code 分层（流式写入，盘中逐码追加，避免按日写放大）；
    其余全部时间分层，且二级目录统一从 year=YYYY/market=xx 切入（与 daily 完全同构）——
    minute 家族（1m/5m/15m/30m）以 period 目录段区分，hour=60m
  - week/month/year 由 daily 本地级联聚合物化（沉淀时一次计算多次复用），同骨架同列
  - 复权：沉淀时经 factor_map 物化 adj_factor+fq 列进各粒度分区（查询零计算）；
    事件源是内存输入，不占 facts 目录
  - lhb/fundamental 等事件/快照类暂不占位（延后，接入时再定）

文件粒度选「年/市场/日」而非「每标的一文件」：全市场日K约 5000 行/日，
按日成文件既保持只增不改的追加语义，又避免每年数千小文件拖慢全表扫描；
单标的时序查询靠文件内 code 排序 + Parquet 行组统计裁剪。

市场归类与 app._classify_code 同域（交易所维度 sh/sz/bj；hk 并作 daily 的市场分区）。
"""
from __future__ import annotations

import re
from pathlib import Path

import config

_DATE_RE = re.compile(r"^(\d{4})(\d{2})(\d{2})$")

# 交易所前缀表（2-3 位代码段 → 市场）
_SH_PREFIXES = ("50", "51", "52", "56", "58", "60", "68")  # 沪主板/科创板/沪 ETF·LOF
_SZ_PREFIXES = ("00", "15", "16", "18", "30")  # 深主板/创业板/深 ETF·LOF·REITs
_BJ_PREFIXES = ("43", "83", "87", "88", "92")  # 北交所

# 分钟K 周期（period 目录段；hour=60m 独立 dataset）
MINUTE_PERIODS = ("1m", "5m", "15m", "30m")


def root_dir() -> Path:
    """仓库根（动态读 config，测试可 patch config.WAREHOUSE_DIR）。"""
    return Path(config.WAREHOUSE_DIR)


def normalize_date(value) -> str:
    """"2026-08-22" / "20260822" / 20260822 → "20260822"（8 位，布局与 watermark 统一口径）。"""
    s = str(value).replace("-", "").strip()
    if not _DATE_RE.match(s):
        raise ValueError(f"invalid date: {value!r}")
    return s


def iso_date(value) -> str:
    """任意合法日期输入 → "YYYY-MM-DD"（DuckDB DATE 列格式）。"""
    d = normalize_date(value)
    return f"{d[:4]}-{d[4:6]}-{d[6:]}"


def market_of(code) -> str:
    """代码 → 市场分区名（sh/sz/bj/hk/other）。"""
    c = str(code).strip().lower()
    if c.startswith("hk"):
        c = c[2:]
    if c.isdigit() and len(c) == 5:
        return "hk"
    if c[:2] in _SH_PREFIXES:
        return "sh"
    if c[:2] in _SZ_PREFIXES:
        return "sz"
    if c[:3] == "430" or c[:2] in _BJ_PREFIXES:
        return "bj"
    return "other"


def facts_dir(root: Path) -> Path:
    return Path(root) / "facts"


def duckdb_path(root: Path) -> Path:
    return Path(root) / "warehouse.duckdb"


def backups_dir(root: Path) -> Path:
    return Path(root) / "backups"


def _ts_dir(root: Path, dataset: str, date, market: str, period: str | None = None) -> Path:
    """时间序列统一骨架：facts/<dataset>[/period=xx]/year=YYYY/market=xx/date=YYYYMMDD.parquet
    （0.10.10：除 tick 外全部时间分层，二级目录统一 year/market 切入）。"""
    d = normalize_date(date)
    base = facts_dir(root) / dataset
    if period is not None:
        base = base / f"period={period}"
    return base / f"year={d[:4]}" / f"market={market}" / f"date={d}.parquet"


def daily_partition(root: Path, date, market: str) -> Path:
    """日K分区：facts/daily/year=YYYY/market=xx/date=YYYYMMDD.parquet（时间分层，现役）。"""
    return _ts_dir(root, "daily", date, market)


def minute_partition(root: Path, period: str, date, market: str) -> Path:
    """分钟K分区：facts/minute/period=5m/year=YYYY/market=xx/date=YYYYMMDD.parquet
    （period 目录段区分 1m/5m/15m/30m；hive 解析出 period 列）。"""
    period = str(period).lower()
    if period not in MINUTE_PERIODS:
        raise ValueError(f"invalid minute period: {period!r}（须在 {MINUTE_PERIODS}）")
    return _ts_dir(root, "minute", date, market, period=period)


def hour_partition(root: Path, date, market: str) -> Path:
    """小时K分区：facts/hour/year=YYYY/market=xx/date=YYYYMMDD.parquet（60m，时间分层）。"""
    return _ts_dir(root, "hour", date, market)


def week_partition(root: Path, date, market: str) -> Path:
    """周K分区：facts/week/year=YYYY/market=xx/date=YYYYMMDD.parquet（date=周结束日，daily 聚合）。"""
    return _ts_dir(root, "week", date, market)


def month_partition(root: Path, date, market: str) -> Path:
    """月K分区：facts/month/year=YYYY/market=xx/date=YYYYMMDD.parquet（date=月末，daily 聚合）。"""
    return _ts_dir(root, "month", date, market)


def year_partition(root: Path, date, market: str) -> Path:
    """年K分区：facts/year/year=YYYY/market=xx/date=YYYYMMDD.parquet（date=年末，daily 聚合）。"""
    return _ts_dir(root, "year", date, market)


def tick_partition(root: Path, code, date, part: str | None = None) -> Path:
    """逐笔分区：facts/tick/code=xxxxxx/date=YYYYMMDD[/part=HHMM]/data.parquet
    （唯一按码分层：流式写入、日内多窗口；part 为半小时窗口，接入时定粒度）。"""
    d = normalize_date(date)
    base = facts_dir(root) / "tick" / f"code={code}" / f"date={d}"
    if part is not None:
        base = base / f"part={part}"
    return base / "data.parquet"


def list_daily_dates(root: Path) -> list[str]:
    """已沉淀的交易日清单（按日期去重——同日多市场多文件；升序；无分区返回 []）。"""
    daily = facts_dir(root) / "daily"
    if not daily.is_dir():
        return []
    return sorted({f.stem[len("date="):] for f in daily.rglob("date=*.parquet")})
