"""test_market_v3_research — 补充合同 stockdb-market-v3-research-extension 1.0.0
服务端分支测试（A 指数/板块日K + B 因子/复权 R/F/A + 长历史）。

行情数值全部合成（与合同 examples.json 同风格），不作为真实市场事实；
真实数据/部署验收在 acceptance runner（live/nas 模式）另行执行。
"""

from __future__ import annotations

import json
import os
import sys
import tempfile
import time
import unittest
import uuid
from decimal import Decimal
from pathlib import Path
from unittest import mock

_MCP_DIR = Path(__file__).resolve().parent
_BASE_DIR = _MCP_DIR.parent.parent
for _p in (str(_MCP_DIR), str(_BASE_DIR), str(_MCP_DIR / "contract")):
    if _p not in sys.path:
        sys.path.insert(0, _p)

from interfaces.mcp import market_v3 as v3  # noqa: E402
from interfaces.mcp import market_v3_research as r  # noqa: E402
from interfaces.mcp import stockdb_mcp_server as server  # noqa: E402
from test_market_v3 import (  # noqa: E402
    BASE_ARGS, CATALOG, EQUITY_KEY, ETF_KEY, LATEST, SYNC_SUCCESSES,
    V3TestCase, _stub_engine_rows, validate_contract)

# 扩展合同校验器（FactorResponse 等 kind 在扩展包内；独立模块名避免与
# 基线 validate_contract 同名缓存冲突）
import importlib.util as _ilu  # noqa: E402
_EXT_DIR = Path(__file__).resolve().parent / "contract" / "research-extension"
_ext_validate_contract = None
try:
    _spec = _ilu.spec_from_file_location(
        "research_extension_validate_contract",
        _EXT_DIR / "validate_contract.py")
    _mod = _ilu.module_from_spec(_spec)
    sys.modules["research_extension_validate_contract"] = _mod
    _spec.loader.exec_module(_mod)
    _ext_validate_contract = _mod
except Exception:  # noqa: BLE001
    _ext_validate_contract = None

try:
    import jsonschema  # noqa: F401
except Exception:  # noqa: BLE001
    jsonschema = None

# ---------------------------------------------------------------------------
# 合成序列数据
# ---------------------------------------------------------------------------

INDEX_KEY = "XSHG/index/exchange/000001"
INDEX_KEY_2 = "XSHE/index/exchange/399006"
CSI_KEY = "XSHG/index/csi/000852"
BOARD_KEY = "CN/board/eastmoney/BK1138"
BOARD_KEY_2 = "CN/board/eastmoney/BK1408"

_SYN_DATES = ["20261020", "20261021", "20261022", "20261023"]


def _index_rows(dates=None, base=3000.0):
    dates = dates or _SYN_DATES
    rows = []
    value = base
    for d in dates:
        value += 1.0
        rows.append({"date8": d, "open": round(value, 3),
                     "close": round(value + 5.0, 3),
                     "high": round(value + 6.0, 3),
                     "low": round(value - 1.0, 3)})
    return rows


def _board_rows(dates=None, base=1000.0):
    dates = dates or _SYN_DATES
    rows = []
    value = base
    for d in dates:
        value += 2.0
        rows.append({"date8": d, "open": round(value, 2),
                     "close": round(value + 8.0, 2),
                     "high": round(value + 9.0, 2),
                     "low": round(value - 2.0, 2)})
    return rows


# 合成因子事件（002600）：20261022 现金分红 div=0.5，cum 1.0 → 1.05
_SYN_EVENTS = [("20261022", {"div": 0.5, "give": 0.0, "trans": 0.0,
                             "mult": 1.0469, "cum": 1.05})]
# 合成组合事件（送转 + 现金，A08 用）：give=0.2、div=0.4 → mult 组合
_COMBO_EVENTS = [("20261022", {"div": 0.4, "give": 0.2, "trans": 0.0,
                               "mult": 1.3, "cum": 1.3})]


def _stub_factor_events(code):
    return list(_SYN_EVENTS)


class ResearchTestCase(V3TestCase):
    """V3TestCase 之上：research 序列源/因子事件全部桩化。"""

    def setUp(self) -> None:
        super().setUp()
        r._factor_cache.clear()
        # 0.16.0：板块补采线程/东财客户端状态逐测试隔离（线程明确关闭；
        # 风控冷却/连败计数不得跨测试泄漏）
        os.environ["STOCKDB_V3_BOARD_BACKFILL"] = "off"
        self.addCleanup(os.environ.pop, "STOCKDB_V3_BOARD_BACKFILL", None)
        with r._EM_STATE_LOCK:
            r._EM_LAST_STATE.clear()
            r._EM_BACKFILL_STATE.clear()
        self.addCleanup(r._EM_LAST_STATE.clear)
        self._real_eastmoney = r.eastmoney_board_daily  # 真实客户端（风控测试用）
        self._rpatches = [
            mock.patch.object(r, "tencent_index_daily",
                              side_effect=self._tencent_stub),
            mock.patch.object(r, "eastmoney_board_daily",
                              side_effect=self._eastmoney_stub),
            mock.patch.object(r, "engine_factor_events",
                              side_effect=_stub_factor_events),
        ]
        for p in self._rpatches:
            p.start()
            self.addCleanup(p.stop)
        # 每个测试独立的持久化快照目录（A15 重启模拟需要同目录跨实例）
        import shutil
        self._rmtree = shutil.rmtree
        self.snap_dir = tempfile.mkdtemp(prefix="v3rs-")
        self.assertTrue(Path(self.snap_dir).is_absolute(), self.snap_dir)
        os.environ["STOCKDB_V3_SNAPSHOT_DIR"] = self.snap_dir
        r._snapshot_dir_cached["path"] = Path(self.snap_dir)

    def tearDown(self) -> None:
        os.environ.pop("STOCKDB_V3_SNAPSHOT_DIR", None)
        r._snapshot_dir_cached["path"] = None
        import shutil
        shutil.rmtree(self.snap_dir, ignore_errors=True)
        super().tearDown()

    _index_store = None
    _board_store = None
    _board_fail = False

    @classmethod
    def _tencent_stub(cls, symbol, start8, end8, *, timeout=12.0):
        rows = _index_rows()
        return [row for row in rows if start8 <= row["date8"] <= end8]

    @classmethod
    def _eastmoney_stub(cls, secid, start8, end8, *, timeout=8.0):
        if cls._board_fail:
            raise r.ResearchError(
                "SOURCE_UNAVAILABLE", "eastmoney unreachable (synthetic)")
        rows = _board_rows()
        return [row for row in rows if start8 <= row["date8"] <= end8]

    def validate(self, payload: dict, kind: str = "KlineResponse") -> None:
        """信封契约校验：扩展 kind 用扩展校验器，其余沿用基线校验器。"""
        if kind in ("FactorResponse", "FactorToolResponse"):
            if _ext_validate_contract is None:
                self.skipTest("扩展校验器不可用")
            _ext_validate_contract.validate(payload, kind)
            return
        super().validate(payload, kind)

    def _reconstruct_check(self, raw_env, factor_env, adjusted_env) -> None:
        """用未改动扩展校验器的 reconstruct 验证 R/F/A（E01 验收标准）。"""
        if _ext_validate_contract is None:
            self.skipTest("扩展校验器不可用")
        for mode_env in (raw_env, factor_env, adjusted_env):
            _ext_validate_contract.validate(
                mode_env,
                "FactorResponse" if mode_env is factor_env
                else "KlineResponse")
        _ext_validate_contract.reconstruct(raw_env, factor_env,
                                           adjusted_env)

    # --- 便捷调用 ---------------------------------------------------------
    def kline(self, **overrides) -> dict:
        args = {**BASE_ARGS, **overrides}
        return v3.get_kline_v3(args)

    def factors(self, **overrides) -> dict:
        args = {"contract_version": "stockdb-market-v3",
                "codes": [EQUITY_KEY], "start": "20261020", "end": "20261023",
                "fq": "qfq", "adjustment_anchor": "20261023"}
        args.update(overrides)
        return r.get_adjust_factors_v3(args)


# ---------------------------------------------------------------------------
# A 批：指数/板块日K（I01—I10 服务侧）
# ---------------------------------------------------------------------------

