"""test_warehouse — 列式仓库层测试（0.10.0，D12；随批次 W1→W5 增补）。

W1：availability 探针（duckdb 缺失/开关关闭降级）+ 层规则（services 禁直接依赖）。
W2：layout 分区规则 / sink 幂等·原子·独立可读·护栏 / catalog watermark 只前进。
后续批次：W3 engine/run_sql、W4 沉淀任务、W5 MCP 工具。
"""
import json
import pathlib
import sys
import tempfile
import unittest
from unittest import mock

import duckdb

WEBUI = pathlib.Path(__file__).resolve().parent
if str(WEBUI) not in sys.path:
    sys.path.insert(0, str(WEBUI))

import config
from storage import warehouse
from storage.warehouse import backup, catalog, layout, sink
from storage.warehouse.engine import GuardrailError, WarehouseEngine
from storage.warehouse.queries import WarehouseUnavailable


def _sample_rows() -> list[dict]:
    """三市场 + ETF + 北交所 的迷你全市场样本。"""
    return [
        {"code": "600000", "name": "浦发银行", "is_st": False,
         "open": 10.0, "high": 10.5, "low": 9.9, "close": 10.2, "prev_close": 10.0,
         "volume": 1234567.0, "amount": 12500000.0},
        {"code": "000001", "name": "平安银行", "is_st": False,
         "open": 11.0, "high": 11.2, "low": 10.8, "close": 11.1, "prev_close": 11.0,
         "volume": 2234567.0, "amount": 24500000.0},
        {"code": "300750", "name": "宁德时代", "is_st": False,
         "open": 200.0, "high": 205.0, "low": 198.0, "close": 203.0, "prev_close": 200.0,
         "volume": 3234567.0, "amount": 650000000.0},
        {"code": "920001", "name": "北交样本", "is_st": False,
         "open": 5.0, "high": 5.2, "low": 4.9, "close": 5.1, "prev_close": 5.0,
         "volume": 234567.0, "amount": 1200000.0},
        {"code": "510300", "name": "沪深300ETF", "is_st": None,
         "open": 4.0, "high": 4.02, "low": 3.98, "close": 4.01, "prev_close": 4.0,
         "volume": 8234567.0, "amount": 33000000.0},
        # 镜像语义用例（0.10.7）：close=NaN 的行不再被拒——消毒为 NULL 落盘
        {"code": "600001", "name": "脏行", "is_st": False,
         "open": 1.0, "high": 1.0, "low": 1.0, "close": float("nan"), "prev_close": 1.0,
         "volume": 1.0, "amount": 1.0},
    ]


def _guard_fixture_keys():
    """守护断言（0.10.14）：fixture 键名必须全部落在 _DAILY_COLUMNS 名集内。

    0.10.7 扩列时 _DAILY_COLUMNS 误写 pre_close 而引擎快照键是 prev_close——
    fixture 跟着用旧名，写入侧该列全 NULL、聚合 SQL Binder Error，测试因
    fixture/schema 同错而双双失真。此守护强制 fixture 与生产 schema 对齐：
    schema 演进（改名/删列）时先炸这里，而非周一实盘聚合。
    """
    known = {name for name, _ in sink._DAILY_COLUMNS}
    for row in _sample_rows():
        unknown = set(row) - known
        assert not unknown, f"fixture 键 {unknown} 不在 _DAILY_COLUMNS（schema 脱节）"


_guard_fixture_keys()


class WarehouseAvailabilityTest(unittest.TestCase):
    """W1 验收：duckdb 缺失 → (False, 说明)；开关关闭 → False；正常 → True。"""

    def test_available_when_duckdb_present_and_enabled(self):
        ok, note = warehouse.availability()
        self.assertTrue(ok, note)
        self.assertEqual(note, "ok")

    def test_disabled_by_switch(self):
        with mock.patch.object(config, "WAREHOUSE_ENABLED", False):
            ok, note = warehouse.availability()
        self.assertFalse(ok)
        self.assertIn("WAREHOUSE_ENABLED", note)

    def test_degrades_when_duckdb_missing(self):
        """模拟镜像内无 duckdb wheel：import 被拦 → 降级说明（DEPENDENCY_UNAVAILABLE 语义）。"""
        import builtins
        real_import = builtins.__import__

        def fake_import(name, *args, **kwargs):
            if name == "duckdb":
                raise ImportError("No module named 'duckdb'")
            return real_import(name, *args, **kwargs)

        with mock.patch("builtins.__import__", side_effect=fake_import):
            ok, note = warehouse.availability()
        self.assertFalse(ok)
        self.assertIn("duckdb unavailable", note)


class WarehouseLayoutTest(unittest.TestCase):
    """W2：分区路径规则与市场归类。"""

    def test_market_classification(self):
        self.assertEqual(layout.market_of("600000"), "sh")
        self.assertEqual(layout.market_of("688001"), "sh")
        self.assertEqual(layout.market_of("510300"), "sh")
        self.assertEqual(layout.market_of("000001"), "sz")
        self.assertEqual(layout.market_of("301001"), "sz")
        self.assertEqual(layout.market_of("159915"), "sz")
        self.assertEqual(layout.market_of("430047"), "bj")
        self.assertEqual(layout.market_of("920001"), "bj")
        self.assertEqual(layout.market_of("hk00700"), "hk")
        self.assertEqual(layout.market_of("200002"), "other")

    def test_normalize_date(self):
        self.assertEqual(layout.normalize_date("2026-08-22"), "20260822")
        self.assertEqual(layout.normalize_date(20260822), "20260822")
        with self.assertRaises(ValueError):
            layout.normalize_date("2026/08/22")

    def test_daily_partition_path(self):
        root = pathlib.Path("/tmp/wh")
        p = layout.daily_partition(root, "20260822", "sh")
        self.assertEqual(
            p.relative_to(root).as_posix(),
            "facts/daily/year=2026/market=sh/date=20260822.parquet",
        )

    def test_granularity_ladder_partition_paths(self):
        """0.10.10 粒度阶梯：时间序列统一 year/market 二级切入；tick 唯一按码分层。"""
        root = pathlib.Path("/tmp/wh")
        m = layout.minute_partition(root, "5m", "20260822", "sh")
        self.assertEqual(
            m.relative_to(root).as_posix(),
            "facts/minute/period=5m/year=2026/market=sh/date=20260822.parquet",
        )
        h = layout.hour_partition(root, "20260822", "sz")
        self.assertEqual(
            h.relative_to(root).as_posix(),
            "facts/hour/year=2026/market=sz/date=20260822.parquet",
        )
        w = layout.week_partition(root, "20260822", "sh")
        self.assertEqual(
            w.relative_to(root).as_posix(),
            "facts/week/year=2026/market=sh/date=20260822.parquet",
        )
        t = layout.tick_partition(root, "600000", "20260822", part="1030")
        self.assertEqual(
            t.relative_to(root).as_posix(),
            "facts/tick/code=600000/date=20260822/part=1030/data.parquet",
        )
        with self.assertRaises(ValueError):
            layout.minute_partition(root, "45m", "20260822", "sh")  # 非法周期


