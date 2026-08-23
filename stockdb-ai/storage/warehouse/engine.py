"""storage.warehouse.engine — DuckDB 查询/计算引擎（0.10.0 W3，D12 第 2/3 层）。

连接策略：每 root 一个常驻连接 + threading.Lock 全程串行（沿 mydb _rd_lock 模式）——
单进程（D9）内 MCP/HTTP/调度线程共用；个人研究量级无瓶颈。

SQL 面（读写全开 + 三护栏，用户拍板"最大权限"）：
  1. 单语句：一次 run_sql 只允许一条语句（信封结果形态唯一）
  2. facts 只读：语句文本含 facts/ 路径即拒（事实区唯一写入口是 sink；视图读不受影响）
  3. 行数上限/超时：SELECT 超 cap 截断（truncated 标记由信封承载）；
     超时经 watchdog 线程 interrupt()（尽力而为，见 docstring 已知限制）

视图：v_daily（日K 全分区，含物化复权列 adj_factor/open_fq/high_fq/low_fq/close_fq）/
v_daily_fq（= v_daily，复权列沉淀时一次计算，查询零 JOIN 零计算）/
v_codes（代码表）。指标 = 表宏（PARTITION BY code 保证时序窗口正确性；
ta_ma/ta_rsi/ta_macd），口径见 docs/design/warehouse.md。
"""
from __future__ import annotations

import threading
from pathlib import Path

import config

from . import catalog, layout


class WarehouseUnavailable(RuntimeError):
    """duckdb 缺失或仓库关闭（上层映射 DEPENDENCY_UNAVAILABLE）。"""


class GuardrailError(ValueError):
    """run_sql 护栏拒绝（上层映射 INVALID_ARGUMENT）。"""


_DAILY_GLOB = "daily/*/*/date=*.parquet"

_DAILY_EMPTY_COLUMNS = [
    ("code", "TEXT"), ("date", "DATE"), ("name", "TEXT"), ("is_st", "BOOLEAN"),
    ("open", "DOUBLE"), ("high", "DOUBLE"), ("low", "DOUBLE"), ("close", "DOUBLE"),
    ("pre_close", "DOUBLE"), ("volume", "DOUBLE"), ("amount", "DOUBLE"),
    ("turnover", "DOUBLE"), ("pct_chg", "DOUBLE"), ("amplitude", "DOUBLE"),
    ("vol_ratio", "DOUBLE"), ("pb", "DOUBLE"), ("pe_ttm", "DOUBLE"),
    ("total_share", "DOUBLE"), ("float_share", "DOUBLE"),
    ("total_mv", "DOUBLE"), ("float_mv", "DOUBLE"),
    ("adj_factor", "DOUBLE"), ("open_fq", "DOUBLE"), ("high_fq", "DOUBLE"),
    ("low_fq", "DOUBLE"), ("close_fq", "DOUBLE"),
]


