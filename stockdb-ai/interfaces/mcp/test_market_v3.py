#!/usr/bin/env python3
"""test_market_v3 — stockdb-market-v3（修订 3.0.0）服务端分支测试。

离线运行（不连 NAS/引擎）：引擎/目录/同步日志全部桩化注入，信封一律用
**消费方交付包同源**的 validate_contract.py 校验（contract/ 目录，SHA256 与
MANIFEST 一致）。覆盖验收 C01—C22 的服务侧断言与 X01—X05 的拒绝声明；
C23—C24 属消费方项目接入，不在本文件范围。

设置 STOCKDB_V3_LIVE=1 且 STOCKDB_HOST/STOCKDB_PORT 指向真实引擎时，
额外运行 LiveEngineTests（真实数据形状/单位/日历交叉验证）。
"""

from __future__ import annotations

import json
import os
import sys
import threading
import time
import unittest
from pathlib import Path
from unittest import mock

_MCP_DIR = Path(__file__).resolve().parent
_BASE_DIR = _MCP_DIR.parent.parent
for _p in (str(_MCP_DIR), str(_BASE_DIR), str(_MCP_DIR / "contract")):
    if _p not in sys.path:
        sys.path.insert(0, _p)

from interfaces.mcp import market_v3 as v3  # noqa: E402
from interfaces.mcp import stockdb_mcp_server as server  # noqa: E402

_VC_REASON = "jsonschema 不可用（契约校验将跳过）"
try:
    import validate_contract  # 合同离线校验器（消费方同源）
except Exception as _vc_exc:  # noqa: BLE001 - jsonschema 缺失时跳过契约校验
    validate_contract = None
    _VC_REASON = f"jsonschema 不可用: {_vc_exc}"


# ---------------------------------------------------------------------------
# 桩引擎数据（合成值；与 examples.json 同风格，不含真实行情）
# ---------------------------------------------------------------------------

CATALOG = {"002600", "000967", "002328", "603516", "159915", "510300",
           "000001", "600000", "160316", "920002", "300750"}
LATEST = "20261023"
SYNC_SUCCESSES = [("2026-10-22", "15:48:11"), ("2026-10-23", "15:52:42")]

_EQUITY_ROWS = [
    {"date": 20261020, "code": "002600", "name": "synthetic-equity",
     "open": 10.29, "high": 11.46, "low": 10.22, "close": 11.15,
     "volume": 170526900, "amount": 1864140000},
    {"date": 20261021, "code": "002600", "name": "synthetic-equity",
     "open": 11.15, "high": 11.30, "low": 11.02, "close": 11.21,
     "volume": 98123400, "amount": 1098765000},
    {"date": 20261022, "code": "002600", "name": "synthetic-equity",
     "open": 11.20, "high": 11.40, "low": 11.11, "close": 11.33,
     "volume": 112233400, "amount": 1274000000},
    {"date": 20261023, "code": "002600", "name": "synthetic-equity",
     "open": 11.35, "high": 11.55, "low": 11.25, "close": 11.48,
     "volume": 123456700, "amount": 1414000000},
]
_ETF_ROWS = [
    {"date": 20261020, "code": "159915", "name": "synthetic-etf",
     "open": 2.549, "high": 2.610, "low": 2.541, "close": 2.600,
     "volume": 7605420900, "amount": 19738440000},
    {"date": 20261021, "code": "159915", "name": "synthetic-etf",
     "open": 2.600, "high": 2.640, "low": 2.590, "close": 2.620,
     "volume": 5400000100, "amount": 14130000000},
    {"date": 20261022, "code": "159915", "name": "synthetic-etf",
     "open": 2.620, "high": 2.660, "low": 2.611, "close": 2.650,
     "volume": 6100000200, "amount": 16150000000},
    {"date": 20261023, "code": "159915", "name": "synthetic-etf",
     "open": 2.650, "high": 2.700, "low": 2.640, "close": 2.690,
     "volume": 7000000300, "amount": 18800000000},
]
# 单行证券（区间内只有一天有行情 → 覆盖不完整，C10）
_ONE_DAY_ROWS = [
    {"date": 20261021, "code": "000967", "name": "synthetic-one-day",
     "open": 7.10, "high": 7.35, "low": 7.05, "close": 7.28,
     "volume": 12345600, "amount": 89765000},
]
# 全空证券（范围内无行 → 未解释空，C08）
_EMPTY_ROWS_CODES = {"002328"}

_STUB_ROWS: dict[str, list[dict]] = {
    "002600": _EQUITY_ROWS, "159915": _ETF_ROWS, "000967": _ONE_DAY_ROWS,
}

BASE_ARGS = {"contract_version": "stockdb-market-v3",
             "frequency": "1d", "start": "20261020", "end": "20261023",
             "fq": "none", "limit": 0}
EQUITY_KEY = "XSHE/equity/exchange/002600"
ETF_KEY = "XSHE/etf/exchange/159915"
ONEDAY_KEY = "XSHE/equity/exchange/000967"
EMPTY_KEY = "XSHE/equity/exchange/002328"


def _stub_engine_rows(code, start, end, *, timeout=15.0):
    faults = v3.TEST_FAULTS
    if code in faults.get("sleep_codes", {}):
        time.sleep(faults["sleep_codes"][code] / 1000.0)
    fail = faults.get("fail_codes", {}).get(code)
    if fail:
        raise v3.EngineFailure(fail, faults.get("fail_messages", {}).get(
            code, f"injected failure for {code}"))
    if code in _EMPTY_ROWS_CODES:
        return []
    rows = [r for r in _STUB_ROWS.get(code, [])
            if int(start) <= int(str(r["date"])[:8]) <= int(end)]
    return rows


class V3TestCase(unittest.TestCase):
    """公共桩化：目录/引擎/健康/同步日志全部注入，测试间快照仓清空。"""

    def setUp(self) -> None:
        v3.TEST_FAULTS.clear()
        v3.SNAPSHOTS.clear()
        self._patches = [
            mock.patch.object(v3, "engine_daily_rows", _stub_engine_rows),
            mock.patch.object(v3, "catalog_codes",
                              return_value=(set(CATALOG), "2026-10-23T16:00:00+08:00")),
            mock.patch.object(v3, "engine_latest_trade_date",
                              return_value=LATEST),
            mock.patch.object(v3, "sync_successes",
                              return_value=list(SYNC_SUCCESSES)),
            mock.patch.object(v3, "engine_version_label",
                              return_value="0.3.5-stockdb"),
        ]
        for p in self._patches:
            p.start()
            self.addCleanup(p.stop)
        v3.HEALTH.inject(LATEST)

    def kline(self, **overrides) -> dict:
        args = {**BASE_ARGS, **overrides}
        return v3.get_kline_v3(args)

    def validate(self, payload: dict, kind: str = "KlineResponse") -> None:
        """用消费方同源校验器验证信封（jsonschema 缺失时跳过并标记）。"""
        if validate_contract is None:
            self.skipTest(f"jsonschema 不可用，跳过契约校验: {_VC_REASON}")
        validate_contract.validate(payload, kind)


@unittest.skipUnless(validate_contract is not None,
                     f"jsonschema 不可用: {_VC_REASON}")
class ContractBundleTest(unittest.TestCase):
    """C02（离线部分）：合同文件与消费方交付包字节级一致，校验器自检通过。"""

    def test_contract_files_match_manifest(self):
        import hashlib
        manifest = (_MCP_DIR / "contract" / "MANIFEST.sha256").read_text()
        for line in manifest.strip().splitlines():
            expected, name = line.split()
            data = (_MCP_DIR / "contract" / name).read_bytes()
            self.assertEqual(hashlib.sha256(data).hexdigest(), expected,
                             f"contract file drifted: {name}")

    def test_validator_self_check(self):
        count, rejected = validate_contract.self_check()
        self.assertGreaterEqual(count, 15)
        self.assertGreaterEqual(rejected, 10)