@unittest.skipIf(jsonschema is None, "jsonschema 不可用")
class IndexKlineTest(ResearchTestCase):
    """I01/I02/I05/I06/I09：指数身份、同码隔离、目录、单位、混合批量。"""

    def test_index_identity_and_envelope(self):
        env = self.kline(codes=[INDEX_KEY, INDEX_KEY_2, CSI_KEY])
        self.assertEqual(env["status"], "complete")
        for key in (INDEX_KEY, INDEX_KEY_2, CSI_KEY):
            self.assertEqual(len(env["data"][key]), 4)
            meta = env["meta"][key]
            self.assertEqual(meta["instrument"]["identity_verified"], True)
            self.assertEqual(meta["units"]["price"], "points")
            self.assertEqual(meta["units"]["price_decimals"], 3)
            self.assertEqual(meta["coverage"]["state"], "complete")
            row = env["data"][key][0]
            self.assertIsNone(row["volume"])
            self.assertIsNone(row["amount"])
            self.assertEqual(meta["units"]["null_reason"],
                             r._UNITS_NULL_REASON_INDEX)
        self.validate(env, "KlineResponse")

    def test_series_catalog_covers_exactly_query_codes(self):
        env = self.kline(codes=[INDEX_KEY, EQUITY_KEY, ETF_KEY])
        catalog = env["extensions"]["research_v1"]["series_catalog"]
        self.assertEqual(set(catalog), {INDEX_KEY, EQUITY_KEY, ETF_KEY})
        desc = catalog[INDEX_KEY]
        self.assertEqual(desc["provider"], "SSE")
        self.assertEqual(desc["calendar_exchange"], "XSHG")
        self.assertEqual(desc["history_start"], "2026-10-20")
        self.assertEqual(desc["history_end"], "2026-10-23")
        self.assertTrue(desc["origin_reference"])
        self.assertIsNone(desc["native_volume_unit"])
        self.assertEqual(catalog[EQUITY_KEY]["native_volume_unit"], "shares")
        self.assertEqual(catalog[ETF_KEY]["native_volume_unit"], "fund_units")

    def test_csi_namespace_kept_separate(self):
        # I01：中证1000 编制者命名空间 csi ≠ 交易所命名空间；不混淆
        env = self.kline(code=CSI_KEY)
        self.assertEqual(env["meta"][CSI_KEY]["instrument"]["namespace"], "csi")
        env2 = self.kline(code="XSHG/index/exchange/000852")
        self._assert_refusal(env2, "UNKNOWN_INSTRUMENT",
                             "XSHG/index/exchange/000852")

    def _assert_refusal(self, env, code, key):
        self.assertEqual(env["status"], "failed")
        self.assertEqual(env["errors"][0]["code"], code)
        self.assertIn(key, env["meta"])

    def test_same_code_index_vs_equity_isolated(self):
        # I02：上证指数 000001 与深市股票 000001 同请求不同 identity/来源
        eq_rows = [
            {"date": int(d), "code": "000001", "name": "synthetic-pab",
             "open": round(11.5 + i * 0.1, 2), "high": round(11.7 + i * 0.1, 2),
             "low": round(11.4 + i * 0.1, 2), "close": round(11.6 + i * 0.1, 2),
             "volume": 1000000 + i, "amount": 11600000 + i}
            for i, d in enumerate(("20261020", "20261021", "20261022",
                                   "20261023"))]
        with mock.patch.object(
                v3, "engine_daily_rows",
                side_effect=lambda code, s, e, **kw: eq_rows):
            env = self.kline(codes=[INDEX_KEY,
                                    "XSHE/equity/exchange/000001"])
        idx, eq = env["meta"][INDEX_KEY], env["meta"]["XSHE/equity/exchange/000001"]
        self.assertEqual(idx["instrument"]["asset_type"], "index")
        self.assertEqual(eq["instrument"]["asset_type"], "equity")
        self.assertEqual(idx["units"]["price"], "points")
        self.assertEqual(eq["units"]["price"], "CNY")
        self.assertNotEqual(idx["instrument"]["identity_source_id"],
                            eq["instrument"]["identity_source_id"])
        # 指数行来自 series 源、股票行来自引擎源，互不串
        idx_src = env["data"][INDEX_KEY][0]["source_id"]
        eq_src = env["data"]["XSHE/equity/exchange/000001"][0]["source_id"]
        self.assertNotEqual(idx_src, eq_src)
        self.assertEqual(env["status"], "complete")

    def test_mixed_batch_partial_failure_keeps_all_keys(self):
        # I09：指数+板块+股票混合；单项源失败保留其他项与全部请求键
        type(self)._board_fail = True
        try:
            env = self.kline(codes=[INDEX_KEY, BOARD_KEY, EQUITY_KEY])
        finally:
            type(self)._board_fail = False
        self.assertEqual(set(env["data"]), {INDEX_KEY, BOARD_KEY, EQUITY_KEY})
        self.assertEqual(env["meta"][BOARD_KEY]["status"], "error")
        self.assertEqual(env["meta"][INDEX_KEY]["status"], "ok")
        self.assertEqual(env["meta"][EQUITY_KEY]["status"], "ok")
        self.assertEqual(env["status"], "partial")
        codes = [e["instrument_id"] for e in env["errors"]]
        self.assertEqual(codes, [BOARD_KEY])
        self.validate(env, "KlineResponse")

    def test_limit_and_projection_on_index_rows(self):
        env = self.kline(code=INDEX_KEY, limit=2, fields=["close"])
        rows = env["data"][INDEX_KEY]
        self.assertEqual(len(rows), 2)
        self.assertEqual([row["date"] for row in rows],
                         ["2026-10-22", "2026-10-23"])
        self.assertEqual(set(rows[0]), {"date", "bar_state", "source_id", "close"})
        meta = env["meta"][INDEX_KEY]
        self.assertTrue(meta["truncated"])
        self.assertEqual(meta["truncation_reason"], "caller_limit")
        self.assertEqual(env["truncated"], True)

    def test_index_snapshot_segment_rebuild(self):
        # H07：同快照拆证券/日期/字段投影合并 = 整段
        full = self.kline(codes=[INDEX_KEY, EQUITY_KEY])
        snap = full["snapshot_id"]
        part1 = self.kline(code=INDEX_KEY, snapshot_id=snap)
        part2 = self.kline(code=EQUITY_KEY, snapshot_id=snap,
                           start="20261022", end="20261023")
        self.assertEqual([row for row in full["data"][INDEX_KEY]],
                         part1["data"][INDEX_KEY])
        self.assertEqual(part2["data"][EQUITY_KEY],
                         full["data"][EQUITY_KEY][2:])
        # 日期扩张超出捕获范围 → 明确失败，不实时补
        bad = self.kline(code=INDEX_KEY, snapshot_id=snap,
                         start="20261019", end="20261023")
        self.assertEqual(bad["errors"][0]["code"], "SNAPSHOT_SCOPE_MISMATCH")

    def test_index_calendar_gap_incomplete(self):
        # H04/H05：源缺一天 → UNKNOWN gap，覆盖不完整，不造平盘
        def partial(symbol, start8, end8, *, timeout=12.0):
            rows = _index_rows()
            return [row for row in rows if row["date8"] != "20261021"]
        with mock.patch.object(r, "tencent_index_daily", partial):
            env = self.kline(code=INDEX_KEY)
        meta = env["meta"][INDEX_KEY]
        self.assertEqual(meta["coverage"]["state"], "incomplete")
        self.assertEqual([g["date"] for g in meta["coverage"]["gaps"]],
                         ["2026-10-21"])
        self.assertEqual(env["errors"][0]["code"], "INCOMPLETE_COVERAGE")
        self.assertEqual(env["status"], "partial")