class WarehouseSinkTest(unittest.TestCase):
    """W2 验收：幂等 / 原子 / 独立可读 / NaN 护栏 / watermark 推进。"""

    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.root = pathlib.Path(self._tmp.name)

    def tearDown(self):
        self._tmp.cleanup()

    def test_write_daily_layout_and_independent_read(self):
        """写入落正确分区；文件可被独立 duckdb 连接直读（沿备份独立可读断言模式）。"""
        result = sink.write_daily(self.root, "20260822", _sample_rows())
        self.assertEqual(result["status"], "written")
        self.assertEqual(sorted(result["markets"]), ["bj", "sh", "sz"])
        self.assertEqual(result["rows"], 6)  # 0.10.7：NaN 消毒为 NULL，不再丢行
        self.assertEqual(result["dropped_nonfinite"], 1)  # 消毒单元格计数

        sh = layout.daily_partition(self.root, "20260822", "sh")
        sz = layout.daily_partition(self.root, "20260822", "sz")
        self.assertTrue(sh.exists() and sz.exists())

        con = duckdb.connect()  # 独立连接直读（不依赖写入连接）
        try:
            cols = {r[0] for r in con.execute(
                f"DESCRIBE SELECT * FROM read_parquet('{sh.as_posix()}')").fetchall()}
            self.assertIn("close", cols)
            self.assertIn("is_st", cols)
            rows = con.execute(
                f"SELECT code, close FROM read_parquet('{sz.as_posix()}') ORDER BY code"
            ).fetchall()
            self.assertEqual(rows[0], ("000001", 11.1))
        finally:
            con.close()

    def test_write_daily_idempotent(self):
        """同日双跑：第二次 skipped，行数与文件内容不变，无重复行。"""
        first = sink.write_daily(self.root, "20260822", _sample_rows())
        again = sink.write_daily(self.root, "20260822", _sample_rows())
        self.assertEqual(first["status"], "written")
        self.assertEqual(again["status"], "skipped")
        self.assertEqual(again["markets"], [])

        con = duckdb.connect()
        try:
            n = con.execute(
                f"SELECT count(*) FROM read_parquet("
                f"'{layout.facts_dir(self.root).as_posix()}/daily/*/*/date=*.parquet')"
            ).fetchone()[0]
            self.assertEqual(n, 6)  # 无重复（含消毒行）
        finally:
            con.close()
        # watermark 第二次不再推进
        self.assertFalse(again["watermark_advanced"])

    def test_write_daily_atomic_on_failure(self):
        """模拟 rename 中断：异常传播、无可见分区文件、无 .tmp 残留。"""
        with mock.patch("os.replace", side_effect=OSError("disk full")):
            with self.assertRaises(OSError):
                sink.write_daily(self.root, "20260822", _sample_rows())
        self.assertFalse(layout.daily_partition(self.root, "20260822", "sh").exists())
        leftovers = list(layout.facts_dir(self.root).rglob("*.tmp"))
        self.assertEqual(leftovers, [])

    def test_watermark_only_advances(self):
        d1 = layout.daily_partition(self.root, "20260822", "sh")
        self.assertFalse(d1.exists())
        self.assertTrue(catalog.set_watermark(self.root, "daily", "20260822"))
        self.assertEqual(catalog.get_watermark(self.root, "daily"), "20260822")
        # 回退与原地重写都不生效
        self.assertFalse(catalog.set_watermark(self.root, "daily", "20260801"))
        self.assertFalse(catalog.set_watermark(self.root, "daily", "20260822"))
        self.assertEqual(catalog.get_watermark(self.root, "daily"), "20260822")

    def test_daily_factor_materialization(self):
        """0.10.10：复权物化——factor_map 沉淀时一次计算落盘（adj_factor + fq 列），
        无因子行 fq 列 NULL；幂等重写不改变已物化值。"""
        rows = _sample_rows()[:1]  # 600000
        r1 = sink.write_daily(self.root, "20260822", rows, factor_map={"600000": 2.0})
        self.assertEqual(r1["status"], "written")
        con = duckdb.connect()
        try:
            p = layout.daily_partition(self.root, "20260822", "sh")
            got = con.execute(
                f"SELECT close, adj_factor, close_fq FROM read_parquet('{p.as_posix()}')"
            ).fetchone()
            self.assertAlmostEqual(got[0], 10.2)
            self.assertAlmostEqual(got[1], 2.0)
            self.assertAlmostEqual(got[2], 20.4)  # close × factor 物化
        finally:
            con.close()
        # 无 factor_map：fq 列 NULL（事件未就绪 → 原价）
        sink.write_daily(self.root, "20260825", rows[:1], factor_map=None)
        con = duckdb.connect()
        try:
            p2 = layout.daily_partition(self.root, "20260825", "sh")
            got2 = con.execute(
                f"SELECT adj_factor, close_fq FROM read_parquet('{p2.as_posix()}')"
            ).fetchone()
            self.assertIsNone(got2[0])
            self.assertIsNone(got2[1])
        finally:
            con.close()

    def test_write_codes(self):
        r = sink.write_codes(self.root, [{"code": "600000", "name": "浦发银行"},
                                         {"code": "", "name": "空代码拒收"}])
        self.assertEqual(r["rows"], 1)
        con = duckdb.connect(str(layout.duckdb_path(self.root)))
        try:
            self.assertEqual(
                con.execute("SELECT code, name FROM codes").fetchall(),
                [("600000", "浦发银行")],
            )
        finally:
            con.close()

    def test_empty_write_advances_watermark_only(self):
        result = sink.write_daily(self.root, "20260826", [])
        self.assertEqual(result["status"], "empty")
        self.assertTrue(result["watermark_advanced"])
        self.assertFalse(layout.facts_dir(self.root).exists())

    def test_aggregate_weekly_semantics(self):
        """0.10.10 周K聚合语义：open=周首日、high/low=max/min、close=周末日、
        volume/amount/turnover=求和、pct_chg/amplitude 重算、复权列同规则聚合。"""
        # 一周 5 个交易日，单只股票（600000，sh）
        week = [("20260817", 10.0, 11.0, 10.5, 1000.0, 10000.0, 1.0, 2.0),
                ("20260818", 10.5, 12.0, 10.8, 2000.0, 20000.0, 2.0, 2.0),
                ("20260819", 10.8, 13.0, 12.0, 3000.0, 30000.0, 3.0, 2.0),
                ("20260820", 12.0, 14.0, 13.0, 4000.0, 40000.0, 4.0, 2.0),
                ("20260821", 13.0, 15.0, 14.0, 5000.0, 50000.0, 5.0, 2.0)]
        for d, o, h, c, v, a, t, f in week:
            sink.write_daily(self.root, d, [{
                "code": "600000", "name": "样本", "is_st": False,
                "open": o, "high": h, "low": o - 0.5, "close": c,
                "prev_close": o - 0.2, "volume": v, "amount": a, "turnover": t,
            }], factor_map={"600000": f})

        res = sink.aggregate_weekly(self.root, "20260821")
        self.assertEqual(res["status"], "written")
        self.assertEqual(res["markets"], ["sh"])
        self.assertEqual(res["rows"], 1)

        con = duckdb.connect()
        try:
            p = layout.week_partition(self.root, "20260821", "sh")
            row = con.execute(
                f"SELECT code, open, high, low, close, prev_close, volume, amount, "
                f"turnover, pct_chg, amplitude, adj_factor, close_fq, open_fq "
                f"FROM read_parquet('{p.as_posix()}')"
            ).fetchone()
            code, open_, high, low, close, prev_close, vol, amt, to, pct, amp, fac, cfq, ofq = row
            self.assertEqual(code, "600000")
            self.assertAlmostEqual(open_, 10.0)      # 周一首日开盘
            self.assertAlmostEqual(high, 15.0)       # 周内最高
            self.assertAlmostEqual(low, 9.5)         # 周内最低（首日 open-0.5）
            self.assertAlmostEqual(close, 14.0)      # 周五收盘
            self.assertAlmostEqual(prev_close, 9.8)   # 周一首日 prev_close
            self.assertAlmostEqual(vol, 15000.0)     # 求和
            self.assertAlmostEqual(amt, 150000.0)    # 求和
            self.assertAlmostEqual(to, 15.0)         # 换手求和
            self.assertAlmostEqual(pct, (14.0 - 9.8) / 9.8 * 100)  # 周涨跌幅重算
            self.assertAlmostEqual(amp, (15.0 - 9.5) / 9.8 * 100)  # 周振幅重算
            self.assertAlmostEqual(fac, 2.0)         # 周末日因子
            self.assertAlmostEqual(cfq, 28.0)        # 周五 close × 周五因子
            self.assertAlmostEqual(ofq, 20.0)        # 周一 open × 周一因子
        finally:
            con.close()
        # watermark 推进
        self.assertEqual(catalog.get_watermark(self.root, "week"), "20260821")

    def test_aggregate_weekly_idempotent(self):
        """周聚合幂等：重复聚合 skipped，watermark 不回退，行数不变。"""
        for d in ("20260817", "20260818", "20260819", "20260820", "20260821"):
            sink.write_daily(self.root, d, [{
                "code": "600000", "name": "样本", "is_st": False,
                "open": 10.0, "high": 11.0, "low": 9.5, "close": 10.5,
                "prev_close": 9.8, "volume": 1000.0, "amount": 10000.0,
            }])
        first = sink.aggregate_weekly(self.root, "20260821")
        again = sink.aggregate_weekly(self.root, "20260821")
        self.assertEqual(first["status"], "written")
        self.assertEqual(again["status"], "skipped")
        self.assertEqual(catalog.get_watermark(self.root, "week"), "20260821")
        # 旧周聚合不推进 watermark（回看补聚合不覆盖已推进值）
        self.assertEqual(catalog.get_watermark(self.root, "week"), "20260821")

    def test_aggregate_weekly_skips_other_market(self):
        """other 兜底市场不沉淀周K（无意义孤码）。"""
        sink.write_daily(self.root, "20260817", [{
            "code": "200002", "name": "B股孤码", "is_st": False,
            "open": 1.0, "high": 1.1, "low": 0.9, "close": 1.0,
            "prev_close": 1.0, "volume": 1.0, "amount": 1.0,
        }])
        res = sink.aggregate_weekly(self.root, "20260817")
        self.assertEqual(res["status"], "empty")
        self.assertEqual(res["rows"], 0)

    def test_aggregate_monthly_semantics(self):
        """0.10.12 月K聚合语义：与周K同口径——open=月首日、close=月末日、
        high/low=max/min、量求和、pct_chg 重算。"""
        # 8 月 4 个交易日（简化为 0817~0820 跨周同月）
        for d, o, c in (("20260817", 10.0, 11.0), ("20260818", 11.0, 10.5),
                        ("20260819", 10.5, 12.0), ("20260820", 12.0, 13.0)):
            sink.write_daily(self.root, d, [{
                "code": "600000", "name": "样本", "is_st": False,
                "open": o, "high": max(o, c) + 0.5, "low": min(o, c) - 0.5,
                "close": c, "prev_close": o - 0.2,
                "volume": 1000.0, "amount": 10000.0, "turnover": 1.0,
            }])
        res = sink.aggregate_monthly(self.root, "20260831")
        self.assertEqual(res["status"], "written")
        self.assertEqual(res["rows"], 1)
        con = duckdb.connect()
        try:
            p = layout.month_partition(self.root, "20260831", "sh")
            row = con.execute(
                f"SELECT open, high, low, close, volume, pct_chg "
                f"FROM read_parquet('{p.as_posix()}')"
            ).fetchone()
            open_, high, low, close, vol, pct = row
            self.assertAlmostEqual(open_, 10.0)   # 月首日开盘
            self.assertAlmostEqual(close, 13.0)   # 月末日收盘
            self.assertAlmostEqual(high, 13.5)    # 月内最高
            self.assertAlmostEqual(low, 9.5)      # 月内最低（首日 low = min(10,11)-0.5）
            self.assertAlmostEqual(vol, 4000.0)   # 求和
            self.assertAlmostEqual(pct, (13.0 - 9.8) / 9.8 * 100)
        finally:
            con.close()
        self.assertEqual(catalog.get_watermark(self.root, "month"), "20260831")

    def test_aggregate_monthly_idempotent(self):
        for d in ("20260817", "20260818"):
            sink.write_daily(self.root, d, [{
                "code": "600000", "name": "样本", "is_st": False,
                "open": 10.0, "high": 11.0, "low": 9.5, "close": 10.5,
                "prev_close": 9.8, "volume": 1000.0, "amount": 10000.0,
            }])
        first = sink.aggregate_monthly(self.root, "20260831")
        again = sink.aggregate_monthly(self.root, "20260831")
        self.assertEqual(first["status"], "written")
        self.assertEqual(again["status"], "skipped")
        self.assertEqual(catalog.get_watermark(self.root, "month"), "20260831")