class RequestValidationTest(V3TestCase):
    """C02：参数边界与错误分类（无法正规化 → ToolFailureResponse）。"""

    def _assert_tool_failure(self, env: dict, code: str) -> None:
        self.assertEqual(env["status"], "failed")
        self.assertEqual(len(env["errors"]), 1)
        self.assertEqual(env["errors"][0]["code"], code)
        self.assertIsNone(env["query"])
        self.assertIsNone(env["request_hash"])
        self.assertEqual(env["data"], {})
        self.assertEqual(env["meta"], {})
        self.assertEqual(env["source_catalog"], {})
        self.validate(env, "ToolFailureResponse")

    def _assert_refusal(self, env: dict, code: str, key: str) -> None:
        """身份/能力拒绝：请求可正规化 → 带 query 的 failed KlineResponse。"""
        self.assertEqual(env["status"], "failed")
        self.assertTrue(env["is_partial"])
        self.assertIsNotNone(env["query"])
        self.assertEqual(len(env["errors"]), 1)
        self.assertEqual(env["errors"][0]["code"], code)
        self.assertEqual(env["errors"][0]["instrument_id"], key)
        self.assertEqual(env["data"], {key: []})
        self.assertEqual(env["meta"][key]["status"], "error")
        self.validate(env)

    def test_code_and_codes_mutually_exclusive(self):
        env = self.kline(code=EQUITY_KEY, codes=[EQUITY_KEY])
        self._assert_tool_failure(env, "INVALID_ARGUMENT")

    def test_bare_code_is_ambiguous(self):
        env = self.kline(code="000001")
        self._assert_tool_failure(env, "AMBIGUOUS_INSTRUMENT")
        self.assertEqual(env["errors"][0]["details"]["received"], "000001")

    def test_bare_codes_element_is_ambiguous(self):
        env = self.kline(codes=[EQUITY_KEY, "000001"])
        self._assert_tool_failure(env, "AMBIGUOUS_INSTRUMENT")

    def test_legacy_comma_fields_string_rejected(self):
        env = self.kline(code=EQUITY_KEY, fields="close,volume")
        self._assert_tool_failure(env, "INVALID_ARGUMENT")

    def test_negative_limit_rejected(self):
        env = self.kline(code=EQUITY_KEY, limit=-1)
        self._assert_tool_failure(env, "INVALID_ARGUMENT")

    def test_impossible_date_rejected(self):
        env = self.kline(code=EQUITY_KEY, start="20260230", end="20261023")
        self._assert_tool_failure(env, "INVALID_ARGUMENT")

    def test_start_after_end_rejected(self):
        env = self.kline(code=EQUITY_KEY, start="20261023", end="20261020")
        self._assert_tool_failure(env, "INVALID_ARGUMENT")

    def test_unknown_parameter_rejected(self):
        env = self.kline(code=EQUITY_KEY, portfolio_tags=["private"])
        self._assert_tool_failure(env, "INVALID_ARGUMENT")

    def test_duplicate_codes_rejected(self):
        env = self.kline(codes=[EQUITY_KEY, EQUITY_KEY])
        self._assert_tool_failure(env, "INVALID_ARGUMENT")

    def test_over_50_codes_rejected(self):
        env = self.kline(codes=[f"XSHE/equity/exchange/0000{i:03d}" for i in range(51)])
        self._assert_tool_failure(env, "INVALID_ARGUMENT")

    def test_missing_fq_rejected(self):
        args = {k: v for k, v in {**BASE_ARGS, "code": EQUITY_KEY}.items()
                if k != "fq"}
        env = v3.get_kline_v3(args)
        self._assert_tool_failure(env, "INVALID_ARGUMENT")

    def test_qfq_refused_until_adjusted_history_accepted(self):
        env = self.kline(code=EQUITY_KEY, fq="qfq")
        self._assert_tool_failure(env, "UNSUPPORTED_ADJUSTMENT")

    def test_minute_frequency_refused(self):
        env = self.kline(code=EQUITY_KEY, frequency="1m")
        self._assert_tool_failure(env, "UNSUPPORTED_FREQUENCY")

    def test_weekly_frequency_refused(self):
        env = self.kline(code=EQUITY_KEY, frequency="1w")
        self._assert_tool_failure(env, "UNSUPPORTED_FREQUENCY")

    def test_index_refused(self):
        env = self.kline(code="XSHG/index/exchange/000001")
        self._assert_refusal(env, "UNSUPPORTED_ASSET",
                             "XSHG/index/exchange/000001")

    def test_board_refused(self):
        env = self.kline(code="CN/board/eastmoney/BK1037")
        self._assert_refusal(env, "UNSUPPORTED_ASSET",
                             "CN/board/eastmoney/BK1037")

    def test_beijing_exchange_refused(self):
        env = self.kline(code="XBSE/equity/exchange/920002")
        self._assert_refusal(env, "UNSUPPORTED_ASSET",
                             "XBSE/equity/exchange/920002")

    def test_lof_segment_unknown_instrument(self):
        env = self.kline(code="XSHE/etf/exchange/160316")
        self._assert_refusal(env, "UNKNOWN_INSTRUMENT",
                             "XSHE/etf/exchange/160316")

    def test_not_in_catalog_unknown_instrument(self):
        env = self.kline(code="XSHE/equity/exchange/999999")
        self._assert_refusal(env, "UNKNOWN_INSTRUMENT",
                             "XSHE/equity/exchange/999999")

    def test_exchange_mismatch_unknown_instrument(self):
        env = self.kline(code="XSHE/equity/exchange/600000")
        self._assert_refusal(env, "UNKNOWN_INSTRUMENT",
                             "XSHE/equity/exchange/600000")

    def test_non_exchange_namespace_refused(self):
        env = self.kline(code="XSHE/equity/akshare/002600")
        self._assert_refusal(env, "UNSUPPORTED_ASSET",
                             "XSHE/equity/akshare/002600")

    def test_anchor_with_fq_none_rejected(self):
        env = self.kline(code=EQUITY_KEY, adjustment_anchor="20261020")
        self._assert_tool_failure(env, "INVALID_ARGUMENT")

    def test_unknown_contract_version_rejected(self):
        result = v3.maybe_dispatch("get_kline", {
            **BASE_ARGS, "contract_version": "stockdb-market-v4",
            "code": EQUITY_KEY})
        env = result["structuredContent"]
        self._assert_tool_failure(env, "UNSUPPORTED_CONTRACT")
        self.assertEqual(env["errors"][0]["details"]["requested_contract"],
                         "stockdb-market-v4")
        self.assertTrue(result["isError"])

    def test_v3_on_non_core_tool_rejected(self):
        result = v3.maybe_dispatch("get_adjust_factors", {
            "contract_version": "stockdb-market-v3", "code": "002600"})
        env = result["structuredContent"]
        self._assert_tool_failure(env, "UNSUPPORTED_CONTRACT")