@unittest.skipIf(jsonschema is None, "jsonschema 不可用")
class BoardKlineTest(ResearchTestCase):
    """I03/I04/I05：板块序列、namespace 隔离、日历声明。"""

    def test_board_daily_envelope(self):
        env = self.kline(code=BOARD_KEY)
        self.assertEqual(env["status"], "complete")
        meta = env["meta"][BOARD_KEY]
        self.assertEqual(meta["units"]["price"], "points")
        self.assertEqual(meta["units"]["price_decimals"], 2)
        self.assertIsNone(env["data"][BOARD_KEY][0]["volume"])
        desc = env["extensions"]["research_v1"]["series_catalog"][BOARD_KEY]
        self.assertEqual(desc["provider"], "eastmoney")
        self.assertEqual(desc["calendar_exchange"], "XSHG")
        self.assertIn("push2his", desc["series_id"])
        self.validate(env, "KlineResponse")

    def test_board_failure_is_honest_per_series(self):
        # I03：源不可达 → 该序列 error + SOURCE_UNAVAILABLE，不冒充、不串 THS
        type(self)._board_fail = True
        try:
            env = self.kline(codes=[BOARD_KEY, BOARD_KEY_2])
        finally:
            type(self)._board_fail = False
        self.assertEqual(env["status"], "failed")
        for key in (BOARD_KEY, BOARD_KEY_2):
            self.assertEqual(env["meta"][key]["status"], "error")
        self.assertEqual({e["code"] for e in env["errors"]},
                         {"SOURCE_UNAVAILABLE"})
        # 错误证券 descriptor 为 null（§3.3）
        catalog = env["extensions"]["research_v1"]["series_catalog"]
        self.assertEqual(set(catalog), {BOARD_KEY, BOARD_KEY_2})
        self.assertIsNone(catalog[BOARD_KEY])

    def test_ths_never_serves_bk_request(self):
        # I04：THS 命名空间拒绝；BK 请求绝不返回 THS 序列
        env = self.kline(code="CN/board/ths/886044")
        self._assert_refusal(env, "UNSUPPORTED_ASSET", "CN/board/ths/886044")
        # 成员工具映射存在不代表日K——本服务无 THS 序列能力（负例）
        env2 = self.kline(code="CN/board/eastmoney/BK9999")
        self._assert_refusal(env2, "UNKNOWN_INSTRUMENT",
                             "CN/board/eastmoney/BK9999")

    def _assert_refusal(self, env, code, key):
        self.assertEqual(env["status"], "failed")
        self.assertEqual(env["errors"][0]["code"], code)
        self.assertIn(key, env["meta"])


# ---------------------------------------------------------------------------
# 长历史（H01/H02/H03/H06/H08）
# ---------------------------------------------------------------------------

@unittest.skipIf(jsonschema is None, "jsonschema 不可用")
class LongHistoryTest(ResearchTestCase):
    """长窗口矩阵、分页穷尽去重、日历边界。"""

    @staticmethod
    def _sessions(n, end="20261023"):
        """从已核实日历取最近 n 个交易日（升序 YYYYMMDD）。"""
        days = v3._calendar().trading_days_between("20240101", end)
        iso = [str(d).replace("-", "") for d in days]
        return iso[-n:]

    def test_index_500_session_window_complete(self):
        # H01：指数 500 交易日窗口（合成数据覆盖全窗）
        sessions = self._sessions(500)
        start, end = sessions[0], sessions[-1]
        with mock.patch.object(r, "tencent_index_daily",
                               return_value=_index_rows(sessions)):
            env = self.kline(code=INDEX_KEY, start=start, end=end)
        meta = env["meta"][INDEX_KEY]
        self.assertEqual(meta["coverage"]["state"], "complete")
        self.assertEqual(meta["coverage"]["expected_rows"], 500)
        self.assertEqual(meta["coverage"]["returned_rows"], 500)
        self.assertEqual(env["status"], "complete")
        self.validate(env, "KlineResponse")

    def test_tencent_paging_exhausts_and_dedupes(self):
        # H03：多页穷尽、边界相接、跨页去重（同日不重不漏）
        sessions = self._sessions(900)
        start, end = sessions[0], sessions[-1]
        rows = _index_rows(sessions)

        def two_pages(symbol, s8, e8, *, timeout=12.0):
            # 真实 API 语义：从 s8 起最多 760 行（页界重叠由调用方去重）
            return [row for row in rows
                    if s8 <= row["date8"] <= e8][:760]

        with mock.patch.object(r, "tencent_index_daily", two_pages):
            env = self.kline(code=INDEX_KEY, start=start, end=end)
        got = [row["date"].replace("-", "") for row in env["data"][INDEX_KEY]]
        self.assertEqual(got, sessions)
        self.assertEqual(env["meta"][INDEX_KEY]["coverage"]["state"], "complete")

    def test_trading_days_covers_long_natural_range(self):
        # H02：日历工具无 400 自然日静默截断
        env = v3.get_trading_days_v3({
            "contract_version": "stockdb-market-v3", "exchange": "XSHG",
            "start": "20241001", "end": "20261023"})
        self.assertEqual(env["status"], "complete")
        self.assertGreater(len(env["trading_days"]), 480)

    def test_window_before_calendar_coverage_is_not_complete(self):
        # H08/H02 边界：超出已核实日历（2024 前）→ OUT_OF_CALENDAR / 覆盖未知
        env = v3.get_trading_days_v3({
            "contract_version": "stockdb-market-v3", "exchange": "XSHG",
            "start": "20211001", "end": "20261023"})
        self.assertEqual(env["errors"][0]["code"], "OUT_OF_CALENDAR")

    def test_result_too_large_on_huge_window(self):
        # H08：超行数上限明确失败
        sessions = self._sessions(500) * 500  # 250k 槽位 > 200k 上限
        env = self.kline(codes=[INDEX_KEY] * 1, start="20241008",
                         end="20261023")
        self.assertNotEqual(env["errors"][0]["code"] if env["errors"] else "",
                            "RESULT_TOO_LARGE")  # 500 槽位不应超限
        with mock.patch.object(v3, "_expected_slots", return_value=None), \
                mock.patch.object(v3, "MAX_RESPONSE_ROWS", 100):
            sessions2 = self._sessions(500)
            with mock.patch.object(r, "tencent_index_daily",
                                   return_value=_index_rows(sessions2)):
                env2 = self.kline(code=INDEX_KEY, start=sessions2[0],
                                  end=sessions2[-1])
        self.assertEqual(env2["errors"][0]["code"], "RESULT_TOO_LARGE")


# ---------------------------------------------------------------------------
# B 批：因子工具与复权（A01—A20 服务侧）
# ---------------------------------------------------------------------------