class WarehouseEngineTest(unittest.TestCase):
    """W3 验收：视图 / 宏数值正确性 / 三护栏 / 超时 / 状态清单。

    指标正确性基准：与教科书口径手工对账（异源签字 = W5 发版前接 pybao 通道复核）。
    """

    CLOSES = [10, 11, 12, 11, 10, 11, 12, 13, 14, 13]  # 10 日收盘

    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.root = pathlib.Path(self._tmp.name)
        # 每日一行 × 10 个交易日（生产语义：一个分区 = 一日全市场行）
        # 0.10.10 物化语义：因子 2.0 经 factor_map 在沉淀时一次计算物化进分区
        for i, c in enumerate(self.CLOSES):
            sink.write_daily(self.root, f"202608{11 + i:02d}", [{
                "code": "600000", "name": "样本", "is_st": False,
                "open": c - 0.1, "high": c + 0.5, "low": c - 0.5, "close": float(c),
                "prev_close": float(self.CLOSES[i - 1]) if i else c - 0.2,
                "volume": 1000.0 + i, "amount": 10000.0 + i,
            }], factor_map={"600000": 2.0})
        sink.write_codes(self.root, [{"code": "600000", "name": "样本"}])
        self.engine = WarehouseEngine(self.root)

    def tearDown(self):
        self.engine.close()
        self._tmp.cleanup()

    def test_v_daily_and_v_daily_fq(self):
        r = self.engine.run_sql("SELECT count(*), min(close), max(close) FROM v_daily")
        self.assertEqual(r["rows"][0][0], 10)
        self.assertAlmostEqual(r["rows"][0][1], 10.0)
        # 0.10.10 物化语义：close_fq 是沉淀时算好落盘的列，查询零计算
        r2 = self.engine.run_sql(
            "SELECT close, close_fq, adj_factor FROM v_daily_fq WHERE date = '2026-08-15'")
        close = r2["rows"][0][0]
        self.assertAlmostEqual(r2["rows"][0][1], close * 2.0)
        self.assertAlmostEqual(r2["rows"][0][2], 2.0)

    def test_daily_fq_null_when_factor_absent(self):
        """物化缺失语义：无 factor_map 的行复权列 NULL（原价），查询仍可用。"""
        with tempfile.TemporaryDirectory() as tmp:
            root = pathlib.Path(tmp)
            sink.write_daily(root, "20260822", [{
                "code": "600000", "name": "样本", "is_st": False,
                "open": 10.0, "high": 10.5, "low": 9.9, "close": 10.2,
                "prev_close": 10.0, "volume": 1.0, "amount": 1.0,
            }])
            eng = WarehouseEngine(root)
            try:
                r = eng.run_sql(
                    "SELECT close, close_fq, adj_factor FROM v_daily_fq "
                    "WHERE date = '2026-08-22'")
                self.assertEqual(r["rows"][0][1], None)  # 原价列空
                self.assertEqual(r["rows"][0][2], None)
            finally:
                eng.close()

    def test_ta_ma_window_semantics(self):
        """MA5 第 5 日起有值，且等于近 5 收盘均值（窗口 PARTITION/ORDER 正确性）。"""
        r = self.engine.run_sql(
            "SELECT date, ma FROM ta_ma(5) WHERE code = '600000' ORDER BY date")
        mas = [row[1] for row in r["rows"]]
        self.assertIsNone(mas[3])  # 前 4 日窗口不满
        self.assertAlmostEqual(mas[4], sum(self.CLOSES[:5]) / 5)
        self.assertAlmostEqual(mas[9], sum(self.CLOSES[5:]) / 5)

    def test_ta_rsi_wilder_first_diff_seed(self):
        """RSI(6) Wilder 首差种子（0.10.5 对齐 pybao 实证口径）：
        价差 +1,+1,-1,-1,+1,+1,+1,+1,-1（rn2~10），ag/al 从首差递推。
        前两根纯涨 → 100；末根(rn10,-1) ag=198905/279936 al=81031/279936 → 71.054。"""
        r = self.engine.run_sql(
            "SELECT rsi FROM ta_rsi(6) WHERE code = '600000' ORDER BY date")
        values = [v for v, in r["rows"] if v is not None]
        self.assertEqual(len(values), 9)  # rn2~10 全部有值
        self.assertEqual(values[0], 100.0)
        self.assertEqual(values[1], 100.0)
        self.assertAlmostEqual(values[-1], 71.054, places=3)
        self.assertTrue(all(0 <= v <= 100 for v in values))

    def test_ta_macd_shape_and_ema_seed(self):
        r = self.engine.run_sql(
            "SELECT macd, signal, hist FROM ta_macd(3, 6, 3) "
            "WHERE code = '600000' ORDER BY date")
        rows = r["rows"]
        self.assertEqual(len(rows), 10)
        # EMA 以首日收盘为种子 → 首日 macd = 0
        self.assertAlmostEqual(rows[0][0], 0.0, places=9)
        for macd, signal, hist in rows:
            self.assertAlmostEqual(hist, macd - signal, places=9)

    def test_run_sql_write_allowed_in_research_schema(self):
        """读写全开（用户拍板）：research schema 预建，直接建表/写入/查询全链路。"""
        self.engine.run_sql(
            "CREATE OR REPLACE TABLE research.notes AS SELECT 1 AS k")  # 无需先 CREATE SCHEMA
        self.engine.run_sql("INSERT INTO research.notes VALUES (2)")
        r = self.engine.run_sql("SELECT * FROM research.notes ORDER BY k")
        self.assertEqual(r["rows"], [[1], [2]])

    def test_guardrail_single_statement(self):
        with self.assertRaises(GuardrailError):
            self.engine.run_sql("SELECT 1; SELECT 2")
        with self.assertRaises(GuardrailError):
            self.engine.run_sql("")

    def test_guardrail_facts_write_protected(self):
        """facts 只读护栏：语句含 facts/ 路径即拒（COPY TO 逃逸通道封死）。"""
        with self.assertRaises(GuardrailError):
            self.engine.run_sql(
                f"COPY (SELECT 1) TO '{layout.facts_dir(self.root)}/evil.parquet' (FORMAT PARQUET)")
        with self.assertRaises(GuardrailError):
            self.engine.run_sql(
                f"CREATE TABLE t AS SELECT * FROM read_parquet("
                f"'{layout.facts_dir(self.root)}/daily/*/*/date=*.parquet')")
        # 视图读不受影响
        self.assertEqual(self.engine.run_sql("SELECT count(*) FROM v_daily")["rows"][0][0], 10)

    def test_row_cap_truncation(self):
        with mock.patch.object(config, "WAREHOUSE_ROW_CAP", 3):
            r = self.engine.run_sql("SELECT * FROM v_daily")
        self.assertEqual(r["row_count"], 3)
        self.assertTrue(r["truncated"])

    def test_query_timeout(self):
        with mock.patch.object(config, "WAREHOUSE_QUERY_TIMEOUT", 1):
            with self.assertRaises(TimeoutError):
                self.engine.run_sql(
                    "SELECT count(*) FROM range(100000) a, range(100000) b, range(1000) c")

    def test_syntax_error_maps_to_duckdb_exception(self):
        import duckdb
        with self.assertRaises(duckdb.Error):
            self.engine.run_sql("SELETC 1")

    def test_status_and_list_objects(self):
        s = self.engine.status()
        self.assertEqual(s["watermark_daily"], "20260820")
        self.assertEqual(s["sedimented_dates"], 10)
        self.assertEqual(s["codes"], 1)
        objs = self.engine.list_objects()
        self.assertIn("v_daily", objs["tables"])
        macro_names = {m["name"] for m in objs["macros"]}
        self.assertEqual(macro_names, {"ta_ma", "ta_rsi", "ta_macd"})

    def test_engine_empty_warehouse_views_typed(self):
        """空仓期视图可用（类型正确的空结果），不因无分区报 IO 错。"""
        with tempfile.TemporaryDirectory() as tmp:
            eng = WarehouseEngine(pathlib.Path(tmp))
            try:
                r = eng.run_sql("SELECT count(*) FROM v_daily")
                self.assertEqual(r["rows"][0][0], 0)
            finally:
                eng.close()

    def test_week_month_views_and_current(self):
        """0.10.11/0.10.12：v_week/v_month 读物化分区；v_week_current/v_month_current
        从 v_daily 实时聚合当前未走完周期（历史固定落盘、当前滚动）。"""
        with tempfile.TemporaryDirectory() as tmp:
            root = pathlib.Path(tmp)
            # 单周单月数据（0817~0821 周 + 8 月）
            for i, c in enumerate([10, 11, 12, 11, 10]):
                sink.write_daily(root, f"202608{17 + i:02d}", [{
                    "code": "600000", "name": "样本", "is_st": False,
                    "open": float(c), "high": float(c) + 1.0, "low": float(c) - 1.0,
                    "close": float(c), "prev_close": float(c) - 0.1,
                    "volume": 1000.0, "amount": 10000.0,
                }], factor_map={"600000": 2.0})
            sink.aggregate_weekly(root, "20260821")
            sink.aggregate_monthly(root, "20260831")
            eng = WarehouseEngine(root)
            try:
                r = eng.run_sql(
                    "SELECT code, open, close, close_fq FROM v_week WHERE code='600000'")
                self.assertEqual(r["rows"][0][1], 10.0)   # 周 open
                self.assertEqual(r["rows"][0][2], 10.0)   # 周 close（周五 0821）
                self.assertAlmostEqual(r["rows"][0][3], 20.0)  # close_fq 物化
                r2 = eng.run_sql(
                    "SELECT code, open, close FROM v_month WHERE code='600000'")
                self.assertEqual(r2["rows"][0][1], 10.0)
                self.assertEqual(r2["rows"][0][2], 10.0)
                # current 视图可查（从 v_daily 实时聚合；真实 current_date 与测试数据
                # 不在同一周期时返回空，不报错——结构正确性断言）
                r3 = eng.run_sql("SELECT count(*) FROM v_week_current")
                self.assertIsInstance(r3["rows"][0][0], int)
                r4 = eng.run_sql("SELECT count(*) FROM v_month_current")
                self.assertIsInstance(r4["rows"][0][0], int)
            finally:
                eng.close()