class KlineEnvelopeTest(V3TestCase):
    """C03/C04/C05/C06/C07/C08/C10/C11/C12/C16：信封语义。"""

    def test_complete_batch_validates_and_shapes(self):
        env = self.kline(codes=[EQUITY_KEY, ETF_KEY])
        self.assertEqual(env["status"], "complete")
        self.assertFalse(env["is_partial"])
        self.assertFalse(env["truncated"])
        self.assertEqual(env["errors"], [])
        self.assertEqual(set(env["data"]), {EQUITY_KEY, ETF_KEY})
        self.assertEqual(set(env["meta"]), {EQUITY_KEY, ETF_KEY})
        self.assertEqual(len(env["data"][EQUITY_KEY]), 4)
        self.assertEqual(len(env["data"][ETF_KEY]), 4)
        self.validate(env)

    def test_decimal_strings_and_units(self):
        env = self.kline(codes=[EQUITY_KEY, ETF_KEY])
        row = env["data"][EQUITY_KEY][0]
        self.assertIsInstance(row["open"], str)
        self.assertEqual(row["open"], "10.29")
        self.assertEqual(row["volume"], 170526900)
        self.assertEqual(row["amount"], "1864140000")
        self.assertEqual(env["meta"][EQUITY_KEY]["units"]["volume"], "shares")
        self.assertEqual(env["meta"][ETF_KEY]["units"]["volume"], "fund_units")
        self.assertEqual(env["meta"][ETF_KEY]["units"]["price_decimals"], 3)
        self.assertEqual(env["meta"][EQUITY_KEY]["units"]["price_decimals"], 2)

    def test_single_one_element_batch_equivalence(self):
        single = self.kline(code=EQUITY_KEY)
        one = self.kline(codes=[EQUITY_KEY])
        batch = self.kline(codes=[EQUITY_KEY, ETF_KEY])
        self.assertEqual(single["request_hash"], one["request_hash"])
        self.assertEqual(single["data"][EQUITY_KEY], one["data"][EQUITY_KEY])
        self.assertEqual(batch["data"][EQUITY_KEY], single["data"][EQUITY_KEY])
        for env in (single, one, batch):
            self.assertIsInstance(env["data"], dict)
            self.validate(env)

    def test_limit_latest_n_rows_ascending_and_truncated(self):
        env = self.kline(codes=[EQUITY_KEY], limit=2)
        self.assertEqual(env["status"], "partial")
        self.assertTrue(env["truncated"])
        rows = env["data"][EQUITY_KEY]
        self.assertEqual([r["date"] for r in rows],
                         ["2026-10-22", "2026-10-23"])
        meta = env["meta"][EQUITY_KEY]
        self.assertTrue(meta["truncated"])
        self.assertEqual(meta["truncation_reason"], "caller_limit")
        self.assertEqual(meta["coverage"]["available_rows"], 4)
        self.assertEqual(meta["coverage"]["returned_rows"], 2)
        self.validate(env)

    def test_limit_at_least_available_is_complete(self):
        env = self.kline(codes=[EQUITY_KEY], limit=10)
        self.assertEqual(env["status"], "complete")
        self.assertFalse(env["truncated"])
        self.assertEqual(len(env["data"][EQUITY_KEY]), 4)
        self.validate(env)

    def test_projection_keeps_mandatory_fields(self):
        env = self.kline(code=EQUITY_KEY, fields=["close", "volume"])
        row = env["data"][EQUITY_KEY][0]
        self.assertEqual(set(row), {"date", "bar_state", "source_id",
                                    "close", "volume"})
        self.validate(env)

    def test_range_inclusive_no_out_of_range(self):
        env = self.kline(code=EQUITY_KEY, start="20261021", end="20261022")
        dates = [r["date"] for r in env["data"][EQUITY_KEY]]
        self.assertEqual(dates, ["2026-10-21", "2026-10-22"])
        self.validate(env)

    def test_partial_interval_is_not_complete(self):
        env = self.kline(codes=[ONEDAY_KEY])
        self.assertEqual(env["status"], "partial")
        meta = env["meta"][ONEDAY_KEY]
        self.assertEqual(meta["coverage"]["state"], "incomplete")
        self.assertEqual(meta["status"], "partial")
        self.assertEqual(len(meta["coverage"]["gaps"]), 3)
        codes_seen = {e["code"] for e in env["errors"]}
        self.assertIn("INCOMPLETE_COVERAGE", codes_seen)
        self.validate(env)

    def test_unknown_empty_batch_is_not_success(self):
        env = self.kline(codes=[EMPTY_KEY])
        self.assertEqual(env["status"], "failed")
        self.assertTrue(env["is_partial"])
        meta = env["meta"][EMPTY_KEY]
        self.assertEqual(meta["status"], "error")
        self.assertEqual(len(meta["coverage"]["gaps"]), 4)
        self.assertEqual(meta["coverage"]["expected_rows"], 4)
        self.assertEqual(meta["coverage"]["returned_rows"], 0)
        self.assertTrue(any(e["code"] == "INCOMPLETE_COVERAGE"
                            for e in env["errors"]))
        self.validate(env)

    def test_zero_calendar_sessions_is_complete_empty(self):
        # 2026-10-17/18 = 周六/周日（范围内无交易日 → 合法空，expected_rows=0）
        env = self.kline(code=EQUITY_KEY, start="20261017", end="20261018")
        self.assertEqual(env["status"], "complete")
        self.assertEqual(env["data"][EQUITY_KEY], [])
        self.assertEqual(env["meta"][EQUITY_KEY]["status"], "empty")
        self.assertEqual(env["meta"][EQUITY_KEY]["coverage"]["expected_rows"], 0)
        self.validate(env)

    def test_bar_state_final_with_sync_evidence(self):
        env = self.kline(codes=[EQUITY_KEY])
        states = {r["bar_state"] for r in env["data"][EQUITY_KEY]}
        self.assertEqual(states, {"final"})
        bars_source = env["source_catalog"]["engine-daily-kline"]
        self.assertIsNotNone(bars_source["finality_evidence"])
        self.validate(env)

    def test_bar_state_unknown_without_sync_evidence(self):
        with mock.patch.object(v3, "sync_successes", return_value=[]):
            env = self.kline(codes=[EQUITY_KEY])
        # 无同步日志依据：最新交易日行不能标 final（也不伪称 preliminary）
        states = {r["bar_state"] for r in env["data"][EQUITY_KEY]}
        self.assertEqual(states, {"unknown"})
        self.validate(env)

    def test_no_extra_instrument_keys(self):
        env = self.kline(codes=[EQUITY_KEY])
        self.assertEqual(set(env["data"]), {EQUITY_KEY})

    def test_source_times_and_unknowns_disclosed(self):
        env = self.kline(codes=[EQUITY_KEY])
        source = env["source_catalog"]["engine-daily-kline"]
        self.assertIsNone(source["provider"])
        self.assertIn("provider", source["unknown_fields"])
        self.assertIn("published_at", source["unknown_fields"])
        self.assertIsNone(source["published_at"])
        self.assertLessEqual(source["retrieved_at"], env["served_at"])
        self.validate(env)

    def test_request_and_data_hashes_deterministic(self):
        env = self.kline(codes=[EQUITY_KEY])
        self.assertEqual(env["request_hash"], v3.digest(env["query"]))
        self.assertEqual(env["data_hash"], v3.digest(env["data"]))

    def test_history_outside_calendar_is_partial_not_complete(self):
        # 2023 年区间（有行但日历未核实）：expected_rows 未知 → 不能标 complete
        rows_2023 = [
            {"date": 20230103, "code": "002600", "name": "synthetic-equity",
             "open": 9.10, "high": 9.40, "low": 9.05, "close": 9.30,
             "volume": 8000000, "amount": 74400000}]
        with mock.patch.object(v3, "engine_daily_rows", return_value=rows_2023):
            env = self.kline(code=EQUITY_KEY, start="20230103", end="20230106")
        self.assertEqual(env["status"], "partial")
        self.assertEqual(env["meta"][EQUITY_KEY]["coverage"]["state"], "unknown")
        self.assertIsNone(env["meta"][EQUITY_KEY]["coverage"]["expected_rows"])
        self.assertTrue(any(e["code"] == "INCOMPLETE_COVERAGE"
                            for e in env["errors"]))
        self.validate(env)

    def test_history_outside_calendar_no_rows_is_failed(self):
        # 2023 年区间且引擎无行：无可用行情 → failed（不是成功空）
        env = self.kline(code=EQUITY_KEY, start="20230103", end="20230106")
        self.assertEqual(env["status"], "failed")
        self.assertEqual(env["meta"][EQUITY_KEY]["status"], "error")
        self.validate(env)

    def test_result_too_large_refused(self):
        # 51 天 × 4000 码规模的等价压缩：直接把 max_response_rows 调小触发
        with mock.patch.object(v3, "MAX_RESPONSE_ROWS", 3):
            env = self.kline(codes=[EQUITY_KEY, ETF_KEY])
        self.assertEqual(env["status"], "failed")
        self.assertEqual(env["errors"][0]["code"], "RESULT_TOO_LARGE")
        self.assertEqual(set(env["data"]), {EQUITY_KEY, ETF_KEY})
        self.validate(env)