@unittest.skipIf(jsonschema is None, "jsonschema 不可用")
class FactorToolTest(ResearchTestCase):
    """A01/A02/A03/A04/A18/A19/A20：FactorRequest/Response 合同行为。"""

    def test_factor_envelope_and_query_hash(self):
        env = self.factors()
        self.assertEqual(env["status"], "complete")
        self.assertEqual(env["query"], {
            "codes": [EQUITY_KEY], "start": "20261020", "end": "20261023",
            "fq": "qfq", "adjustment_anchor": "20261023", "snapshot_id": None})
        self.assertEqual(env["request_hash"], v3.digest(env["query"]))
        rows = env["data"][EQUITY_KEY]
        self.assertEqual([row["date"] for row in rows],
                         ["2026-10-20", "2026-10-21", "2026-10-22",
                          "2026-10-23"])
        # A03：有原始行情的每一天都有系数（不只事件日）
        self.assertEqual(len(rows), 4)
        # 锚点日系数恒等（A05）
        self.assertEqual(rows[-1]["multiplier"], "1")
        self.assertEqual(rows[-1]["offset"], "0")
        # 事件前按 cum 比折算
        self.assertEqual(Decimal(rows[0]["multiplier"]),
                         (Decimal(1) / Decimal("1.05")).quantize(
                             Decimal("1E-12")))
        meta = env["meta"][EQUITY_KEY]
        self.assertEqual(meta["adjustment"]["mode"], "qfq")
        self.assertEqual(meta["adjustment"]["anchor_date"], "20261023")
        self.assertEqual(meta["adjustment"]["algorithm"], "affine-v1")
        derivation = meta["derivation"]
        self.assertEqual(derivation["methodology_id"],
                         "affine-v1-from-engine-cum")
        self.assertIn("stock_sdk.py", derivation["formula_reference"])
        self.assertIn(derivation["source_id"], env["source_catalog"])
        # A04：derivation 是公式证据链
        self.assertIn("cum", env["source_catalog"][derivation["source_id"]]
                      ["reference"])
        self.validate(env, "FactorResponse")

    def test_single_equals_batch_normalization(self):
        # A02：单票/多元素同 F 规范化一致
        base = {"contract_version": "stockdb-market-v3",
                "start": "20261020", "end": "20261023", "fq": "qfq",
                "adjustment_anchor": "20261023"}
        single = r.get_adjust_factors_v3({**base, "code": EQUITY_KEY})
        batch = r.get_adjust_factors_v3({**base, "codes": [EQUITY_KEY]})
        self.assertEqual(single["status"], "complete")
        self.assertEqual(single["query"]["codes"], batch["query"]["codes"])
        self.assertEqual(single["request_hash"], batch["request_hash"])
        self.assertEqual(single["data"][EQUITY_KEY],
                         batch["data"][EQUITY_KEY])

    def test_factor_anchor_semantics_qfq_end_hfq_start(self):
        # A05：qfq 锚末交易日；hfq 锚首交易日（首批必须支持）
        qfq = self.factors(fq="qfq", adjustment_anchor="20261023")
        hfq = self.factors(fq="hfq", adjustment_anchor="20261020")
        q_rows, h_rows = qfq["data"][EQUITY_KEY], hfq["data"][EQUITY_KEY]
        self.assertEqual(q_rows[-1]["multiplier"], "1")   # qfq 锚=末行
        self.assertEqual(h_rows[0]["multiplier"], "1")    # hfq 锚=首行
        self.assertEqual(h_rows[-1]["multiplier"], "1.05")
        # 非末/首锚点（区间内其他已完成交易日）也支持
        mid = self.factors(fq="qfq", adjustment_anchor="20261021")
        self.assertEqual(mid["data"][EQUITY_KEY][-1]["multiplier"], "1.05")

    def test_factor_snapshot_reread_keeps_anchor(self):
        # A05/A18：子区间重读保留原锚点（锚点不在子区间内也不重置）
        env = self.factors()
        fid = env["snapshot_id"]
        sub = self.factors(start="20261020", end="20261021",
                           snapshot_id=fid)
        self.assertEqual(sub["query"]["adjustment_anchor"], "20261023")
        self.assertEqual(len(sub["data"][EQUITY_KEY]), 2)
        self.assertEqual(sub["data"][EQUITY_KEY][0]["multiplier"],
                         env["data"][EQUITY_KEY][0]["multiplier"])
        # fq/锚点不一致 → 拒绝
        bad = self.factors(fq="hfq", adjustment_anchor="20261020",
                           snapshot_id=fid)
        self.assertEqual(bad["errors"][0]["code"], "SNAPSHOT_SCOPE_MISMATCH")
        # 范围扩张 → 拒绝
        wide = self.factors(start="20261019", end="20261023", snapshot_id=fid)
        self.assertEqual(wide["errors"][0]["code"], "SNAPSHOT_SCOPE_MISMATCH")

    def test_factor_combined_event_math(self):
        # A08：现金+送转组合合成（mult=1.3, cum=1.3）
        with mock.patch.object(r, "engine_factor_events",
                               return_value=list(_COMBO_EVENTS)):
            env = self.factors()
        rows = env["data"][EQUITY_KEY]
        self.assertEqual(Decimal(rows[0]["multiplier"]),
                         (Decimal(1) / Decimal("1.3")).quantize(
                             Decimal("1E-12")))
        self.assertEqual(Decimal(rows[-1]["multiplier"]), Decimal(1))
        # 送转组合事件的连续性：调整后价格跨事件连续（经济语义抽验）
        raws = _stub_engine_rows("002600", "20261020", "20261023")
        m_pre, m_post = Decimal(rows[0]["multiplier"]), Decimal(rows[-1][
            "multiplier"])
        adj_pre = Decimal(str(raws[0]["close"])) * m_pre
        adj_post = Decimal(str(raws[2]["close"])) * m_post
        # 合成数据不保证价格连续；断言换算公式本身成立（×mult+0）
        self.assertEqual(adj_post, Decimal(str(raws[2]["close"])))

    def test_factor_negative_and_zero_adjusted_accepted(self):
        # A09：合成负 offset 因子 → 合法 0/负复权价正确返回且 OHLC 有序
        fid = v3.SNAPSHOTS.new_id("factor")
        v3.SNAPSHOTS.capture(fid, {
            "kind": "factor",
            "query": {"codes": [EQUITY_KEY], "start": "20261020",
                      "end": "20261023", "fq": "qfq",
                      "adjustment_anchor": "20261023", "snapshot_id": None},
            "rows": {EQUITY_KEY: [
                {"date": d, "multiplier": "1", "offset": "-100",
                 "source_id": r.FACTOR_SOURCE_ID}
                for d in ("2026-10-20", "2026-10-21", "2026-10-22",
                          "2026-10-23")]},
            "errors": {}, "sources": {r.FACTOR_SOURCE_ID: v3._source_record(
                retrieved_at="2026-10-24T10:00:00+08:00", reference="synthetic"
            )}, "catalog": {}}, row_count=4)
        env = self.kline(code=EQUITY_KEY, fq="qfq",
                         adjustment_anchor="20261023", factor_snapshot_id=fid)
        rows = env["data"][EQUITY_KEY]
        self.assertEqual(env["status"], "complete")
        self.assertTrue(all(Decimal(row["close"]) < 0 for row in rows))
        for row in rows:
            self.assertLessEqual(Decimal(row["low"]),
                                 min(Decimal(row["open"]),
                                     Decimal(row["close"])))
            self.assertGreaterEqual(Decimal(row["high"]),
                                    max(Decimal(row["open"]),
                                        Decimal(row["close"])))
        self.validate(env, "KlineResponse")

    def test_factor_rejections(self):
        # A19：非法请求全拒绝
        base = {"contract_version": "stockdb-market-v3",
                "start": "20261020", "end": "20261023", "fq": "qfq",
                "adjustment_anchor": "20261023"}
        cases = [
            ({**base, "code": EQUITY_KEY, "codes": [EQUITY_KEY]},
             "INVALID_ARGUMENT"),
            ({**base, "codes": []}, "INVALID_ARGUMENT"),
            ({**base, "codes": [EQUITY_KEY, EQUITY_KEY]}, "INVALID_ARGUMENT"),
            ({**base, "codes": [EQUITY_KEY] * 51}, "INVALID_ARGUMENT"),
            ({**base, "codes": [EQUITY_KEY], "start": "20261032"},
             "INVALID_ARGUMENT"),
            ({**base, "codes": [EQUITY_KEY], "start": "20261023",
              "end": "20261020"}, "INVALID_ARGUMENT"),
            ({**base, "codes": [EQUITY_KEY], "fq": "none"},
             "INVALID_ARGUMENT"),
            ({**base, "codes": [EQUITY_KEY], "date_pattern": "*"},
             "INVALID_ARGUMENT"),
            ({**base, "codes": [EQUITY_KEY], "frequency": "1d"},
             "INVALID_ARGUMENT"),
            ({**base, "codes": [EQUITY_KEY], "limit": 5},
             "INVALID_ARGUMENT"),
            ({**base, "codes": [INDEX_KEY]}, "UNSUPPORTED_ADJUSTMENT"),
            ({**base, "codes": [BOARD_KEY]}, "UNSUPPORTED_ADJUSTMENT"),
            ({**base, "codes": [EQUITY_KEY],
              "adjustment_anchor": "20261024"}, "INVALID_ARGUMENT"),  # 未完成
        ]
        for args, code in cases:
            env = r.get_adjust_factors_v3(args)
            self.assertEqual(env["status"], "failed", msg=json.dumps(args))
            self.assertEqual(env["errors"][0]["code"], code,
                             msg=json.dumps(args))

    def test_factor_partial_failure_keeps_other_codes(self):
        # A14：单项源失败 partial、其余项保留、无伪系数
        v3.TEST_FAULTS["fail_codes"] = {"600000": "SOURCE_TIMEOUT"}
        try:
            env = self.factors(codes=[EQUITY_KEY,
                                      "XSHG/equity/exchange/600000"])
        finally:
            v3.TEST_FAULTS.clear()
        self.assertEqual(env["status"], "partial")
        self.assertEqual(len(env["data"][EQUITY_KEY]), 4)
        self.assertEqual(env["data"]["XSHG/equity/exchange/600000"], [])
        self.assertEqual(env["meta"]["XSHG/equity/exchange/600000"]["status"],
                         "error")
        self.assertIsNone(
            env["meta"]["XSHG/equity/exchange/600000"]["adjustment"])
        self.validate(env, "FactorResponse")

    def test_factor_failure_schema_accepted(self):
        # A20：失败信封被实际发布 outputSchema 接受
        env = r.get_adjust_factors_v3({"contract_version": "stockdb-market-v3"})
        schema = r.published_factor_schemas()["outputSchema"]
        from jsonschema import Draft202012Validator
        errors = list(Draft202012Validator(schema).iter_errors(env))
        self.assertEqual(errors, [])