class WarehouseEngine:
    """单连接 DuckDB 引擎：视图/宏注册 + run_sql 护栏。线程安全（内部锁串行）。"""

    def __init__(self, root: Path):
        import duckdb

        self.root = Path(root)
        self.root.mkdir(parents=True, exist_ok=True)
        self._lock = threading.Lock()
        self._con = duckdb.connect(str(layout.duckdb_path(self.root)))
        self._duckdb = duckdb
        self.refresh_views()

    # ---- 视图与宏 ----

    def refresh_views(self) -> None:
        """（重）注册视图与宏。沉淀任务写入后调用；glob 视图查询期求值，新分区自动可见。"""
        con = self._con
        facts = layout.facts_dir(self.root)
        daily_glob = (facts / _DAILY_GLOB).as_posix()
        has_daily = bool(list((facts / "daily").rglob("date=*.parquet"))) if (facts / "daily").is_dir() else False

        # 空仓期给类型正确的空视图（沉淀后 refresh 换成 parquet 视图）
        if has_daily:
            con.execute(
                f"CREATE OR REPLACE VIEW v_daily AS "
                f"SELECT * FROM read_parquet('{daily_glob}', hive_partitioning=true)"
            )
        else:
            # 空仓期给类型正确的空视图（列名加引号：name/is_st 等易撞关键字）
            cols = ", ".join(f"NULL::{t} \"{n}\"" for n, t in _DAILY_EMPTY_COLUMNS)
            con.execute(f"CREATE OR REPLACE VIEW v_daily AS SELECT {cols} WHERE FALSE")

        # 0.10.10 重构：v_daily_fq 直接读物化列（沉淀时一次计算，查询零 JOIN 零计算）
        con.execute(
            "CREATE OR REPLACE VIEW v_daily_fq AS "
            "SELECT * FROM v_daily"
        )
        con.execute("CREATE TABLE IF NOT EXISTS codes (code TEXT PRIMARY KEY, name TEXT)")
        con.execute("CREATE OR REPLACE VIEW v_codes AS SELECT * FROM codes")
        # 用户研究区默认可用（工具文档建议放 research schema——实测首建前直接
        # CREATE TABLE research.x 会 CatalogException，故初始化即预建）
        con.execute("CREATE SCHEMA IF NOT EXISTS research")
        self._register_macros()

    def _register_macros(self) -> None:
        con = self._con
        # 移动平均：n 日简单均线（窗口按 code 分区、date 排序；
        # 窗口不满 n 日为 NULL——对齐 pandas rolling 语义，避免"伪 MA5"）
        con.execute("""
            CREATE OR REPLACE MACRO ta_ma(n) AS TABLE
            SELECT code, date, close,
                   CASE WHEN count(close) OVER (
                            PARTITION BY code ORDER BY date
                            ROWS BETWEEN n - 1 PRECEDING AND CURRENT ROW) >= n
                        THEN avg(close) OVER (
                            PARTITION BY code ORDER BY date
                            ROWS BETWEEN n - 1 PRECEDING AND CURRENT ROW)
                   END AS ma
            FROM v_daily
        """)
        # RSI：Wilder 平滑（0.10.5 口径对齐 pybao/逆向实证：种子 = 首根价差本身
        # 直接递推 ag_t=(ag_{t-1}(n-1)+gain_t)/n，无 SMA 种子窗——从第二根 bar 起
        # 即有值；实测与 zhibiao 逐点差 <0.001）
        con.execute("""
            CREATE OR REPLACE MACRO ta_rsi(n) AS TABLE
            WITH RECURSIVE
            d AS (
                SELECT code, date, close,
                       close - lag(close) OVER w AS diff,
                       row_number() OVER w AS rn
                FROM v_daily
                WINDOW w AS (PARTITION BY code ORDER BY date)),
            r AS (
                SELECT code, date, rn,
                       greatest(diff, 0)::DOUBLE AS ag,
                       greatest(-diff, 0)::DOUBLE AS al
                FROM d WHERE rn = 2
                UNION ALL
                SELECT d.code, d.date, d.rn,
                       (r.ag * (n - 1) + greatest(d.diff, 0)) / n,
                       (r.al * (n - 1) + greatest(-d.diff, 0)) / n
                FROM r JOIN d ON d.code = r.code AND d.rn = r.rn + 1)
            SELECT r.code, r.date, d.close,
                   CASE WHEN r.al = 0 THEN CASE WHEN r.ag > 0 THEN 100.0 END
                        WHEN r.ag = 0 THEN 0.0
                        ELSE 100.0 - 100.0 / (1 + r.ag / r.al)
                   END AS rsi
            FROM r JOIN d ON d.code = r.code AND d.date = r.date
        """)
        # MACD：双 EMA（递归 CTE）+ 信号线 EMA；fast/slow/sig 为周期参数
        con.execute("""
            CREATE OR REPLACE MACRO ta_macd(fast, slow, sig) AS TABLE
            WITH RECURSIVE
            o AS (SELECT code, date, close,
                         row_number() OVER (PARTITION BY code ORDER BY date) rn
                  FROM v_daily),
            e AS (
                SELECT code, date, close, rn,
                       close::DOUBLE AS ef, close::DOUBLE AS es
                FROM o WHERE rn = 1
                UNION ALL
                SELECT o.code, o.date, o.close, o.rn,
                       2.0 / (fast + 1) * o.close + (1 - 2.0 / (fast + 1)) * e.ef,
                       2.0 / (slow + 1) * o.close + (1 - 2.0 / (slow + 1)) * e.es
                FROM e JOIN o ON o.code = e.code AND o.rn = e.rn + 1),
            m AS (SELECT code, date, close, rn, ef - es AS macd FROM e),
            s AS (
                SELECT code, date, close, rn, macd, macd::DOUBLE AS signal
                FROM m WHERE rn = 1
                UNION ALL
                SELECT m.code, m.date, m.close, m.rn, m.macd,
                       2.0 / (sig + 1) * m.macd + (1 - 2.0 / (sig + 1)) * s.signal
                FROM s JOIN m ON m.code = s.code AND m.rn = s.rn + 1)
            SELECT code, date, close, macd, signal, macd - signal AS hist FROM s
        """)

    # ---- run_sql ----

    @staticmethod
    def _jsonable(v):
        """结果值 JSON 安全化：date/datetime → ISO 字符串；Decimal → float。

        0.10.4：MCP 通道 json.dumps 遇 DATE 列抛 TypeError（实测 ta_ma 查询经
        warehouse_run_sql 必现）——统一在引擎出口转换，MCP/HTTP 两侧免处理。
        """
        import datetime as _dt
        import decimal as _decimal
        if isinstance(v, (_dt.date, _dt.datetime)):
            return v.isoformat()
        if isinstance(v, _decimal.Decimal):
            return float(v)
        return v

    def run_sql(self, sql: str) -> dict:
        """执行单条 SQL（读写全开）。

        返回 {"kind": "rows"|"count", "columns", "rows", "row_count",
              "truncated", "statement_type"}；
        护栏/超时/语法错误分别抛 GuardrailError / TimeoutError / duckdb 异常。
        """
        statements = self._duckdb.extract_statements(sql)
        if len(statements) != 1:
            raise GuardrailError("run_sql 仅接受单条语句")
        stmt = statements[0]
        stmt_sql = stmt.query
        if "facts/" in stmt_sql.lower().replace("\\", "/"):
            raise GuardrailError("facts/ 为不可变事实区：只可经视图读取（v_daily/v_daily_fq），写入仅经沉淀任务")

        timeout = max(1, int(config.WAREHOUSE_QUERY_TIMEOUT))
        timer = threading.Timer(timeout, self._con.interrupt)
        try:
            with self._lock:
                timer.start()  # 拿到锁后再计时，避免误打断他人持锁查询
                result = self._con.execute(stmt_sql)
                columns = [d[0] for d in (result.description or [])]
                if columns == ["Count"]:
                    row = result.fetchone()
                    n = row[0] if row else 0  # DDL 亦返回 Count 形态但无行
                    return {"kind": "count", "columns": ["count"], "rows": [[n]],
                            "row_count": 1, "truncated": False,
                            "statement_type": str(getattr(stmt, "type", "unknown"))}
                cap = max(1, int(config.WAREHOUSE_ROW_CAP))
                rows = result.fetchmany(cap + 1)
                truncated = len(rows) > cap
                if truncated:
                    rows = rows[:cap]
                rows = [list(r) for r in rows]  # tuple → list（JSON 序列化友好）
                rows = [[self._jsonable(v) for v in row] for row in rows]
                return {"kind": "rows", "columns": columns, "rows": rows,
                        "row_count": len(rows), "truncated": truncated,
                        "statement_type": str(getattr(stmt, "type", "unknown"))}
        except self._duckdb.InterruptException as exc:
            raise TimeoutError(f"query exceeded {timeout}s (interrupted)") from exc
        finally:
            timer.cancel()

    # ---- 状态与清单 ----

    def status(self) -> dict:
        dates = layout.list_daily_dates(self.root)
        return {
            "root": str(self.root),
            "watermark_daily": catalog.get_watermark(self.root, "daily"),
            "sedimented_dates": len(dates),
            "first_date": dates[0] if dates else None,
            "latest_date": dates[-1] if dates else None,
            "codes": self._count("codes"),
            "duckdb_version": self._duckdb.__version__,
        }

    def list_objects(self) -> dict:
        tables = [r[0] for r in self._con.execute("SHOW TABLES").fetchall()]
        macros = self._con.execute(
            "SELECT function_name, parameters FROM duckdb_functions() "
            "WHERE function_type IN ('macro', 'table_macro') "
            "  AND function_name LIKE 'ta\\_%' ESCAPE '\\' "
            "ORDER BY function_name"
        ).fetchall()
        return {"tables": tables,
                "macros": [{"name": n, "parameters": p} for n, p in macros]}

    def _count(self, table: str) -> int:
        with self._lock:
            return self._con.execute(f"SELECT count(*) FROM {table}").fetchone()[0]

    def close(self) -> None:
        with self._lock:
            self._con.close()


# ---- 模块级单例（组合根/接口层用；测试自建 WarehouseEngine(tmpdir)） ----

_engine: WarehouseEngine | None = None
_engine_lock = threading.Lock()


def get_engine() -> WarehouseEngine:
    """按 config.WAREHOUSE_DIR 的惰性单例（动态读 config，测试可 patch 后 reset）。"""
    global _engine
    with _engine_lock:
        if _engine is not None and str(_engine.root) == str(layout.root_dir()):
            return _engine
        if _engine is not None:
            _engine.close()
            _engine = None
        _engine = WarehouseEngine(layout.root_dir())
        return _engine


def reset_engine() -> None:
    """测试隔离：关闭并清空单例。"""
    global _engine
    with _engine_lock:
        if _engine is not None:
            _engine.close()
            _engine = None