class PerCodeFailureTest(V3TestCase):
    """C09/C18：逐码失败共存、超时与期限。"""

    def test_one_code_timeout_partial(self):
        # fail_codes 以原生代码为键（故障注入在引擎适配层，键=engine code）
        v3.TEST_FAULTS["fail_codes"] = {"159915": "SOURCE_TIMEOUT"}
        env = self.kline(codes=[EQUITY_KEY, ETF_KEY])
        self.assertEqual(env["status"], "partial")
        self.assertEqual(len(env["data"][EQUITY_KEY]), 4)
        self.assertEqual(env["data"][ETF_KEY], [])
        err = next(e for e in env["errors"] if e["instrument_id"] == ETF_KEY)
        self.assertEqual(err["code"], "SOURCE_TIMEOUT")
        self.assertTrue(err["retryable"])
        self.assertEqual(env["meta"][ETF_KEY]["status"], "error")
        self.validate(env)

    def test_all_codes_failed_is_failed(self):
        v3.TEST_FAULTS["fail_codes"] = {"002600": "SOURCE_UNAVAILABLE",
                                        "159915": "SOURCE_UNAVAILABLE"}
        env = self.kline(codes=[EQUITY_KEY, ETF_KEY])
        self.assertEqual(env["status"], "failed")
        self.assertTrue(env["is_partial"])
        self.assertEqual(len(env["errors"]), 2)
        self.validate(env)

    def test_deadline_exhaustion_yields_source_timeout(self):
        # 0.4s 期限 + 每码 0.3s 睡眠 → 第 2 码起应因期限耗尽报 SOURCE_TIMEOUT
        v3.TEST_FAULTS["sleep_codes"] = {code: 300 for code in
                                         ("002600", "159915")}
        with mock.patch.object(v3, "REQUEST_TIMEOUT_MS", 400):
            env = self.kline(codes=[EQUITY_KEY, ETF_KEY])
        codes_seen = {e["code"] for e in env["errors"]}
        self.assertIn("SOURCE_TIMEOUT", codes_seen)
        self.validate(env)

    def test_busy_error_carries_retry_after(self):
        # 占满 4 个行情槽 → 新请求 BUSY + retry_after_ms
        for _ in range(v3.MAX_CONCURRENCY):
            v3._kline_slots.acquire()
        try:
            env = self.kline(codes=[EQUITY_KEY])
            self.assertEqual(env["status"], "failed")
            err = env["errors"][0]
            self.assertEqual(err["code"], "BUSY")
            self.assertGreater(err["details"]["retry_after_ms"], 0)
            self.assertTrue(err["retryable"])
            self.validate(env, "ToolFailureResponse")
        finally:
            for _ in range(v3.MAX_CONCURRENCY):
                v3._kline_slots.release()
        # 释放后恢复
        env = self.kline(codes=[EQUITY_KEY])
        self.assertEqual(env["status"], "complete")


class SnapshotTest(V3TestCase):
    """C15：快照重读/子集/范围/过期/重启丢失。"""

    def test_replay_immutability(self):
        first = self.kline(codes=[EQUITY_KEY, ETF_KEY])
        snap_id = first["snapshot_id"]
        # 上游变化（桩数据替换）后重读：原始行/来源不变
        mutated = [dict(r) for r in _STUB_ROWS["002600"]]
        for r in mutated:
            r["close"] = 99.99
        with mock.patch.object(v3, "engine_daily_rows",
                               side_effect=lambda c, s, e, **k: mutated
                               if c == "002600" else _stub_engine_rows(c, s, e)):
            replay = self.kline(codes=[EQUITY_KEY], snapshot_id=snap_id)
        self.assertEqual(replay["data"][EQUITY_KEY], first["data"][EQUITY_KEY])
        self.assertEqual(replay["source_catalog"], first["source_catalog"])
        self.assertEqual(replay["snapshot_id"], snap_id)
        self.validate(replay)

    def test_subset_security_range_projection(self):
        first = self.kline(codes=[EQUITY_KEY, ETF_KEY])
        snap_id = first["snapshot_id"]
        replay = self.kline(codes=[EQUITY_KEY], start="20261021",
                            end="20261022", fields=["close"],
                            snapshot_id=snap_id)
        self.assertEqual(set(replay["data"]), {EQUITY_KEY})
        self.assertEqual([r["date"] for r in replay["data"][EQUITY_KEY]],
                         ["2026-10-21", "2026-10-22"])
        self.assertEqual(set(replay["data"][EQUITY_KEY][0]),
                         {"date", "bar_state", "source_id", "close"})
        self.validate(replay)

    def test_scope_mismatch_new_code(self):
        first = self.kline(codes=[EQUITY_KEY])
        env = self.kline(codes=[EQUITY_KEY, ETF_KEY],
                         snapshot_id=first["snapshot_id"])
        self.assertEqual(env["status"], "failed")
        self.assertEqual(env["errors"][0]["code"], "SNAPSHOT_SCOPE_MISMATCH")
        self.validate(env, "ToolFailureResponse")

    def test_scope_mismatch_wider_range(self):
        first = self.kline(code=EQUITY_KEY, start="20261021", end="20261022")
        env = self.kline(code=EQUITY_KEY, start="20261020", end="20261023",
                         snapshot_id=first["snapshot_id"])
        self.assertEqual(env["errors"][0]["code"], "SNAPSHOT_SCOPE_MISMATCH")

    def test_not_found_and_restart_loss(self):
        env = self.kline(codes=[EQUITY_KEY], snapshot_id="snap-kline-nope")
        self.assertEqual(env["errors"][0]["code"], "SNAPSHOT_NOT_FOUND")
        first = self.kline(codes=[EQUITY_KEY])
        v3.SNAPSHOTS.clear()  # 模拟重启丢快照
        env = self.kline(codes=[EQUITY_KEY], snapshot_id=first["snapshot_id"])
        self.assertEqual(env["errors"][0]["code"], "SNAPSHOT_NOT_FOUND")
        self.validate(env, "ToolFailureResponse")

    def test_expired_snapshot(self):
        first = self.kline(codes=[EQUITY_KEY])
        item = v3.SNAPSHOTS.get(first["snapshot_id"])
        item["expires_at"] = time.time() - 1
        env = self.kline(codes=[EQUITY_KEY], snapshot_id=first["snapshot_id"])
        self.assertEqual(env["errors"][0]["code"], "SNAPSHOT_EXPIRED")

    def test_retention_at_least_seven_days(self):
        self.assertGreaterEqual(v3.SNAPSHOT_RETENTION_SECONDS, 604800)