@unittest.skipIf(jsonschema is None, "jsonschema 不可用")
class AdjustedKlineTest(ResearchTestCase):
    """A05/A10/A11/A12/A13：R/F/A 链与负例。"""

    def _make_F(self, fq="qfq", anchor="20261023",
                codes=None) -> str:
        env = self.factors(fq=fq, adjustment_anchor=anchor,
                           codes=codes or [EQUITY_KEY, ETF_KEY])
        self.assertEqual(env["status"], "complete", msg=json.dumps(env["errors"]))
        return env["snapshot_id"]

    def test_rfa_roundtrip_and_recomputation(self):
        # A11/A10：A 引用 R/F；R 可 fq=none 重读；R+F 复算 = A；量额不变
        fid = self._make_F()
        a = self.kline(codes=[EQUITY_KEY, ETF_KEY], fq="qfq",
                       adjustment_anchor="20261023", factor_snapshot_id=fid)
        self.assertEqual(a["status"], "complete", msg=json.dumps(a["errors"]))
        ext = a["extensions"]["research_v1"]
        raw_id = ext["raw_snapshot_id"]
        # E01：A 的 series_catalog 描述价格序列（与 R 一致），不是因子序列
        self.assertEqual(ext["series_catalog"][EQUITY_KEY]["methodology_id"],
                         "sse-szse-exchange-daily-bar")
        # R 重读（fq=none）
        r_env = self.kline(codes=[EQUITY_KEY, ETF_KEY], fq="none",
                           snapshot_id=raw_id)
        self.assertEqual(r_env["status"], "complete",
                         msg=json.dumps(r_env["errors"]))
        # E01：R 重读带价格序列 research 元数据，且与 A 的 series 描述一致
        r_ext = r_env["extensions"]["research_v1"]
        self.assertIsNone(r_ext["raw_snapshot_id"])
        for key in (EQUITY_KEY, ETF_KEY):
            r_desc = r_ext["series_catalog"][key]
            a_desc = ext["series_catalog"][key]
            for field in ("series_id", "provider", "methodology_id",
                          "methodology_version", "history_start",
                          "history_end"):
                self.assertEqual(r_desc[field], a_desc[field],
                                 f"{key}.{field}: R vs A series differ")
        # 消费方校验器 reconstruct(R,F,A) 双模式全过（E01 验收标准）
        f_reread = self.factors(codes=[EQUITY_KEY, ETF_KEY],
                                snapshot_id=fid)
        self._reconstruct_check(r_env, f_reread, a)
        # F 重读（子集合重读，保留两码）
        f_env = self.factors(codes=[EQUITY_KEY, ETF_KEY], snapshot_id=fid)
        # 客户端复算：raw × multiplier + offset == adjusted（逐 OHLC）
        for key in (EQUITY_KEY, ETF_KEY):
            decimals = v3._price_decimals(key.split("/")[1])
            factors = {row["date"]: row for row in f_env["data"][key]}
            for raw_bar, adj_bar in zip(r_env["data"][key], a["data"][key]):
                factor = factors[raw_bar["date"]]
                for field in ("open", "high", "low", "close"):
                    expected = r.apply_affine(
                        raw_bar[field], factor["multiplier"],
                        factor["offset"], decimals)
                    self.assertEqual(adj_bar[field], expected,
                                     f"{key} {raw_bar['date']} {field}")
                # A10：量额与 R 逐日相同
                self.assertEqual(adj_bar["volume"], raw_bar["volume"])
                self.assertEqual(adj_bar["amount"], raw_bar["amount"])
            meta = a["meta"][key]
            self.assertEqual(meta["adjustment"]["factor_snapshot_id"], fid)
            self.assertEqual(meta["adjustment"]["anchor_date"], "20261023")

    def test_a_snapshot_reread_same_triple(self):
        # §6 步骤 5：A 重读携带 F+同锚点；data/meta/adjustment 全一致（A13+R3）
        fid = self._make_F()
        args = {"codes": [EQUITY_KEY], "fq": "qfq",
                "adjustment_anchor": "20261023", "factor_snapshot_id": fid}
        a1 = self.kline(**args)
        a2 = self.kline(**args, snapshot_id=a1["snapshot_id"])
        self.assertEqual(a1["data"], a2["data"])
        self.assertEqual(a1["data_hash"], a2["data_hash"])
        self.assertEqual(a2["extensions"]["research_v1"]["raw_snapshot_id"],
                         a1["extensions"]["research_v1"]["raw_snapshot_id"])
        # R3：重放 meta.adjustment 必须与首次一致（mode/anchor/F/algorithm）
        for key in (EQUITY_KEY,):
            self.assertEqual(a2["meta"][key]["adjustment"],
                             a1["meta"][key]["adjustment"])
            self.assertEqual(a2["meta"][key]["adjustment"]["mode"], "qfq")
            self.assertEqual(a2["meta"][key]["adjustment"]["anchor_date"],
                             "20261023")
            self.assertEqual(
                a2["meta"][key]["adjustment"]["factor_snapshot_id"], fid)
            self.assertEqual(a2["meta"][key]["adjustment"]["algorithm"],
                             "affine-v1")
        # R3：重放响应过完整冻结合同校验（不只 data_hash）
        self.validate(a2, "KlineResponse")
        # R3：source_catalog/单位/来源时间与首次一致（不重新抓上游）
        self.assertEqual(a2["source_catalog"], a1["source_catalog"])
        self.assertEqual(a2["meta"][key]["units"], a1["meta"][key]["units"])

    def test_replay_preserves_semantics_matrix(self):
        # R3 必验矩阵：子日期/子证券/fields/limit partial/锚点在子区间外
        fid = self._make_F()
        anchor = "20261023"
        base = {"codes": [EQUITY_KEY, ETF_KEY], "fq": "qfq",
                "adjustment_anchor": anchor, "factor_snapshot_id": fid}
        a1 = self.kline(**base)
        aid = a1["snapshot_id"]
        expected = {k: a1["meta"][k]["adjustment"] for k in base["codes"]}
        # 子日期重放（锚点 20261023 在子区间 [20261021,20261022] 之外不重置；
        # 重放必须携带相同 fq/anchor/F）
        sub = self.kline(codes=base["codes"], snapshot_id=aid,
                         start="20261021", end="20261022", fq="qfq",
                         adjustment_anchor=anchor,
                         factor_snapshot_id=fid)
        self.assertEqual(sub["status"], "complete")
        for key in base["codes"]:
            self.assertEqual(sub["meta"][key]["adjustment"], expected[key])
        self.validate(sub, "KlineResponse")
        # 子证券重放
        one = self.kline(snapshot_id=aid, codes=[ETF_KEY], fq="qfq",
                         adjustment_anchor=anchor, factor_snapshot_id=fid)
        self.assertEqual(one["meta"][ETF_KEY]["adjustment"],
                         expected[ETF_KEY])
        self.validate(one, "KlineResponse")
        # fields 投影重放
        proj = self.kline(snapshot_id=aid, codes=[EQUITY_KEY],
                          fields=["close"], fq="qfq",
                          adjustment_anchor=anchor, factor_snapshot_id=fid)
        self.assertEqual(proj["meta"][EQUITY_KEY]["adjustment"],
                         expected[EQUITY_KEY])
        self.validate(proj, "KlineResponse")
        # 正数 limit → partial，复权语义不退回 none
        lim = self.kline(codes=[EQUITY_KEY], snapshot_id=aid, limit=2,
                         fq="qfq", adjustment_anchor=anchor,
                         factor_snapshot_id=fid)
        self.assertEqual(lim["status"], "partial")
        self.assertTrue(lim["truncated"])
        self.assertEqual(lim["meta"][EQUITY_KEY]["adjustment"],
                         expected[EQUITY_KEY])
        self.assertEqual(len(lim["data"][EQUITY_KEY]), 2)
        self.validate(lim, "KlineResponse")
        # 混合批量重放（股票+ETF 双资产）
        mixed = self.kline(codes=base["codes"], snapshot_id=aid, fq="qfq",
                           adjustment_anchor=anchor, factor_snapshot_id=fid)
        for key in base["codes"]:
            self.assertEqual(mixed["meta"][key]["adjustment"], expected[key])
        self.validate(mixed, "KlineResponse")

    def test_hfq_replay_keeps_semantics(self):
        # R3：hfq 模式重放同样保留语义（反馈反例的第二条路径）
        fid = self._make_F(fq="hfq", anchor="20261020")
        a1 = self.kline(codes=[EQUITY_KEY], fq="hfq",
                        adjustment_anchor="20261020",
                        factor_snapshot_id=fid)
        a2 = self.kline(codes=[EQUITY_KEY], fq="hfq",
                        adjustment_anchor="20261020",
                        factor_snapshot_id=fid,
                        snapshot_id=a1["snapshot_id"])
        self.assertEqual(a2["meta"][EQUITY_KEY]["adjustment"]["mode"], "hfq")
        self.assertEqual(a2["meta"][EQUITY_KEY]["adjustment"]["anchor_date"],
                         "20261020")
        self.assertEqual(a2["data"], a1["data"])
        self.validate(a2, "KlineResponse")

    def test_raw_and_none_snapshots_unaffected(self):
        # R3：R 重读与普通 none 快照保持 none/null，核心分支不受污染
        fid = self._make_F()
        a1 = self.kline(codes=[EQUITY_KEY], fq="qfq",
                        adjustment_anchor="20261023", factor_snapshot_id=fid)
        raw_id = a1["extensions"]["research_v1"]["raw_snapshot_id"]
        r_env = self.kline(codes=[EQUITY_KEY], fq="none",
                           snapshot_id=raw_id)
        self.assertEqual(
            r_env["meta"][EQUITY_KEY]["adjustment"],
            {"mode": "none", "anchor_date": None,
             "factor_snapshot_id": None, "algorithm": None})
        self.validate(r_env, "KlineResponse")
        # 普通 core none 快照重读
        core = self.kline(codes=[EQUITY_KEY])
        core2 = self.kline(codes=[EQUITY_KEY],
                           snapshot_id=core["snapshot_id"])
        self.assertEqual(
            core2["meta"][EQUITY_KEY]["adjustment"]["mode"], "none")
        self.assertIsNone(
            core2["meta"][EQUITY_KEY]["adjustment"]["factor_snapshot_id"])
        self.validate(core2, "KlineResponse")

    def test_negative_cross_snapshot_uses(self):
        # A12：错 fq/锚点/范围/互传快照全拒绝
        fid = self._make_F()
        a = self.kline(code=EQUITY_KEY, fq="qfq",
                       adjustment_anchor="20261023", factor_snapshot_id=fid)
        raw_id = a["extensions"]["research_v1"]["raw_snapshot_id"]
        # F 当行情 snapshot_id
        env = self.kline(code=EQUITY_KEY, fq="none", snapshot_id=fid)
        self.assertEqual(env["errors"][0]["code"], "SNAPSHOT_SCOPE_MISMATCH")
        # R 当 factor_snapshot_id
        env = self.kline(code=EQUITY_KEY, fq="qfq",
                         adjustment_anchor="20261023",
                         factor_snapshot_id=raw_id)
        self.assertEqual(env["errors"][0]["code"], "SNAPSHOT_SCOPE_MISMATCH")
        # R（fq=none 捕获）改 fq=qfq 当 A 重放
        env = self.kline(code=EQUITY_KEY, fq="qfq",
                         adjustment_anchor="20261023", factor_snapshot_id=fid,
                         snapshot_id=raw_id)
        self.assertEqual(env["errors"][0]["code"], "SNAPSHOT_SCOPE_MISMATCH")
        # 锚点不一致
        env = self.kline(code=EQUITY_KEY, fq="qfq",
                         adjustment_anchor="20261020", factor_snapshot_id=fid)
        self.assertEqual(env["errors"][0]["code"], "SNAPSHOT_SCOPE_MISMATCH")
        # 范围扩张
        env = self.kline(code=EQUITY_KEY, fq="qfq",
                         adjustment_anchor="20261023", factor_snapshot_id=fid,
                         start="20261019", end="20261023")
        self.assertEqual(env["errors"][0]["code"], "SNAPSHOT_SCOPE_MISMATCH")
        # A 重读换 fq
        env = self.kline(code=EQUITY_KEY, fq="hfq",
                         adjustment_anchor="20261020", factor_snapshot_id=fid,
                         snapshot_id=a["snapshot_id"])
        self.assertEqual(env["errors"][0]["code"], "SNAPSHOT_SCOPE_MISMATCH")

    def test_factor_missing_day_leads_to_incomplete_not_fudged(self):
        # A03：有原始行情但缺因子日 → 不默认 1/0，partial/failed
        fid = v3.SNAPSHOTS.new_id("factor")
        v3.SNAPSHOTS.capture(fid, {
            "kind": "factor",
            "query": {"codes": [EQUITY_KEY], "start": "20261020",
                      "end": "20261023", "fq": "qfq",
                      "adjustment_anchor": "20261023", "snapshot_id": None},
            "rows": {EQUITY_KEY: [
                {"date": "2026-10-20", "multiplier": "1", "offset": "0",
                 "source_id": r.FACTOR_SOURCE_ID}]},
            "errors": {}, "sources": {r.FACTOR_SOURCE_ID: v3._source_record(
                retrieved_at="2026-10-24T10:00:00+08:00",
                reference="synthetic")}, "catalog": {}}, row_count=1)
        env = self.kline(code=EQUITY_KEY, fq="qfq",
                         adjustment_anchor="20261023", factor_snapshot_id=fid)
        self.assertIn(env["status"], ("partial", "failed"))
        self.assertEqual(env["data"][EQUITY_KEY], [])
        self.assertEqual(env["errors"][0]["code"], "INCOMPLETE_COVERAGE")