class WarehouseBackupTest(unittest.TestCase):
    """0.10.8：warehouse.duckdb 在线备份（COPY FROM DATABASE）——独立可读/一致性/保留策略/日级守卫。"""

    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.root = pathlib.Path(self._tmp.name)
        sink.write_daily(self.root, "20260822", _sample_rows()[:2])
        sink.write_codes(self.root, [{"code": "600000", "name": "浦发银行"}])
        # research 用户表（备份必须带走）
        con = duckdb.connect(str(layout.duckdb_path(self.root)))
        try:
            con.execute("CREATE SCHEMA research")
            con.execute("CREATE TABLE research.spot (code TEXT, close DOUBLE)")
            con.execute("INSERT INTO research.spot VALUES ('600000', 9.08)")
        finally:
            con.close()

    def tearDown(self):
        self._tmp.cleanup()

    def test_backup_file_independent_and_consistent(self):
        """备份文件独立可打开；meta/codes/research 数据读回一致（沿备份独立可读断言模式）。"""
        target = backup.backup_duckdb(self.root, force=True)
        self.assertIsNotNone(target)
        self.assertTrue(target.exists())
        self.assertTrue(target.name.startswith("warehouse-"))
        self.assertEqual(target.parent, layout.backups_dir(self.root))
        # 独立连接直读备份
        con = duckdb.connect(str(target), read_only=True)
        try:
            self.assertEqual(con.execute("SELECT * FROM meta").fetchall(),
                             [("watermark:daily", "20260822")])
            self.assertEqual(con.execute("SELECT code, name FROM codes").fetchall(),
                             [("600000", "浦发银行")])
            self.assertEqual(con.execute("SELECT * FROM research.spot").fetchall(),
                             [("600000", 9.08)])
        finally:
            con.close()

    def test_backup_daily_guard_skips_second_call(self):
        """日级守卫：同日第二次（force=False）跳过；force=True 绕过。"""
        first = backup.backup_duckdb(self.root)
        second = backup.backup_duckdb(self.root)
        self.assertIsNotNone(first)
        self.assertIsNone(second)  # 同日守卫
        third = backup.backup_duckdb(self.root, force=True)
        self.assertIsNotNone(third)  # force 绕过
        self.assertEqual(len(list(layout.backups_dir(self.root).glob("warehouse-*.db"))), 2)

    def test_backup_retention_keeps_newest(self):
        """保留最近 BACKUP_KEEP 份：模拟多日备份后旧文件被清理。"""
        import storage.warehouse.backup as bk
        old_keep = bk.BACKUP_KEEP
        bk.BACKUP_KEEP = 2
        try:
            backup.backup_duckdb(self.root, force=True)
            backup.backup_duckdb(self.root, force=True)
            backup.backup_duckdb(self.root, force=True)
            files = sorted(layout.backups_dir(self.root).glob("warehouse-*.db"))
            self.assertEqual(len(files), 2)  # 保留 2 份
        finally:
            bk.BACKUP_KEEP = old_keep

    def test_backup_missing_source_returns_none(self):
        """源库不存在 → None（静默，无异常）。"""
        empty = pathlib.Path(self._tmp.name) / "empty-root"
        self.assertIsNone(backup.backup_duckdb(empty, force=True))

    def test_backup_with_engine_open(self):
        """0.10.8 实测缺陷回归：engine 常驻连接已持有源库时备份仍成功
        （duckdb.connect(路径) 复用缓存实例；显式 ATTACH 同一路径会
        Unique file handle conflict——备份曾因此静默失败）。"""
        eng = WarehouseEngine(self.root)  # 打开常驻连接（生产真实场景）
        try:
            target = backup.backup_duckdb(self.root, force=True)
        finally:
            eng.close()
        self.assertIsNotNone(target)
        self.assertTrue(target.exists())
        con = duckdb.connect(str(target), read_only=True)
        try:
            self.assertEqual(con.execute("SELECT count(*) FROM research.spot").fetchone()[0], 1)
        finally:
            con.close()