class ListCalendarStatusTest(V3TestCase):
    """C13/C14/C21：清单/日历/状态。"""

    def test_equity_list_excludes_etf(self):
        env = v3.get_stock_list_v3({
            "contract_version": "stockdb-market-v3",
            "asset_types": ["equity"], "exchanges": ["XSHE"],
            "as_of": LATEST})
        self.assertEqual(env["status"], "complete")
        assets = {item["asset_type"] for item in env["data"]}
        self.assertEqual(assets, {"equity"})
        exchanges = {item["exchange"] for item in env["data"]}
        self.assertEqual(exchanges, {"XSHE"})
        self.assertEqual(env["total"], len(env["data"]))
        ids = [i["instrument_id"] for i in env["data"]]
        self.assertEqual(len(ids), len(set(ids)))
        self.assertIn(EQUITY_KEY, ids)
        self.assertNotIn(ETF_KEY, ids)
        self.assertEqual(env["as_of_type"], "observed_current")
        self.assertTrue(env["universe_complete"])
        self.validate(env, "StockListResponse")

    def test_mixed_scope_labels_each_asset(self):
        env = v3.get_stock_list_v3({
            "contract_version": "stockdb-market-v3",
            "asset_types": ["equity", "etf"], "exchanges": ["XSHG", "XSHE"],
            "as_of": LATEST})
        pairs = {(i["asset_type"], i["exchange"]) for i in env["data"]}
        self.assertIn(("etf", "XSHG"), pairs)
        self.assertIn(("equity", "XSHG"), pairs)
        self.validate(env, "StockListResponse")

    def test_past_as_of_refused(self):
        env = v3.get_stock_list_v3({
            "contract_version": "stockdb-market-v3",
            "asset_types": ["equity"], "exchanges": ["XSHE"],
            "as_of": "20260101"})
        self.assertEqual(env["status"], "failed")
        self.assertEqual(env["errors"][0]["code"], "UNSUPPORTED_AS_OF")
        self.assertEqual(env["total"], 0)
        self.validate(env, "StockListResponse")

    def test_index_listing_refused(self):
        env = v3.get_stock_list_v3({
            "contract_version": "stockdb-market-v3",
            "asset_types": ["index"], "exchanges": ["XSHG"],
            "as_of": LATEST})
        self.assertEqual(env["errors"][0]["code"], "UNSUPPORTED_ASSET")

    def test_calendar_sorted_unique_bounded(self):
        env = v3.get_trading_days_v3({
            "contract_version": "stockdb-market-v3", "exchange": "XSHE",
            "start": "20261019", "end": "20261023"})
        self.assertEqual(env["status"], "complete")
        self.assertEqual(env["trading_days"],
                         ["2026-10-19", "2026-10-20", "2026-10-21",
                          "2026-10-22", "2026-10-23"])
        self.assertEqual(env["calendar_id"], "xshg-holiday-table")
        self.assertEqual(env["coverage_start"], "2024-01-01")
        self.assertEqual(env["coverage_end"], "2026-12-31")
        self.validate(env, "TradingDaysResponse")

    def test_calendar_out_of_coverage_refused(self):
        env = v3.get_trading_days_v3({
            "contract_version": "stockdb-market-v3", "exchange": "XSHG",
            "start": "20230101", "end": "20230131"})
        self.assertEqual(env["status"], "failed")
        self.assertEqual(env["errors"][0]["code"], "OUT_OF_CALENDAR")
        self.assertEqual(env["trading_days"], [])
        self.validate(env, "TradingDaysResponse")

    def test_calendar_longer_than_legacy_400_day_cap(self):
        # 2024-01-01..2025-12-31 ≈ 500+ 交易日 > 旧 400 硬上限 → 不截断
        env = v3.get_trading_days_v3({
            "contract_version": "stockdb-market-v3", "exchange": "XSHG",
            "start": "20240101", "end": "20251231"})
        self.assertEqual(env["status"], "complete")
        self.assertFalse(env["truncated"])
        self.assertGreater(len(env["trading_days"]), 400)
        self.validate(env, "TradingDaysResponse")

    def test_holiday_excluded(self):
        # 2026-10-01 国庆休市（表内），区间内不得出现
        env = v3.get_trading_days_v3({
            "contract_version": "stockdb-market-v3", "exchange": "XSHG",
            "start": "20260928", "end": "20261009"})
        self.assertNotIn("2026-10-01", env["trading_days"])
        self.validate(env, "TradingDaysResponse")

    def test_status_ready_profiles_limits_legacy(self):
        env = v3.get_data_status_v3({"contract_version": "stockdb-market-v3"})
        self.assertEqual(env["status"], "ready")
        self.assertEqual(env["issues"], [])
        self.assertEqual(env["legacy"], {"default_contract": "v2",
                                         "sunset_at": None})
        profiles = {p["id"]: p for p in env["profiles"]}
        self.assertEqual(set(profiles), {
            "core_daily_v1", "fullmarket_daily_v1", "index_board_daily_v1",
            "minute_v1", "adjusted_history_v1", "historical_identity_v1"})
        self.assertTrue(profiles["core_daily_v1"]["supported"])
        for pid in ("fullmarket_daily_v1", "index_board_daily_v1",
                    "minute_v1", "adjusted_history_v1", "historical_identity_v1"):
            self.assertFalse(profiles[pid]["supported"])
            self.assertTrue(profiles[pid]["reason"])
        self.assertEqual(env["limits"], {
            "max_codes": 50, "max_concurrency": 4,
            "request_timeout_ms": 30000, "max_response_rows": 200000,
            "snapshot_retention_seconds": 604800})
        self.assertIn("stockdb-market-v3", env["contract_versions"])
        self.assertIn("v2", env["contract_versions"])
        self.assertEqual(env["server"]["version"], server.SERVER_VERSION)
        self.assertTrue(env["server"]["build_id"])
        self.validate(env, "StatusResponse")

    def test_status_degraded_without_engine_health(self):
        v3.HEALTH.inject(None)
        env = v3.get_data_status_v3({"contract_version": "stockdb-market-v3"})
        self.assertEqual(env["status"], "degraded")
        self.assertGreaterEqual(len(env["issues"]), 1)
        self.assertEqual(env["issues"][0]["profile_id"], "core_daily_v1")
        self.assertTrue(env["issues"][0]["retryable"])
        self.validate(env, "StatusResponse")

    def test_status_p95_under_2s_during_slow_engine(self):
        # 慢引擎注入下状态仍只读缓存（20 次调用 p95 ≤ 2s，C19）
        def slow_probe(**kwargs):
            time.sleep(3.0)
            return None
        with mock.patch.object(v3, "engine_latest_trade_date", slow_probe):
            timings = []
            for _ in range(20):
                started = time.monotonic()
                env = v3.get_data_status_v3(
                    {"contract_version": "stockdb-market-v3"})
                timings.append(time.monotonic() - started)
                self.assertIn(env["status"], ("ready", "degraded"))
            timings.sort()
            p95 = timings[int(len(timings) * 0.95) - 1]
            self.assertLess(p95, 2.0, f"status p95={p95:.2f}s exceeds 2s")