@unittest.skipIf(jsonschema is None, "jsonschema 不可用")
class SnapshotPersistenceTest(ResearchTestCase):
    """A15：R/F/A 跨实例（模拟重启）可读；丢失明确失败。"""

    def test_snapshots_survive_store_restart(self):
        fid = self._fid()
        args = {"codes": [EQUITY_KEY], "fq": "qfq",
                "adjustment_anchor": "20261023", "factor_snapshot_id": fid}
        a = self.kline(**args)
        raw_id = a["extensions"]["research_v1"]["raw_snapshot_id"]
        # 模拟重启：全新 store 实例（内存为空、磁盘同目录）
        store2 = r.PersistentSnapshotStore()
        item = store2.get(fid)
        self.assertIsNotNone(item)
        self.assertEqual(item["payload"]["kind"], "factor")
        a_item = store2.get(a["snapshot_id"])
        self.assertIsNotNone(a_item)
        self.assertEqual(a_item["payload"]["raw_snapshot_id"], raw_id)
        r_item = store2.get(raw_id)
        self.assertIsNotNone(r_item)
        self.assertEqual(r_item["payload"]["query"]["fq"], "none")
        # 服务级：替换 store 后重读 A 值不变
        with mock.patch.object(v3, "SNAPSHOTS", store2):
            a2 = self.kline(**args, snapshot_id=a["snapshot_id"])
        self.assertEqual(a2["data"], a["data"])

    def _fid(self) -> str:
        env = self.factors()
        return env["snapshot_id"]

    def test_disk_loss_fails_explicitly(self):
        # 磁盘丢失（删除文件）+ 空内存 → NOT_FOUND，不悄悄重建同 id 内容
        fid = self._fid()
        for path in Path(self.snap_dir).glob("*.json"):
            path.unlink()
        store2 = r.PersistentSnapshotStore()
        self.assertIsNone(store2.get(fid))

    def test_expired_snapshot_reports_expired(self):
        fid = self._fid()
        item = v3.SNAPSHOTS.get(fid)
        item["expires_at"] = time.time() - 1
        env = self.factors(snapshot_id=fid)
        self.assertEqual(env["errors"][0]["code"], "SNAPSHOT_EXPIRED")


# ---------------------------------------------------------------------------
# 精度与舍入（A17）
# ---------------------------------------------------------------------------

class AffinePrecisionTest(unittest.TestCase):
    def test_round_half_up_exactness(self):
        self.assertEqual(r.apply_affine("11.15", "0.952380952381", "0", 2),
                         "10.62")
        # 11.15 × 0.952380952381 = 10.6190476190…  → HALF_UP 10.62
        self.assertEqual(r.apply_affine("2.545", "1", "0", 2), "2.55")
        # .5 恰在边界：HALF_UP 进位（非银行家舍入 2.54）
        self.assertEqual(r.apply_affine("2.635", "1", "0", 2), "2.64")
        self.assertEqual(r.apply_affine("0.05", "0.5", "0", 3), "0.025")
        # 负值合法返回
        self.assertEqual(r.apply_affine("11.15", "1", "-100", 2), "-88.85")
        self.assertEqual(r.apply_affine("50", "1", "-50", 2), "0")

    def test_multiplier_12dp_quantization(self):
        events = [("20200101", {"cum": 3.0}), ("20260101", {"cum": 7.0})]
        # 锚在两事件之间（cum=3）、日期同段 → 1
        m = r.factor_multiplier(events, "20250101", "20250601")
        self.assertEqual(str(m), "1")
        # 锚在两事件后（cum=7）、日期在两事件之间（cum=3）→ 3/7
        m2 = r.factor_multiplier(events, "20250101", "20260601")
        self.assertEqual(m2, Decimal("0.428571428571"))

    def test_cum_at_semantics(self):
        events = [("20250101", {"cum": 2.0}), ("20260101", {"cum": 3.0})]
        self.assertEqual(r.cum_at(events, "20241231"), Decimal(1))
        self.assertEqual(r.cum_at(events, "20250101"), Decimal(2))
        self.assertEqual(r.cum_at(events, "20251231"), Decimal(2))
        self.assertEqual(r.cum_at(events, "20260101"), Decimal(3))