class WarehouseQueriesFacadeTest(unittest.TestCase):
    """W3：queries 门面 availability 降级与 known_at。"""

    def test_unavailable_when_disabled(self):
        with mock.patch.object(config, "WAREHOUSE_ENABLED", False):
            with self.assertRaises(WarehouseUnavailable):
                from storage.warehouse import queries
                queries.status()

    def test_known_at_reads_watermark(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = pathlib.Path(tmp)
            sink.write_daily(root, "20260822", _sample_rows()[:1])
            from storage.warehouse import queries
            with mock.patch.object(layout, "root_dir", return_value=root):
                self.assertEqual(queries.known_at(), "20260822")


def _traded_points() -> list[dict]:
    """快照 TRADED 行样本（两市场 + 一只停牌行，编排测试用）。"""
    return [
        {"code": "600000", "name": "浦发银行", "status": "TRADED",
         "open": 10.0, "high": 10.5, "low": 9.9, "close": 10.2, "prev_close": 10.0,
         "volume": 100.0, "amount": 1020.0, "is_st": False},
        {"code": "000001", "name": "平安银行", "status": "TRADED",
         "open": 11.0, "high": 11.2, "low": 10.8, "close": 11.1, "prev_close": 11.0,
         "volume": 200.0, "amount": 2220.0, "is_st": False},
        {"code": "300750", "name": "宁德时代", "status": "SUSPENDED"},  # 非 TRADED：不沉淀
    ]


class WarehouseReconcileTest(unittest.TestCase):
    """W4 验收：对账三板斧（行数/同源字段/异源）的检出能力。"""

    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.root = pathlib.Path(self._tmp.name)
        from storage.warehouse import reconcile
        self.reconcile = reconcile

    def tearDown(self):
        self._tmp.cleanup()

    @staticmethod
    def _to_engine_fields(points):
        """快照形态 → 引擎原生字段（与生产 _snapshot_points 适配一致，0.10.7）。"""
        return [{**p, "prev_close": p.get("prev_close", p.get("prev_close"))} for p in points]

    def test_reconcile_all_green(self):
        pts = _traded_points()[:2]
        w = sink.write_daily(self.root, "20260822", self._to_engine_fields(pts))
        rec = self.reconcile.reconcile_daily(
            self.root, "20260822", pts,
            sedimented_rows=w["rows"], dropped_nonfinite=w["dropped_nonfinite"])
        self.assertTrue(rec["ok"], rec["issues"])
        self.assertEqual(rec["checked"], 2)

    def test_reconcile_detects_row_count_gap(self):
        pts = _traded_points()[:2]
        sink.write_daily(self.root, "20260822", self._to_engine_fields(pts))
        rec = self.reconcile.reconcile_daily(
            self.root, "20260822", pts + [_traded_points()[0]],  # 多报一行
            sedimented_rows=2, dropped_nonfinite=0)
        self.assertFalse(rec["ok"])
        self.assertEqual(rec["issues"][0]["kind"], "row_count")

    def test_reconcile_detects_field_mismatch(self):
        pts = _traded_points()[:1]
        sink.write_daily(self.root, "20260822", self._to_engine_fields(pts))
        tampered = [{**pts[0], "close": 999.0}]
        rec = self.reconcile.reconcile_daily(
            self.root, "20260822", tampered,
            sedimented_rows=1, dropped_nonfinite=0)
        self.assertFalse(rec["ok"])
        kinds = {i["kind"] for i in rec["issues"]}
        self.assertIn("field_mismatch", kinds)

    def test_reconcile_external_cross_check(self):
        pts = _traded_points()[:2]
        sink.write_daily(self.root, "20260822", self._to_engine_fields(pts))
        # 异源行：600000 开盘一致；000001 开盘差 10%（超 0.5% 容限 → 检出）
        external = [{"code": "600000", "open_price": 10.0, "prev_close": 10.0},
                    {"code": "000001", "open_price": 12.21, "prev_close": 11.0}]
        rec = self.reconcile.reconcile_daily(
            self.root, "20260822", pts,
            sedimented_rows=2, dropped_nonfinite=0, external_rows=external)
        self.assertFalse(rec["ok"])
        kinds = {i["kind"] for i in rec["issues"]}
        self.assertIn("external_mismatch", kinds)


class WarehouseTasksTest(unittest.TestCase):
    """W4：沉淀编排（注入点 mock）——就绪门/幂等/对账记录/降级/单飞。"""

    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.root = pathlib.Path(self._tmp.name)
        import services.warehouse_tasks as wt
        self.wt = wt
        from storage import warehouse as wh_pkg
        from storage.warehouse import layout as wh_layout
        from storage.warehouse import reconcile as wh_reconcile
        from storage.warehouse import sink as wh_sink
        self._saved = {k: getattr(wt, k) for k in
                       ("query_snapshot", "data_latest", "is_trading_day", "sink",
                        "reconcile_daily", "warehouse_root", "availability",
                        "refresh_views", "backup_duckdb", "adjust_provider")}
        wt.query_snapshot = lambda q: {"points": _traded_points()}
        wt.data_latest = lambda force=False: "20260822"
        wt.is_trading_day = lambda d: True
        wt.sink = wh_sink
        wt.reconcile_daily = wh_reconcile.reconcile_daily
        wt.warehouse_root = lambda: self.root
        wt.availability = lambda: (True, "ok")
        wt.refresh_views = lambda: None
        wt.backup_duckdb = lambda root, force=False: None  # 0.10.8：隔离备份副作用
        wt.adjust_provider = None
        # 日检/告警/日志落 tmp（防写到默认 /data）
        self._cm = mock.patch.multiple(config, DATA_DIR=self.root)
        self._cm.start()

    def tearDown(self):
        self._cm.stop()
        for k, v in self._saved.items():
            setattr(self.wt, k, v)

    def test_sediment_run_writes_reconciles_and_records(self):
        res = self.wt.warehouse_run(days=1)
        self.assertTrue(res["ok"], res)
        day = res["days"][0]
        self.assertEqual(day["date"], "20260822")
        self.assertTrue(day["reconcile"]["ok"], day["reconcile"])
        self.assertEqual(day["write"]["rows"], 2)  # SUSPENDED 不沉淀
        self.assertTrue(layout.daily_partition(self.root, "20260822", "sh").exists())
        self.assertTrue(layout.daily_partition(self.root, "20260822", "sz").exists())
        self.assertEqual(self.wt.sink.catalog.get_watermark(self.root, "daily"), "20260822")
        # codes 表刷新（只含 TRADED 行）
        import duckdb
        con = duckdb.connect(str(layout.duckdb_path(self.root)))
        try:
            self.assertEqual(con.execute("SELECT count(*) FROM codes").fetchone()[0], 2)
        finally:
            con.close()

    def test_sediment_idempotent_rerun(self):
        first = self.wt.warehouse_run(days=1)
        second = self.wt.warehouse_run(days=1)
        self.assertTrue(first["ok"] and second["ok"])
        self.assertEqual(len(second["days"]), 0)  # watermark 已达 → 无目标日，幂等

    def test_readiness_gate_blocks_when_data_stale(self):
        original = self.wt.data_latest
        self.wt.data_latest = lambda force=False: "20260820"  # 落后于任何今天
        try:
            res = self.wt.warehouse_run(days=1, require_today=True)
        finally:
            self.wt.data_latest = original
        self.assertFalse(res["ok"])
        self.assertTrue(str(res["reason"]).startswith("未就绪："))

    def test_unavailable_degrades_without_exceptions(self):
        self.wt.availability = lambda: (False, "duckdb unavailable: test")
        res = self.wt.warehouse_run(days=1)
        self.assertFalse(res["ok"])
        self.assertIn("不可用", res["reason"])

    def test_async_single_flight(self):
        self.wt._wh_run_state.update(running=True, started="x")
        try:
            res = self.wt.warehouse_run_async(days=1)
            self.assertFalse(res["ok"])
            self.assertEqual(res["reason"], "沉淀任务已在运行中")
        finally:
            self.wt._wh_run_state.update(running=False, started=None)

    def test_backfill_mode_fills_history(self):
        """0.10.3：backfill=True 向 watermark 之前回看（跳非交易日；幂等；watermark 不回退）。"""
        from datetime import date as _date
        # 预置已沉淀日 0822（setUp 的 data_latest）
        self.wt.warehouse_run(days=1)
        # is_trading_day：周末（0822 六/0823 日）非交易日
        def _trading(d):
            return d.weekday() < 5
        self.wt.is_trading_day = _trading
        res = self.wt.warehouse_run(days=3, backfill=True)
        self.assertTrue(res["ok"], res)
        dates = [d["date"] for d in res["days"]]
        # 0822(六) 往前 3 个交易日 = 0821(五)、0820(四)、0819(三)
        self.assertEqual(dates, ["20260819", "20260820", "20260821"])
        # 继续回填 = 从新的最早日（0819）向下扩展（目标恒低于最早日——构造上免重，
        # skip-existing 不可达属预期）；watermark 不因回填回退
        again = self.wt.warehouse_run(days=2, backfill=True)
        self.assertTrue(again["ok"])
        self.assertEqual([d["date"] for d in again["days"]], ["20260817", "20260818"])
        self.assertEqual(self.wt.sink.catalog.get_watermark(self.root, "daily"), "20260822")

    def test_status_shape(self):
        self.wt.warehouse_run(days=1)
        s = self.wt.warehouse_status()
        self.assertTrue(s["available"])
        self.assertEqual(s["watermark_daily"], "20260822")
        self.assertFalse(s["running"])

    def test_sediment_triggers_weekly_aggregation(self):
        """0.10.10：沉淀完整周后自动聚合周K（facts/week + watermark:week 推进）。"""
        # data_latest=20260822(六)；is_trading_day 周末排除 → 目标日 0822 前向到 0821(五)
        # 先用交易日判定覆盖完整周 0817~0821
        def _trading(d):
            return d.weekday() < 5
        self.wt.is_trading_day = _trading
        self.wt.data_latest = lambda force=False: "20260821"  # 周五，本周完整
        res = self.wt.warehouse_run(days=5)  # 0821 往前补 5 天（含周末跳过）
        self.assertTrue(res["ok"], res)
        dates = [d["date"] for d in res["days"]]
        self.assertEqual(dates, ["20260817", "20260818", "20260819", "20260820", "20260821"])
        # 周K 分区已生成（周结束日=周五 20260821）
        w = layout.week_partition(self.root, "20260821", "sh")
        self.assertTrue(w.exists(), f"{w} 不存在")
        self.assertEqual(self.wt.sink.catalog.get_watermark(self.root, "week"), "20260821")

    def test_incomplete_week_not_aggregated(self):
        """0.10.10：周内缺口（只有周一~周三）不聚合——等周完整后一次聚合。"""
        def _trading(d):
            return d.weekday() < 5
        self.wt.is_trading_day = _trading
        self.wt.data_latest = lambda force=False: "20260819"  # 周三，本周不完整
        res = self.wt.warehouse_run(days=3)
        self.assertTrue(res["ok"], res)
        self.assertEqual([d["date"] for d in res["days"]],
                         ["20260817", "20260818", "20260819"])
        self.assertFalse(layout.week_partition(self.root, "20260821", "sh").exists())
        self.assertIsNone(self.wt.sink.catalog.get_watermark(self.root, "week"))

    def test_sediment_triggers_monthly_aggregation(self):
        """0.10.12：月完整 → 自动月K聚合（facts/month + watermark:month 推进）。
        直接铺满整月 daily 后调 _aggregate_months（编排层判定逻辑单测）。"""
        def _trading(d):
            return d.weekday() < 5
        self.wt.is_trading_day = _trading
        # 铺满 8 月全部交易日（0803~0831 的周一~周五）
        from datetime import date as _date, timedelta as _td
        cursor = _date(2026, 8, 3)
        while cursor <= _date(2026, 8, 31):
            if cursor.weekday() < 5:
                sink.write_daily(self.root, cursor.strftime("%Y%m%d"), [{
                    "code": "600000", "name": "样本", "is_st": False,
                    "open": 10.0, "high": 11.0, "low": 9.5, "close": 10.5,
                    "prev_close": 9.8, "volume": 1000.0, "amount": 10000.0,
                }])
            cursor += _td(days=1)
        self.wt._aggregate_months(self.root, [{"date": "20260831"}])
        m = layout.month_partition(self.root, "20260831", "sh")
        self.assertTrue(m.exists(), f"{m} 不存在")
        self.assertEqual(self.wt.sink.catalog.get_watermark(self.root, "month"), "20260831")

    def test_incomplete_month_not_aggregated(self):
        """0.10.12：月内缺口（只沉淀到月中）不聚合——等月完整后一次聚合。"""
        def _trading(d):
            return d.weekday() < 5
        self.wt.is_trading_day = _trading
        # 只铺 8 月上旬（0803~0814），月中起缺失 → 月不完整
        from datetime import date as _date, timedelta as _td
        cursor = _date(2026, 8, 3)
        while cursor <= _date(2026, 8, 14):
            if cursor.weekday() < 5:
                sink.write_daily(self.root, cursor.strftime("%Y%m%d"), [{
                    "code": "600000", "name": "样本", "is_st": False,
                    "open": 10.0, "high": 11.0, "low": 9.5, "close": 10.5,
                    "prev_close": 9.8, "volume": 1000.0, "amount": 10000.0,
                }])
            cursor += _td(days=1)
        self.wt._aggregate_months(self.root, [{"date": "20260814"}])
        self.assertFalse(layout.month_partition(self.root, "20260831", "sh").exists())
        self.assertIsNone(self.wt.sink.catalog.get_watermark(self.root, "month"))

    def test_sediment_triggers_backup(self):
        """0.10.8：沉淀成功（有 results）后调用备份注入点；无沉淀时不调用。"""
        calls = []
        self.wt.backup_duckdb = lambda root, force=False: calls.append(root) or None
        res = self.wt.warehouse_run(days=1)
        self.assertTrue(res["ok"])
        self.assertEqual(len(calls), 1)  # 有目标日 → 备份一次
        self.wt.warehouse_run(days=1)  # 幂等：无新目标日
        self.assertEqual(len(calls), 1)  # 不重复备份
        # 备份异常不影响沉淀结论
        self.wt.backup_duckdb = lambda root, force=False: (_ for _ in ()).throw(RuntimeError("boom"))
        res2 = self.wt.warehouse_run(days=1)
        self.assertTrue(res2["ok"])


class EngineGateConvergenceTest(unittest.TestCase):
    """W4 C1：MCP _http_get 收敛为闸口薄委托（行为保持：解析/默认值不变）。"""

    def test_http_get_delegates_to_gate(self):
        from storage.providers import free_stockdb
        from interfaces.mcp import stockdb_mcp_server as srv
        captured = {}

        def fake_fetch(path, timeout=0.0, breaker=False, base=None, block=False):
            captured.update(path=path, base=base, block=block)
            return '{"ok": 1}'

        with mock.patch.object(free_stockdb, "fetch", side_effect=fake_fetch):
            out = srv._http_get("get", "日k:600000:20260822")
        self.assertEqual(out, {"ok": 1})
        self.assertEqual(captured["path"], "/?cmd=get&t=%E6%97%A5k%3A600000%3A20260822")
        self.assertIn(":", captured["base"])  # 独立模式默认 host:port 透传
        self.assertTrue(captured["block"])  # 查询路径排队语义


class McpWarehouseToolsTest(unittest.TestCase):
    """W5 验收：warehouse 组 3 工具——信封 8 键/错误码/known_at=watermark/分组/降级。"""

    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.root = pathlib.Path(self._tmp.name)
        sink.write_daily(self.root, "20260822", _sample_rows()[:2])
        self._cm = mock.patch.multiple(config, WAREHOUSE_DIR=self.root)
        self._cm.start()
        from storage.warehouse import engine as wh_engine
        wh_engine.reset_engine()
        from interfaces.mcp import stockdb_mcp_server as srv
        self.srv = srv

    def tearDown(self):
        from storage.warehouse import engine as wh_engine
        wh_engine.reset_engine()
        self._cm.stop()
        self._tmp.cleanup()

    def _call(self, name, args=None):
        result = self.srv._call_tool(name, args or {})
        return json.loads(result["content"][0]["text"])

    def test_run_sql_contract_envelope(self):
        out = self._call("warehouse_run_sql",
                         {"sql": "SELECT count(*) AS n FROM v_daily"})
        for key in ("source", "source_contract_version", "known_at",
                    "is_partial", "truncated", "total", "errors", "known_limitations"):
            self.assertIn(key, out)
        self.assertEqual(out["source"], "warehouse")
        self.assertEqual(out["source_contract_version"], "warehouse-sql-v1")
        self.assertEqual(out["known_at"], "20260822")  # watermark
        self.assertEqual(out["rows"], [[2]])

    def test_run_sql_write_via_mcp(self):
        self._call("warehouse_run_sql",
                   {"sql": "CREATE OR REPLACE TABLE t1_mcp AS SELECT 1 AS v"})
        out = self._call("warehouse_run_sql", {"sql": "SELECT * FROM t1_mcp"})
        self.assertEqual(out["rows"], [[1]])

    def test_run_sql_error_codes(self):
        # 空参数 → INVALID_ARGUMENT
        err0 = self.srv._call_tool("warehouse_run_sql", {})
        self.assertIn("INVALID_ARGUMENT", err0["content"][0]["text"])
        # 护栏（facts 只读）→ INVALID_ARGUMENT
        err = self.srv._call_tool(
            "warehouse_run_sql", {"sql": f"COPY (SELECT 1) TO "
                                         f"'{layout.facts_dir(self.root)}/x.parquet'"})
        self.assertIn("INVALID_ARGUMENT", err["content"][0]["text"])
        # 语法错误 → INVALID_ARGUMENT
        err2 = self.srv._call_tool("warehouse_run_sql", {"sql": "SELETC 1"})
        self.assertIn("INVALID_ARGUMENT", err2["content"][0]["text"])

    def test_run_sql_unavailable_degrades(self):
        with mock.patch.object(config, "WAREHOUSE_ENABLED", False):
            err = self.srv._call_tool("warehouse_run_sql", {"sql": "SELECT 1"})
        self.assertIn("DEPENDENCY_UNAVAILABLE", err["content"][0]["text"])

    def test_list_tables_and_status_tools(self):
        out = self._call("warehouse_list_tables")
        self.assertIn("v_daily", out["tables"])
        self.assertEqual({m["name"] for m in out["macros"]},
                         {"ta_ma", "ta_rsi", "ta_macd"})
        st = self._call("warehouse_status")
        self.assertEqual(st["watermark_daily"], "20260822")
        self.assertEqual(st["sedimented_dates"], 1)

    def test_run_sql_date_column_json_safe(self):
        """0.10.4：DATE 列经 MCP 边界返回 ISO 字符串（此前 json.dumps 必崩）。"""
        out = self._call("warehouse_run_sql",
                         {"sql": "SELECT date, close FROM v_daily LIMIT 3"})
        self.assertIsInstance(out["rows"][0][0], str)
        self.assertRegex(out["rows"][0][0], r"^[0-9]{4}-[0-9]{2}-[0-9]{2}$")

    def test_warehouse_group_registration(self):
        names = {t["name"] for t in self.srv.TOOLS if t.get("group") == "warehouse"}
        self.assertEqual(names, {"warehouse_run_sql", "warehouse_list_tables",
                                 "warehouse_status", "warehouse_run"})
        self.assertIn("warehouse", self.srv.TOOL_GROUPS)
        # 分组过滤：group=warehouse 只见 4 工具（0.10.15 增 warehouse_run 触发口）
        listed = self.srv._tools_for_group("warehouse")
        self.assertEqual({t["name"] for t in listed}, names)


if __name__ == "__main__":
    unittest.main(verbosity=2)