class DualPayloadDispatchTest(V3TestCase):
    """C01/C17/C22：双载荷一致性、旧版兼容、legacy 回归。"""

    def test_v3_result_dual_payload_equal(self):
        result = server._call_tool("get_kline", {
            "contract_version": "stockdb-market-v3", **BASE_ARGS,
            "codes": [EQUITY_KEY, ETF_KEY]})
        self.assertIn("structuredContent", result)
        self.assertFalse(result.get("isError", False))
        text_blocks = [b for b in result["content"] if b["type"] == "text"]
        self.assertEqual(len(text_blocks), 1)
        self.assertEqual(json.loads(text_blocks[0]["text"]),
                         result["structuredContent"])

    def test_v3_failed_result_is_error(self):
        result = server._call_tool("get_kline", {
            "contract_version": "stockdb-market-v3", **BASE_ARGS,
            "code": "000001"})
        self.assertTrue(result["isError"])
        env = result["structuredContent"]
        self.assertEqual(env["errors"][0]["code"], "AMBIGUOUS_INSTRUMENT")

    def test_legacy_request_keeps_old_shape(self):
        with mock.patch.object(server, "query_daily_kline",
                               side_effect=lambda c, s, e, f: list(_EQUITY_ROWS)):
            result = server._call_tool("get_kline", {
                "code": "002600", "frequency": "1d", "start": "20261020",
                "end": "20261023"})
        self.assertNotIn("structuredContent", result)
        self.assertNotIn("isError", result)
        payload = json.loads(result["content"][0]["text"])
        for key in ("source", "known_at", "is_partial", "truncated",
                    "total", "errors", "known_limitations"):
            self.assertIn(key, payload)
        self.assertNotIn("contract_version", payload)
        self.assertNotIn("query", payload)

    def test_legacy_single_element_codes_keeps_flat_list(self):
        # L03：旧版单元素 codes 的扁平列表是文档化兼容行为（v3 才统一对象）
        with mock.patch.object(server, "_pybao_sdk_client",
                               return_value=_FakeSdkClient()):
            result = server._call_tool("get_kline", {
                "codes": ["002600"], "frequency": "1d", "start": "20261020",
                "end": "20261023", "limit": 10})
        payload = json.loads(result["content"][0]["text"])
        self.assertIsInstance(payload["data"], list)

    def test_legacy_batch_uses_per_code_sdk_calls(self):
        """L01/L02 回归：批量不再整批传 codes 给 SDK pipe（逐码调用）。"""
        fake = _FakeSdkClient()
        with mock.patch.object(server, "_pybao_sdk_client",
                               return_value=fake):
            result = server._call_tool("get_kline", {
                "codes": ["603516", "000967", "002600"], "frequency": "1d",
                "start": "20261020", "end": "20261023", "limit": 0})
        payload = json.loads(result["content"][0]["text"])
        # 逐码调用：3 次单码（每次 str），从不传 list
        self.assertEqual(fake.batch_calls, 0)
        self.assertEqual(sorted(fake.single_calls),
                         ["000967", "002600", "603516"])
        self.assertIsInstance(payload["data"], dict)
        for code in ("603516", "000967", "002600"):
            self.assertEqual(len(payload["data"][code]), 4)

    def test_tools_list_has_contract_version_schema(self):
        names = {t["name"] for t in server.TOOLS}
        for tool in ("get_kline", "get_stock_list", "get_trading_days",
                     "get_data_status"):
            self.assertIn(tool, names)
        kline_tool = next(t for t in server.TOOLS if t["name"] == "get_kline")
        # 0.13.1：inputSchema 为 anyOf[v3 合同分支, 旧版分支]；contract_version
        # 约束在 v3 分支（$defs/KlineRequest）内
        self.assertIn("anyOf", kline_tool["inputSchema"])
        self.assertIn("KlineRequest", kline_tool["inputSchema"]["$defs"])
        self.assertIn("outputSchema", kline_tool)

    def test_protocol_and_version_identity_independent(self):
        init = server.dispatch({"jsonrpc": "2.0", "id": 1,
                                "method": "initialize",
                                "params": {"protocolVersion": "2025-06-18"}})
        info = init["result"]["serverInfo"]
        self.assertEqual(info["version"], server.SERVER_VERSION)
        env = v3.get_data_status_v3({"contract_version": "stockdb-market-v3"})
        self.assertEqual(env["protocol_versions"], ["2025-06-18"])
        self.assertEqual(env["contract_revision"], "3.0.0")
        self.assertNotEqual(env["server"]["version"], "3.0.0")

    def test_json_and_error_transport(self):
        # JSON-RPC 协议层错误保持协议错误（不伪装行情成功/失败）
        resp = server.dispatch({"jsonrpc": "2.0", "id": 7,
                                "method": "no/such/method"})
        self.assertEqual(resp["error"]["code"], -32601)
        resp = server.dispatch({"jsonrpc": "2.0", "id": 8,
                                "method": "tools/call",
                                "params": {"name": "nope", "arguments": {}}})
        result = resp["result"]
        self.assertTrue(result.get("isError"))
        payload = json.loads(result["content"][0]["text"])
        self.assertIn("code", payload)


class _FakeSdkClient:
    """桩 SDK：记录单码/批量调用次数，返回确定性行（L01/L02 回归用）。"""

    def __init__(self):
        self.single_calls: list[str] = []
        self.batch_calls = 0

    def get_data(self, code, start=None, end=None, frequency="1d",
                 fields=None, fq=None, limit=None, **kwargs):
        if isinstance(code, list):
            self.batch_calls += 1
            return {c: [self._row(c, i) for i in range(4)]
                    for c in code}
        self.single_calls.append(str(code))
        return [self._row(str(code), i) for i in range(4)]

    @staticmethod
    def _row(code: str, i: int) -> dict:
        return {"date": 20261020 + i, "code": code, "open": 10.0,
                "high": 10.5, "low": 9.9, "close": 10.2, "volume": 1000,
                "amount": 10200}


class HashCanonicalTest(V3TestCase):
    """C16：canonical_json_v1 细则。"""

    def test_canonical_json_key_order_and_escape(self):
        self.assertEqual(
            v3.canonical_json({"b": 1, "a": "é"}),
            b'{"a":"\xc3\xa9","b":1}')

    def test_float_rejected_in_hash_input(self):
        with self.assertRaises(ValueError):
            v3.digest({"x": 1.5})

    def test_integer_range_guard(self):
        with self.assertRaises(ValueError):
            v3.digest({"x": 2**53})

    def test_single_and_one_element_same_query_hash(self):
        q_single = v3._validate_kline_request(
            {"contract_version": "stockdb-market-v3", "code": EQUITY_KEY,
             "frequency": "1d", "start": "20261020", "end": "20261021",
             "fq": "none"})
        q_array = v3._validate_kline_request(
            {"contract_version": "stockdb-market-v3", "codes": [EQUITY_KEY],
             "frequency": "1d", "start": "20261020", "end": "20261021",
             "fq": "none"})
        self.assertEqual(v3.digest(q_single), v3.digest(q_array))
        self.assertEqual(q_single["fields"], list(v3.FIELDS_ORDER))

    def test_decimal_str_no_scientific_notation(self):
        self.assertEqual(v3.decimal_str(1864140000), "1864140000")
        self.assertEqual(v3.decimal_str(2.549), "2.549")
        self.assertEqual(v3.decimal_str(10.0), "10")


@unittest.skipUnless(validate_contract is not None,
                     f"jsonschema 不可用: {_VC_REASON}")