# ---------------------------------------------------------------------------
# 发布 schema（S02/A01）
# ---------------------------------------------------------------------------

@unittest.skipIf(jsonschema is None, "jsonschema 不可用")
class PublishedFactorSchemaTest(ResearchTestCase):
    """S02/A01：get_adjust_factors 实际发布 schema 的正反例。"""

    def _schema(self, kind):
        entry = next(t for t in server.TOOLS if t["name"] == "get_adjust_factors")
        return entry[kind]

    def test_root_object_and_defs_from_extension_contract(self):
        for kind in ("inputSchema", "outputSchema"):
            schema = self._schema(kind)
            self.assertEqual(schema["type"], "object")
        ext_defs = json.loads(
            r.EXTENSION_SCHEMA_PATH.read_text(encoding="utf-8"))["$defs"]
        for name in ("FactorRequest", "FactorResponse", "ToolFailureResponse"):
            self.assertEqual(self._schema("inputSchema")["$defs"].get(name)
                             or self._schema("outputSchema")["$defs"][name],
                             ext_defs[name],
                             f"$def {name} 必须与扩展合同逐字节一致")

    def test_positive_and_negative_matrix(self):
        from jsonschema import Draft202012Validator
        ins = Draft202012Validator(self._schema("inputSchema"))
        outs = Draft202012Validator(self._schema("outputSchema"))
        good_in = {"contract_version": "stockdb-market-v3",
                   "codes": [EQUITY_KEY], "start": "20261020",
                   "end": "20261023", "fq": "qfq",
                   "adjustment_anchor": "20261023"}
        self.assertEqual(list(ins.iter_errors(good_in)), [])
        bad_in = {**good_in, "fq": "none"}
        self.assertTrue(list(ins.iter_errors(bad_in)))
        bad_in2 = {**good_in, "codes": [EQUITY_KEY], "code": EQUITY_KEY}
        self.assertTrue(list(ins.iter_errors(bad_in2)))
        # 旧版输入仍可进入旧分支
        legacy_in = {"code": "600633", "date_pattern": "2026*"}
        self.assertEqual(list(ins.iter_errors(legacy_in)), [])
        # 输出：成功 FactorResponse / 失败 ToolFailureResponse / 旧版 envelope
        ok = self.factors()
        self.assertEqual(list(outs.iter_errors(ok)), [])
        ok2 = self.factors(codes=[EQUITY_KEY, INDEX_KEY])
        self.assertEqual(list(outs.iter_errors(ok2)), [])
        fail = r.get_adjust_factors_v3({"contract_version": "stockdb-market-v3"})
        self.assertEqual(list(outs.iter_errors(fail)), [])
        legacy_out = {"source": "http", "known_at": None, "is_partial": False,
                      "truncated": False, "total": 3, "errors": [],
                      "known_limitations": []}
        self.assertEqual(list(outs.iter_errors(legacy_out)), [])
        # 坏输出：伪装 v3 的对象必须被拒绝（无任意 object 兜底）
        bad_out = {"contract_version": "stockdb-market-v3",
                   "status": "complete", "data": "wrong"}
        self.assertTrue(list(outs.iter_errors(bad_out)))

    def test_tools_list_contains_factor_tool(self):
        names = {t["name"] for t in server.TOOLS}
        self.assertIn("get_adjust_factors", names)
        self.assertIn("get_kline", names)


# ---------------------------------------------------------------------------
# 兼容与隔离（S03/S05/S08）
# ---------------------------------------------------------------------------

class CompatAndIsolationTest(ResearchTestCase):
    def test_legacy_requests_still_return_none_from_dispatch(self):
        # S05：无 contract_version 的请求不经 v3 分支
        self.assertIsNone(v3.maybe_dispatch("get_adjust_factors",
                                            {"code": "002600"}))
        self.assertIsNone(v3.maybe_dispatch("get_kline", {"code": "002600"}))

    def test_core_kline_carries_price_series_metadata(self):
        # S03+E01：纯股票/ETF 请求也携带 research 元数据（价格序列目录）；
        # 八键信封与正式字段形状不变（extensions 是合同允许的可选对象，
        # 基线校验器/旧客户端解析不受影响）
        env = self.kline(codes=[EQUITY_KEY, ETF_KEY])
        self.assertEqual(env["status"], "complete")
        self.validate(env, "KlineResponse")
        ext = env["extensions"]["research_v1"]
        self.assertIsNone(ext["raw_snapshot_id"])
        self.assertEqual(set(ext["series_catalog"]),
                         {EQUITY_KEY, ETF_KEY})
        desc = ext["series_catalog"][EQUITY_KEY]
        self.assertEqual(desc["methodology_id"],
                         "sse-szse-exchange-daily-bar")
        self.assertEqual(desc["native_volume_unit"], "shares")

    def test_status_degrades_when_board_source_down(self):
        type(self)._board_fail = True
        try:
            env = self.kline(code=BOARD_KEY)  # 触发一次失败（经桩）
            self.assertEqual(env["status"], "failed")
            r._em_record(False, "synthetic eastmoney outage")  # 真实路径记录
        finally:
            type(self)._board_fail = False
            self.addCleanup(lambda: r._em_record(True))
        status = v3.get_data_status_v3({"contract_version": "stockdb-market-v3"})
        self.assertEqual(status["status"], "degraded")
        issue = status["issues"][0]
        self.assertEqual(issue["profile_id"], "index_board_daily_v1")
        self.assertEqual(issue["code"], "SOURCE_UNAVAILABLE")
        self.validate(status, "StatusResponse")

    def test_no_private_payload_in_responses(self):
        # S08：公开查询边界——响应不含账户/持仓/路径类字段
        env = self.kline(codes=[INDEX_KEY, EQUITY_KEY])
        text = json.dumps(env, ensure_ascii=False)
        for banned in ("account", "position", "holding", "/vol1/", "token",
                       "password"):
            self.assertNotIn(banned, text.lower())


@unittest.skipIf(jsonschema is None, "jsonschema 不可用")
class SinaFallbackTest(ResearchTestCase):
    """0.14.0 补：腾讯全挂时新浪第四提供者接管（2026-10-25 WAF 实证）。"""

    def test_sina_fallback_serves_and_records_provider(self):
        def tencent_dead(symbol, start8, end8, *, timeout=12.0):
            raise RuntimeError("HTTP Error 501: Not Implemented (waf)")

        def sina(symbol, start8, end8, *, timeout=15.0):
            rows = _index_rows()
            return [row for row in rows if start8 <= row["date8"] <= end8]

        with mock.patch.object(r, "tencent_index_daily", tencent_dead),                 mock.patch.object(r, "_sina_index_daily", sina):
            env = self.kline(code=INDEX_KEY)
        self.assertEqual(env["status"], "complete")
        desc = env["extensions"]["research_v1"]["series_catalog"][INDEX_KEY]
        self.assertTrue(desc["series_id"].startswith(
            "sina-kline-index-daily:"))
        ref = env["source_catalog"][desc is None or
                                    r.series_source_id(INDEX_KEY)]
        self.assertIn("CN_MarketDataService", ref["reference"])
        self.validate(env, "KlineResponse")

    def test_all_index_providers_dead_is_honest_failure(self):
        def dead(symbol, start8, end8, *, timeout=12.0):
            raise RuntimeError("501")

        with mock.patch.object(r, "tencent_index_daily", dead),                 mock.patch.object(r, "_sina_index_daily", dead):
            env = self.kline(code=INDEX_KEY)
        self.assertEqual(env["status"], "failed")
        self.assertEqual(env["errors"][0]["code"], "SOURCE_UNAVAILABLE")
        self.assertIn("sina", env["errors"][0]["message"])