class PublishedSchemaTest(V3TestCase):
    """FEEDBACK 2026-10-24 §4/§6.3：**实际发布** tools/list schema 的正反例矩阵。

    反例/正例取自消费方 schema-repro.json 原文（4 坏输出 + 3 坏输入 + bool_limit
    对照）；正例含合同 examples.json 请求、真实生成的 v3 信封与旧版 envelope。
    针对运行部署所使用的工具描述（server.TOOLS），不是独立的合同文件。
    """

    # --- 消费方反例原文（schema-repro.json） ---
    BAD_OUTPUT = {"contract_version": "stockdb-market-v3",
                  "status": "complete", "data": "wrong"}
    BAD_INPUT_BOTH_CODE_AND_CODES = {
        "contract_version": "stockdb-market-v3",
        "codes": ["XSHE/equity/exchange/002600"], "frequency": "1d",
        "start": "20261020", "end": "20261023", "fq": "none", "limit": 0,
        "code": "XSHE/equity/exchange/002600"}
    BAD_INPUT_FIELDS_WRONG_TYPE = {
        "contract_version": "stockdb-market-v3",
        "codes": ["XSHE/equity/exchange/002600"], "frequency": "1d",
        "start": "20261020", "end": "20261023", "fq": "none", "limit": 0,
        "fields": 123}
    BAD_INPUT_NO_END = {
        "contract_version": "stockdb-market-v3",
        "codes": ["XSHE/equity/exchange/002600"], "frequency": "1d",
        "start": "20261020", "fq": "none", "limit": 0}

    @staticmethod
    def _schema(tool: str, kind: str) -> dict:
        entry = next(t for t in server.TOOLS if t["name"] == tool)
        return entry[kind]

    def _assert_schema(self, tool: str, kind: str, instance, expected_valid: bool):
        from jsonschema import Draft202012Validator
        schema = self._schema(tool, kind)
        errors = list(Draft202012Validator(schema).iter_errors(instance))
        valid = not errors
        self.assertEqual(
            valid, expected_valid,
            f"{tool}.{kind} 对实例的判定={valid}（期望={expected_valid}）；"
            f"首个错误: {errors[0].message[:160] if errors else '无'}")

    # --- 反例：必须全部拒绝 ---

    def test_bad_v3_output_rejected_by_all_four_output_schemas(self):
        """FEEDBACK §4.1：宽泛旧分支不得吞掉非法 v3 对象（0.13.0 缺陷反例）。"""
        for tool in ("get_kline", "get_stock_list", "get_trading_days",
                     "get_data_status"):
            self._assert_schema(tool, "outputSchema", self.BAD_OUTPUT, False)

    def test_bad_v3_inputs_rejected_by_kline_input_schema(self):
        """FEEDBACK §4.2：code+codes / fields=123 / 缺 end 必须被发布 schema 拒绝。"""
        for bad in (self.BAD_INPUT_BOTH_CODE_AND_CODES,
                    self.BAD_INPUT_FIELDS_WRONG_TYPE,
                    self.BAD_INPUT_NO_END):
            self._assert_schema("get_kline", "inputSchema", bad, False)

    def test_bool_limit_control_rejected(self):
        """消费方对照用例：limit=true 必须仍被拒绝（证明真的执行了类型检查）。"""
        bad = {**self.BAD_INPUT_NO_END, "end": "20261023", "limit": True}
        self._assert_schema("get_kline", "inputSchema", bad, False)

    def test_unknown_contract_version_rejected_by_published_schema(self):
        """未知版本不匹配任何分支（v3 分支=合同 const；旧分支排除 contract_version）；
        运行时对直连调用方仍返回 UNSUPPORTED_CONTRACT（分类不变）。"""
        bad = {**BASE_ARGS, "contract_version": "stockdb-market-v4",
               "code": EQUITY_KEY}
        self._assert_schema("get_kline", "inputSchema", bad, False)

    def test_null_values_rejected_by_schema_and_runtime(self):
        """显式 null 与缺省不等价：schema 与运行时同判非法（口径一致）。"""
        for field in ("limit", "snapshot_id", "fields"):
            bad = {**BASE_ARGS, "code": EQUITY_KEY, field: None}
            self._assert_schema("get_kline", "inputSchema", bad, False)
            env = v3.get_kline_v3(bad)
            self.assertEqual(env["status"], "failed", field)
            self.assertEqual(env["errors"][0]["code"], "INVALID_ARGUMENT", field)

    # --- 正例：必须仍然接受 ---

    def test_contract_example_requests_accepted(self):
        """合同 examples.json 的 7 个请求对相应发布 inputSchema 全部合法。"""
        examples = json.loads((_MCP_DIR / "contract" / "examples.json")
                              .read_text(encoding="utf-8"))
        tool_by_schema = {"KlineRequest": "get_kline",
                          "StockListRequest": "get_stock_list",
                          "TradingDaysRequest": "get_trading_days",
                          "StatusRequest": "get_data_status"}
        for example in examples["requests"]:
            tool = tool_by_schema[example["schema"]]
            self._assert_schema(tool, "inputSchema", example["payload"], True)

    def test_acceptance_c03_request_accepted(self):
        request = {**BASE_ARGS, "codes": [
            "XSHG/equity/exchange/603516", "XSHE/equity/exchange/000967",
            "XSHE/equity/exchange/002328", "XSHE/equity/exchange/002600",
            "XSHE/etf/exchange/159915"]}
        self._assert_schema("get_kline", "inputSchema", request, True)

    def test_legacy_inputs_accepted(self):
        """旧版输入（无 contract_version）仍匹配旧分支，不受 v3 收紧影响。"""
        self._assert_schema("get_kline", "inputSchema",
                            {"code": "600633", "frequency": "1d",
                             "start": "20260620"}, True)
        self._assert_schema("get_kline", "inputSchema",
                            {"codes": ["600633", "000001"],
                             "fields": "close,volume", "limit": 10}, True)
        self._assert_schema("get_stock_list", "inputSchema", {}, True)
        self._assert_schema("get_trading_days", "inputSchema",
                            {"start": "20260101"}, True)
        self._assert_schema("get_data_status", "inputSchema", {}, True)

    def test_real_v3_outputs_accepted(self):
        """真实生成的 v3 信封（完整/部分/失败/工具失败/清单/日历/状态）
        对各自发布 outputSchema 全部合法。"""
        envelopes = {
            "get_kline": [self.kline(codes=[EQUITY_KEY, ETF_KEY]),
                          self.kline(codes=[EQUITY_KEY, ONEDAY_KEY]),
                          self.kline(codes=[EMPTY_KEY]),
                          v3.tool_failure([v3.V3Error("AMBIGUOUS_INSTRUMENT",
                                                       "x")])],
            "get_stock_list": [v3.get_stock_list_v3({
                "contract_version": "stockdb-market-v3",
                "asset_types": ["equity"], "exchanges": ["XSHE"],
                "as_of": LATEST})],
            "get_trading_days": [v3.get_trading_days_v3({
                "contract_version": "stockdb-market-v3", "exchange": "XSHE",
                "start": "20261020", "end": "20261023"})],
            "get_data_status": [v3.get_data_status_v3(
                {"contract_version": "stockdb-market-v3"})],
        }
        for tool, items in envelopes.items():
            for env in items:
                self._assert_schema(tool, "outputSchema", env, True)

    def test_legacy_envelope_accepted(self):
        """旧版 8 键 envelope（无 contract_version）仍匹配旧分支。"""
        legacy = {"source": "http", "known_at": None, "is_partial": False,
                  "truncated": False, "total": 4, "errors": [],
                  "known_limitations": [], "code": "002600", "data": []}
        for tool in ("get_kline", "get_stock_list", "get_trading_days",
                     "get_data_status"):
            self._assert_schema(tool, "outputSchema", legacy, True)

    # --- 发布内容与冻结合同的一致性 ---

    def test_published_defs_equal_contract_defs(self):
        """嵌入 $defs 与冻结合同文件逐字节一致（程序化抽取，防漂移）。"""
        contract = json.loads((_MCP_DIR / "contract" / "contract.schema.json")
                              .read_text(encoding="utf-8"))
        for tool in ("get_kline", "get_stock_list", "get_trading_days",
                     "get_data_status"):
            for kind in ("inputSchema", "outputSchema"):
                for name, defn in self._schema(tool, kind).get("$defs", {}).items():
                    if name == "LegacyToolResult":
                        continue
                    self.assertEqual(
                        defn, contract["$defs"][name],
                        f"{tool}.{kind} 的 def {name} 与合同定义不一致")

    def test_603516_example_exchange_fixed(self):
        """FEEDBACK §4.2：603516 是上交所代码，示例不得再写 XSHE。"""
        entry = next(t for t in server.TOOLS if t["name"] == "get_kline")
        self.assertIn("XSHG/equity/exchange/603516", entry["description"])
        self.assertNotIn("XSHE/equity/exchange/603516", entry["description"])

    def test_output_schema_root_type_object(self):
        """FEEDBACK 1024 §3：MCP Tool 定义要求 input/output schema 根节点
        显式 type=object（8 个发布 schema 全部检查）。"""
        for tool in ("get_kline", "get_stock_list", "get_trading_days",
                     "get_data_status"):
            for kind in ("inputSchema", "outputSchema"):
                self.assertEqual(self._schema(tool, kind).get("type"), "object",
                                 f"{tool}.{kind} 根节点缺 type:object")

    def test_legal_failure_envelopes_accepted_by_own_output_schema(self):
        """FEEDBACK 1024 §2：状态/清单/日历的统一失败信封（ToolFailureResponse）
        是合同 §4.3 要求的合法输出——服务自己返回的失败必须被自己发布的
        outputSchema 接受（0.13.1 遗漏，3 例实测被自拒）。"""
        failures = {
            "get_stock_list": v3.get_stock_list_v3(
                {"contract_version": "stockdb-market-v3"}),  # 缺必填 → 参数失败
            "get_trading_days": v3.get_trading_days_v3(
                {"contract_version": "stockdb-market-v3",
                 "exchange": "XSHG", "start": "20261020"}),  # 缺 end
            "get_data_status": v3.get_data_status_v3(
                {"contract_version": "stockdb-market-v3",
                 "unexpected_public_parameter": True}),  # 未知参数
        }
        for tool, envelope in failures.items():
            self.assertEqual(envelope["status"], "failed", tool)
            self.validate(envelope, "ToolFailureResponse")
            self._assert_schema(tool, "outputSchema", envelope, True)
        # UNSUPPORTED_CONTRACT（v3 分发层统一失败信封）同样必须被接受
        result = v3.maybe_dispatch("get_stock_list", {
            "contract_version": "stockdb-market-v4"})
        for tool in ("get_stock_list", "get_trading_days", "get_data_status"):
            self._assert_schema(tool, "outputSchema",
                                result["structuredContent"], True)
        # 对照：get_kline 的失败信封由 KlineResponse 分支覆盖（0.13.1 起即通过）
        self._assert_schema("get_kline", "outputSchema",
                            self.kline(code="000001"), True)

    def test_failure_branch_does_not_swallow_malformed_v3(self):
        """新增 ToolFailureResponse 分支不得重新放宽：坏 v3 输出仍被全拒。"""
        for tool in ("get_kline", "get_stock_list", "get_trading_days",
                     "get_data_status"):
            self._assert_schema(tool, "outputSchema", self.BAD_OUTPUT, False)

    def test_published_defs_cover_tool_failure_closure(self):
        """非kline三工具的 $defs 含 ToolFailureResponse 及其传递依赖。"""
        for tool in ("get_stock_list", "get_trading_days", "get_data_status"):
            defs = self._schema(tool, "outputSchema")["$defs"]
            self.assertIn("ToolFailureResponse", defs, tool)
            # ToolFailureResponse 依赖 KlineResponse 全链（allOf $ref）
            self.assertIn("KlineResponse", defs, tool)
            self.assertIn("ResponseHeader", defs, tool)
        # get_kline 无需显式 ToolFailureResponse 分支（KlineResponse 已含失败形状）
        self.assertNotIn("ToolFailureResponse",
                         self._schema("get_kline", "outputSchema")["$defs"])

    def test_legacy_branch_excludes_contract_version(self):
        """四个发布 schema 的旧分支都带 not:required[contract_version]。"""
        for tool in ("get_kline", "get_stock_list", "get_trading_days",
                     "get_data_status"):
            legacy_branches = [
                b for b in self._schema(tool, "inputSchema")["anyOf"]
                if "not" in b]
            self.assertTrue(legacy_branches, tool)
            self.assertIn("required", legacy_branches[0]["not"])
            self.assertEqual(legacy_branches[0]["not"]["required"],
                             ["contract_version"])