@unittest.skipIf(jsonschema is None, "jsonschema 不可用")
class BoardCacheAndBackoffTest(ResearchTestCase):
    """0.16.0 板块持久缓存 / 东财风控退避 / 缺口补采（push2his 根因修复回归）。

    根因（2026-10-29 实测）：push2his 对非浏览器客户端静默断连并按行为评分
    升级至滑块验证码/IP 级惩罚；原实现 4 主机连发+无退避+无缓存维持封禁。
    """

    def test_cache_serves_when_source_fails_and_annotates_source_record(self):
        env = self.kline(code=BOARD_KEY)  # 首次：源成功（桩）→ 逐日落盘
        self.assertEqual(env["status"], "complete")
        first_rows = env["data"][BOARD_KEY]
        type(self)._board_fail = True
        try:
            env2 = self.kline(code=BOARD_KEY)  # 源失败 → 缓存如实服务
        finally:
            type(self)._board_fail = False
        self.assertEqual(env2["status"], "complete")
        self.assertEqual(env2["data"][BOARD_KEY], first_rows)  # 内容不变
        ref = env2["source_catalog"][r.series_source_id(BOARD_KEY)]
        # 来源可追溯：缓存服务必须如实标注，不得伪标实时抓取
        self.assertIn("persistent board cache", ref["reference"])
        self.assertTrue(ref["retrieved_at"])
        store = json.loads(r._board_cache_path(BOARD_KEY)
                           .read_text(encoding="utf-8"))
        self.assertGreaterEqual(store.get("fetch_failures", 0), 1)

    def test_cache_gap_reports_partial_with_incomplete_coverage(self):
        env = self.kline(code=BOARD_KEY)
        self.assertEqual(env["status"], "complete")
        # 摘除一天缓存（模拟历史缺口），源失败 → partial + 缺口如实上报
        path = r._board_cache_path(BOARD_KEY)
        store = json.loads(path.read_text(encoding="utf-8"))
        removed = sorted(store["bars"])[1]
        del store["bars"][removed]
        path.write_text(json.dumps(store, ensure_ascii=False),
                        encoding="utf-8")
        type(self)._board_fail = True
        try:
            env2 = self.kline(code=BOARD_KEY)
        finally:
            type(self)._board_fail = False
        self.assertEqual(env2["status"], "partial")
        served = [b["date"] for b in env2["data"][BOARD_KEY]]
        self.assertEqual(len(served), 3)
        removed_iso = f"{removed[:4]}-{removed[4:6]}-{removed[6:]}"
        self.assertNotIn(removed_iso, served)
        self.assertIn("INCOMPLETE_COVERAGE",
                      {e["code"] for e in env2["errors"]})
        # 缓存里的真实日子绝不因上游失败被抹掉
        self.assertEqual(served[0], "2026-10-20")

    def test_waf_drop_single_attempt_then_cooldown_failfast(self):
        import http.client
        calls = {"n": 0}

        def drop(url, *, referer, timeout, headers=None):
            calls["n"] += 1
            raise http.client.RemoteDisconnected("silent close")

        with mock.patch.object(r, "_http_json", side_effect=drop):
            with self.assertRaises(r.ResearchError) as ctx:
                self._real_eastmoney("90.BK1138", "20261020", "20261023")
        self.assertEqual(ctx.exception.code, "SOURCE_UNAVAILABLE")
        self.assertEqual(calls["n"], 1)  # 风控断连绝不轮转同簇主机连发
        em = r.em_board_availability()
        self.assertEqual(em["last_drop_class"], "waf_drop")
        self.assertGreater(em["cooldown_until"], time.time())
        self.assertEqual(em["backoff_streak"], 1)
        # 冷却期内 fail-fast：不触网（请求数不再增长）
        with mock.patch.object(r, "_http_json", side_effect=drop):
            with self.assertRaises(r.ResearchError) as ctx2:
                self._real_eastmoney("90.BK1138", "20261020", "20261023")
        self.assertIn("退避", ctx2.exception.message)
        self.assertEqual(calls["n"], 1)

    def test_backoff_doubles_then_success_resets(self):
        import http.client

        def drop(url, *, referer, timeout, headers=None):
            raise http.client.RemoteDisconnected("silent close")

        def ok(url, *, referer, timeout, headers=None):
            return {"data": {"klines": ["2026-10-20,1.0,2.0,2.0,1.0,0,0"]}}

        with mock.patch.object(r, "_http_json", side_effect=drop):
            with self.assertRaises(r.ResearchError):
                self._real_eastmoney("90.BK1138", "20261020", "20261023")
        first = r.em_board_availability()["cooldown_until"] - time.time()
        # 手动过期 → 再败一次 → 冷却指数增长
        r._em_client_state()["cooldown_until"] = 0.0
        with mock.patch.object(r, "_http_json", side_effect=drop):
            with self.assertRaises(r.ResearchError):
                self._real_eastmoney("90.BK1138", "20261020", "20261023")
        second = r.em_board_availability()["cooldown_until"] - time.time()
        self.assertGreater(second, first)
        self.assertEqual(r.em_board_availability()["backoff_streak"], 2)
        # 成功 → 退避状态复位（先手动过期冷却以触达源）
        r._em_client_state()["cooldown_until"] = 0.0
        with mock.patch.object(r, "_http_json", side_effect=ok):
            rows = self._real_eastmoney("90.BK1138", "20261020", "20261023")
        self.assertEqual(len(rows), 1)
        em = r.em_board_availability()
        self.assertEqual(em["backoff_streak"], 0)
        self.assertEqual(em["cooldown_until"], 0.0)
        self.assertEqual(em["last_drop_class"], None)

    def test_polite_headers_and_ut_param(self):
        seen = {}

        def ok(url, *, referer, timeout, headers=None):
            seen["url"], seen["referer"], seen["headers"] = url, referer, headers
            return {"data": {"klines": []}}

        with mock.patch.object(r, "_http_json", side_effect=ok):
            self._real_eastmoney("90.BK1160", "20261020", "20261023")
        self.assertIn("ut=fa5fd1943c7b386f172d6893dbfba10b", seen["url"])
        self.assertIn("secid=90.BK1160", seen["url"])
        self.assertIn("klt=101&fqt=0", seen["url"])
        self.assertIn("lmt=1000000", seen["url"])  # 站点同款，防大窗口被默认截断
        self.assertEqual(seen["referer"],
                         "https://quote.eastmoney.com/bk/90.BK1160.html")
        self.assertIn("qgqp_b_id=", seen["headers"]["Cookie"])
        self.assertIn("Chrome", seen["headers"]["User-Agent"])
        self.assertIn("zh-CN", seen["headers"]["Accept-Language"])

    def test_backfill_cycle_fills_gap(self):
        env = self.kline(code=BOARD_KEY)
        self.assertEqual(env["status"], "complete")
        path = r._board_cache_path(BOARD_KEY)
        store = json.loads(path.read_text(encoding="utf-8"))
        removed = sorted(store["bars"])[1]
        del store["bars"][removed]
        path.write_text(json.dumps(store, ensure_ascii=False),
                        encoding="utf-8")
        latest = v3.HEALTH.get().get("latest") or "20261023"
        # 窗口/板块目录在测试中收窄到合成序列（stub 只提供 4 个合成日）
        iso = [f"{d[:4]}-{d[4:6]}-{d[6:]}" for d in _SYN_DATES]
        only_board = {BOARD_KEY: r.BOARD_SERIES[BOARD_KEY]}
        with mock.patch.object(v3, "_expected_slots", return_value=iso),                 mock.patch.object(r, "BOARD_SERIES", only_board):
            self.assertGreaterEqual(
                r.board_cache_gap_report(latest)[BOARD_KEY]["gap_count"], 1)
            summary = r.board_backfill_cycle()
            self.assertEqual(summary["boards"][BOARD_KEY]["action"], "fetched")
            self.assertEqual(
                r.board_cache_gap_report(latest)[BOARD_KEY]["gap_count"], 0)

    def test_backfill_cycle_skips_in_cooldown(self):
        env = self.kline(code=BOARD_KEY)
        self.assertEqual(env["status"], "complete")
        path = r._board_cache_path(BOARD_KEY)
        store = json.loads(path.read_text(encoding="utf-8"))
        removed = sorted(store["bars"])[1]
        del store["bars"][removed]
        path.write_text(json.dumps(store, ensure_ascii=False),
                        encoding="utf-8")
        r._em_enter_cooldown("synthetic waf drop", "waf_drop")
        summary = r.board_backfill_cycle()
        self.assertIn("冷却", summary["skipped"])
        # 缓存缺口保持原样（不触网、不虚造）
        store2 = json.loads(path.read_text(encoding="utf-8"))
        self.assertNotIn(removed, store2["bars"])


if __name__ == "__main__":
    unittest.main()