@unittest.skipUnless(os.environ.get("STOCKDB_V3_LIVE") == "1",
                     "设置 STOCKDB_V3_LIVE=1 且 STOCKDB_HOST/PORT 指向真实引擎时运行")
class LiveEngineTests(unittest.TestCase):
    """真实引擎交叉验证（真实数据不作为断言真值，只验证形状/单位/一致性）。"""

    def test_live_batch_real_engine(self):
        env = v3.get_kline_v3({
            "contract_version": "stockdb-market-v3",
            "codes": ["XSHG/equity/exchange/603516",
                      "XSHE/equity/exchange/000967",
                      "XSHE/equity/exchange/002328",
                      "XSHE/equity/exchange/002600",
                      "XSHE/etf/exchange/159915"],
            "frequency": "1d", "start": "20261020", "end": "20261023",
            "fq": "none", "limit": 0})
        if validate_contract is not None:
            validate_contract.validate(env, "KlineResponse")
        # 与逐码单独查询一致（C03 live 等价性）
        for key in env["query"]["codes"]:
            single = v3.get_kline_v3({
                "contract_version": "stockdb-market-v3", "code": key,
                "frequency": "1d", "start": "20261020", "end": "20261023",
                "fq": "none", "limit": 0})
            self.assertEqual(single["data"][key], env["data"][key],
                             f"batch/single mismatch for {key}")

    def test_live_list_and_calendar(self):
        latest = v3.engine_latest_trade_date()
        env = v3.get_stock_list_v3({
            "contract_version": "stockdb-market-v3",
            "asset_types": ["equity", "etf"],
            "exchanges": ["XSHG", "XSHE"], "as_of": latest})
        if validate_contract is not None:
            validate_contract.validate(env, "StockListResponse")
        self.assertGreater(env["total"], 5000)
        cal = v3.get_trading_days_v3({
            "contract_version": "stockdb-market-v3", "exchange": "XSHG",
            "start": "20261020", "end": "20261023"})
        if validate_contract is not None:
            validate_contract.validate(cal, "TradingDaysResponse")
        # 日历与引擎真实行情日期一致（10-20..10-23 均为交易日且有行情）
        for key in ("XSHG/equity/exchange/603516",):
            kenv = v3.get_kline_v3({
                "contract_version": "stockdb-market-v3", "code": key,
                "frequency": "1d", "start": "20261020", "end": "20261023",
                "fq": "none", "limit": 0})
            dates = {r["date"] for r in kenv["data"][key]}
            self.assertTrue(dates.issubset(set(cal["trading_days"])))


if __name__ == "__main__":
    unittest.main()
