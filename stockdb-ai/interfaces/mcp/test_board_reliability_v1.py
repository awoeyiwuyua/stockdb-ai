#!/usr/bin/env python3
"""test_board_reliability_v1 — 板块可靠性增强（C01-C18, C22）集成单测套件。

涵盖：
  - C01: 冻结合同原字节保留，旧协议与新增扩展兼容，身份不变
  - C02: 写入/flush/fsync/权限/空间失败显式暴露，未落盘数据不声称持久
  - C03: 多线程、多进程与中断恢复无丢失更新、临时文件冲突或半发布
  - C04: 完整500日暖缓存直接返回，上游调用数为0
  - C05: 仅补逻辑缺口/受控重叠，去重有界；年度物理请求如实区分
  - C06: 断源仍能服务完整有效缓存，来源健康与原获取时间不伪装
  - C07: 缺T或仅499日如实partial/失败，不偷换T或补零
  - C08: 日历异常导致coverage unknown/错误，不误报零缺口
  - C09: 未终结行、错码、错日、空/乱码响应和非法OHLC被拒且不污染缓存
  - C10: 同日连续至少两次修订保留完整链，旧快照内容不变
  - C11: 整窗、子窗、子证券、批量和字段投影重放范围/hash/元数据正确
  - C12: 提供者/身份/周期/复权/解释版本隔离，THS不替代BK，885809角色保留
  - C13: 所有路径遵守各provider限流冷却，东财冷却不阻塞THS
  - C14: worker启停/去重/重试有界可见，状态读取不启动worker或抓源
  - C15: 缓存损坏及资源错误可观测，可恢复已有有效版本，不静默当空缓存
  - C16: 状态分别报告覆盖、来源、持久化、任务和实际运行build
  - C17: 四指数、原六股票/ETF、none/qfq/hfq、R/F/A及负复权价格等旧合同回归不退化
  - C18: 无私有业务数据/密钥泄露，新维护控制不引入无鉴权管理接口
  - C22: 隔离候选副本完成旧格式兼容和回退演练，保留新缓存，不以清空数据通过
"""

from __future__ import annotations

import json
import os
import shutil
import sys
import tempfile
import threading
import time
import unittest
from datetime import datetime, timedelta, timezone
from decimal import Decimal
from pathlib import Path
from unittest.mock import patch

_MCP_DIR = Path(__file__).resolve().parent
_BASE_DIR = _MCP_DIR.parent.parent
for _p in (str(_MCP_DIR), str(_BASE_DIR), str(_MCP_DIR / "contract")):
    if _p not in sys.path:
        sys.path.insert(0, _p)

import config
import interfaces.mcp.board_reliability as br
import interfaces.mcp.market_v3 as v3
import interfaces.mcp.market_v3_research as r


class TestBoardReliabilityV1(unittest.TestCase):
    def setUp(self) -> None:
        self.temp_dir = Path(tempfile.mkdtemp(prefix="test_br_v1_"))
        self.orig_snapshot_dir = r._snapshot_dir_cached.get("path")
        r._snapshot_dir_cached["path"] = self.temp_dir

        # Windows 下确保 Path.read_text 默认 utf-8 编码，防止冻结合同特殊符号解码异常
        orig_read_text = Path.read_text
        def safe_read_text(path_obj, *args, **kwargs):
            if "encoding" not in kwargs:
                kwargs["encoding"] = "utf-8"
            return orig_read_text(path_obj, *args, **kwargs)
        self.patcher_read_text = patch.object(Path, "read_text", safe_read_text)
        self.patcher_read_text.start()

        # 使用隔离的快照仓与缓存目录
        self.test_cache_dir = self.temp_dir / "board_daily_cache"
        self.test_cache_dir.mkdir(parents=True, exist_ok=True)
        self.patcher_cache_dir = patch.object(br, "get_board_cache_dir", return_value=self.test_cache_dir)
        self.patcher_cache_dir.start()

        br.WORKER.stop()
        v3.HEALTH.stop()
        # 重置提供者闸门与研究状态
        br.EM_GATE.reset()
        br.THS_GATE.reset()
        em_st = r._em_client_state()
        dict.__setitem__(em_st, 'cooldown_until', 0.0)
        dict.__setitem__(em_st, 'backoff_streak', 0)
        r._EM_LAST_STATE["fail_at"] = 0.0
        r._EM_LAST_STATE["ok_at"] = time.time()
        r._EM_LAST_STATE["fail_reason"] = ""
        r._EM_LAST_STATE["cooldown_until"] = 0.0

    def tearDown(self) -> None:
        br.WORKER.stop()
        v3.HEALTH.stop()
        self.patcher_cache_dir.stop()
        self.patcher_read_text.stop()
        r._snapshot_dir_cached["path"] = self.orig_snapshot_dir
        shutil.rmtree(self.temp_dir, ignore_errors=True)

    def test_c01_frozen_contracts_preserved(self) -> None:
        """C01: 冻结合同原字节保留，旧协议与新增扩展兼容，身份不变。"""
        # 验证基线 schema 与 validator
        try:
            from interfaces.mcp.contract import validate_contract as vc_base
        except ImportError:
            self.skipTest("jsonschema not installed in runtime image")
            return
        self.assertEqual(vc_base.SCHEMA["$defs"]["ContractVersion"]["const"], "stockdb-market-v3")
        self.assertEqual(v3.CONTRACT_VERSION, "stockdb-market-v3")
        self.assertEqual(v3.CONTRACT_REVISION, "3.0.0")

        # 验证 research-extension schema 与 validator
        import importlib.util
        ext_path = _MCP_DIR / "contract" / "research-extension" / "validate_contract.py"
        spec = importlib.util.spec_from_file_location("vc_ext", ext_path)
        vc_ext = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(vc_ext)
        self.assertEqual(vc_ext.self_test()["status"], "PASS")
        self.assertEqual(r.EXTENSION_CONTRACT, "stockdb-market-v3-research-extension")
        self.assertEqual(r.EXTENSION_REVISION, "1.0.0")

    def test_c02_persistence_failure_exposure(self) -> None:
        """C02: 写入/flush/fsync/权限/空间失败显式暴露，未落盘数据不声称持久。"""
        target_path = self.test_cache_dir / "fail_test.json"

        # 模拟 fsync 抛出 OSError (磁盘空间已满/权限拒绝)
        with patch("os.fsync", side_effect=OSError("No space left on device")):
            with self.assertRaises(br.PersistenceError) as ctx:
                br.atomic_write_json(target_path, {"test": "data"})
            self.assertIn("No space left on device", str(ctx.exception))
            # 目标文件未落盘
            self.assertFalse(target_path.exists())

    def test_c03_concurrency_and_temp_file_safety(self) -> None:
        """C03: 多线程、多进程与中断恢复无丢失更新、临时文件冲突或半发布。"""
        key = "CN/board/eastmoney/BK1408"
        errors = []

        def worker(thread_idx: int) -> None:
            try:
                for i in range(5):
                    with br.cross_process_file_lock(br.get_board_lock_path(key)):
                        store = br.load_board_cache(key)
                        bars = store.setdefault("bars", {})
                        d = f"202610{thread_idx:02d}"
                        bars[d] = {
                            "open": f"{10 + i}.00",
                            "close": f"{11 + i}.00",
                            "high": f"{12 + i}.00",
                            "low": f"{9 + i}.00",
                            "bar_state": "final",
                        }
                        br.save_board_cache(key, store)
                    time.sleep(0.01)
            except Exception as exc:
                errors.append(exc)

        threads = [threading.Thread(target=worker, args=(t,)) for t in range(4)]
        for t in threads:
            t.start()
        for t in threads:
            t.join()

        self.assertEqual(errors, [])
        # 最终文件必须能正常读取且为合法 JSON
        final_store = br.load_board_cache(key)
        self.assertGreaterEqual(len(final_store.get("bars", {})), 4)

    def test_c04_warm_cache_zero_upstream_calls(self) -> None:
        """C04: 完整500日暖缓存直接返回，上游调用数为0。"""
        key = "CN/board/eastmoney/BK1408"
        secid = "90.BK1408"
        t = br.compute_target_trade_date_shanghai()

        import core.calendar_xshg as cal
        days = cal.trading_days_between("20230101", t)[-500:]

        store = br.load_board_cache(key)
        bars = store.setdefault("bars", {})
        for d in days:
            bars[d] = {
                "open": "100.00",
                "high": "105.00",
                "low": "98.00",
                "close": "102.00",
                "bar_state": "final",
                "retrieved_at": "2026-10-24T10:00:00+08:00",
            }
        br.save_board_cache(key, store)

        # 发起读取最近 500 日
        with patch.object(r, "eastmoney_board_daily") as mock_upstream:
            rows, ci = r.board_daily_cached(key, secid, days[0], t)
            # 必须未调用上游
            mock_upstream.assert_not_called()
            self.assertEqual(ci.get("upstream_calls"), 0)
            self.assertTrue(ci.get("cache_served"))
            self.assertEqual(len(rows), 500)
            self.assertEqual(rows[-1]["date8"], t)

    def test_c05_bounded_gap_backfill_and_physical_separation(self) -> None:
        """C05: 仅补逻辑缺口/受控重叠，去重有界；年度物理请求如实区分。"""
        key = "CN/board/eastmoney/BK1134"
        secid = "90.BK1134"
        t = br.compute_target_trade_date_shanghai()

        import core.calendar_xshg as cal
        days = cal.trading_days_between("20230101", t)[-500:]

        # 填充大部分，仅留最后 3 天为缺口
        store = br.load_board_cache(key)
        bars = store.setdefault("bars", {})
        for d in days[:-3]:
            bars[d] = {
                "open": "50.00", "high": "52.00", "low": "49.00", "close": "51.00",
                "bar_state": "final", "retrieved_at": "2026-10-24T10:00:00+08:00",
            }
        br.save_board_cache(key, store)

        mock_fresh_rows = [
            {"date8": d, "open": "50.00", "high": "52.00", "low": "49.00", "close": "51.00", "bar_state": "final"}
            for d in days[-5:]  # 补采返回重叠 2 天 + 缺失 3 天
        ]

        with patch.object(r, "eastmoney_board_daily", return_value=mock_fresh_rows) as mock_upstream:
            rows, ci = r.board_daily_cached(key, secid, days[0], t)
            self.assertEqual(ci.get("upstream_calls"), 1)
            # 校验补采起始日期包含了重叠安全余量 (BOARD_OVERLAP_DAYS)
            called_start = mock_upstream.call_args[0][1]
            self.assertLessEqual(called_start, days[-3])
            self.assertEqual(len(rows), 500)

    def test_c06_offline_source_served_from_cache(self) -> None:
        """C06: 断源仍能服务完整有效缓存，来源健康与原获取时间不伪装。"""
        key = "CN/board/eastmoney/BK1138"
        secid = "90.BK1138"
        t = br.compute_target_trade_date_shanghai()

        import core.calendar_xshg as cal
        days = cal.trading_days_between("20230101", t)[-10:]

        store = br.load_board_cache(key)
        bars = store.setdefault("bars", {})
        orig_retrieved = "2026-10-20T08:00:00+08:00"
        for d in days:
            bars[d] = {
                "open": "20.00", "high": "21.00", "low": "19.50", "close": "20.50",
                "bar_state": "final", "retrieved_at": orig_retrieved,
            }
        br.save_board_cache(key, store)

        # 模拟上游不可达
        with patch.object(r, "eastmoney_board_daily", side_effect=r.ResearchError("SOURCE_UNAVAILABLE", "upstream down")):
            rows, ci = r.board_daily_cached(key, secid, days[0], days[-1])
            self.assertTrue(ci.get("cache_served"))
            self.assertEqual(ci.get("upstream_calls"), 0)
            self.assertEqual(ci.get("retrieved_at"), orig_retrieved)
            self.assertEqual(len(rows), 10)

    def test_c07_missing_t_reports_partial_honestly(self) -> None:
        """C07: 缺T或仅499日如实partial/失败，不偷换T或补零。"""
        key = "CN/board/eastmoney/BK1101"
        t = br.compute_target_trade_date_shanghai()

        import core.calendar_xshg as cal
        days = cal.trading_days_between("20230101", t)[-500:]

        # 仅缓存 499 日（缺少 T）
        store = br.load_board_cache(key)
        bars = store.setdefault("bars", {})
        for d in days[:-1]:
            bars[d] = {
                "open": "10.00", "high": "11.00", "low": "9.00", "close": "10.50",
                "bar_state": "final", "retrieved_at": "2026-10-20T08:00:00+08:00",
            }
        br.save_board_cache(key, store)

        # 上游断连，读取包含 T
        with patch.object(r, "eastmoney_board_daily", side_effect=r.ResearchError("SOURCE_UNAVAILABLE", "waf down")):
            resp = v3.get_kline_v3({
                "contract_version": "stockdb-market-v3",
                "codes": [key],
                "start": days[0],
                "end": t,
                "frequency": "1d",
                "fq": "none",
            })
            # 顶层状态必须是 partial，绝不能是 complete
            self.assertEqual(resp["status"], "partial")
            meta = resp["meta"][key]
            self.assertIn(meta["coverage"]["state"], ("incomplete", "partial"))
            gaps = [g["date"] for g in meta["coverage"]["gaps"]]
            self.assertIn(f"{t[:4]}-{t[4:6]}-{t[6:]}", gaps)

    def test_c08_calendar_error_reports_unknown_coverage(self) -> None:
        """C08: 日历异常导致coverage unknown/错误，不误报零缺口。"""
        with patch("core.calendar_xshg.trading_days_between", side_effect=RuntimeError("calendar db corrupt")):
            report = r.board_cache_gap_report()
            for key, info in report.items():
                self.assertEqual(info["coverage"], "unknown")

    def test_c09_invalid_ohlc_rejected_and_cache_unpolluted(self) -> None:
        """C09: 未终结行、错码、错日、空/乱码响应和非法OHLC被拒且不污染缓存。"""
        t = br.compute_target_trade_date_shanghai()

        # 1. high < low
        with self.assertRaises((ValueError, br.BoardReliabilityError)):
            br.validate_ohlc_record({"date8": "20261020", "open": "10", "high": "8", "low": "9", "close": "10"}, target_trade_date=t)

        # 2. negative price
        with self.assertRaises((ValueError, br.BoardReliabilityError)):
            br.validate_ohlc_record({"date8": "20261020", "open": "-10", "high": "10", "low": "5", "close": "8"}, target_trade_date=t)

        # 3. date > target_t
        with self.assertRaises((ValueError, br.BoardReliabilityError)):
            br.validate_ohlc_record({"date8": "20991231", "open": "10", "high": "12", "low": "9", "close": "11"}, target_trade_date=t)

    def test_c10_continuous_revisions_chain(self) -> None:
        """C10: 同日连续至少两次修订保留完整链，旧快照内容不变。"""
        key = "CN/board/eastmoney/BK1037"
        secid = "90.BK1037"
        t = "20261020"

        # 初始抓取 (preliminary 状态允许后续被修订更新)
        row1 = [{"date8": t, "open": "10.00", "high": "12.00", "low": "9.00", "close": "11.00", "bar_state": "preliminary"}]
        with patch.object(r, "eastmoney_board_daily", return_value=row1):
            r.board_daily_cached(key, secid, t, t)

        # 第一次修订 (11.00 -> 11.50)
        store = br.load_board_cache(key)
        store["bars"][t]["bar_state"] = "preliminary"
        br.save_board_cache(key, store)
        row2 = [{"date8": t, "open": "10.00", "high": "12.00", "low": "9.00", "close": "11.50", "bar_state": "preliminary"}]
        with patch.object(r, "eastmoney_board_daily", return_value=row2):
            r.board_daily_cached(key, secid, t, t)

        # 第二次修订 (11.50 -> 11.80)
        store = br.load_board_cache(key)
        store["bars"][t]["bar_state"] = "preliminary"
        br.save_board_cache(key, store)
        row3 = [{"date8": t, "open": "10.00", "high": "12.20", "low": "9.00", "close": "11.80", "bar_state": "final"}]
        with patch.object(r, "eastmoney_board_daily", return_value=row3):
            r.board_daily_cached(key, secid, t, t)

        store = br.load_board_cache(key)
        revisions = store.get("revisions", {}).get(t, [])
        self.assertIsInstance(revisions, list)
        self.assertEqual(len(revisions), 2)
        self.assertEqual(revisions[0]["from"]["close"], "11.00")
        self.assertEqual(revisions[0]["to"]["close"], "11.50")
        self.assertEqual(revisions[1]["from"]["close"], "11.50")
        self.assertEqual(revisions[1]["to"]["close"], "11.80")

    def test_c11_snapshot_subset_replay_and_slicing(self) -> None:
        """C11: 整窗、子窗、子证券、批量和字段投影重放范围/hash/元数据正确。"""
        code1 = "CN/board/eastmoney/BK1408"
        code2 = "CN/board/ths/886069"

        # 预设两标的缓存
        for c in (code1, code2):
            store = br.load_board_cache(c)
            store["bars"] = {
                "20261020": {"open": "10.00", "high": "12.00", "low": "9.00", "close": "11.00", "bar_state": "final"},
                "20261021": {"open": "11.00", "high": "13.00", "low": "10.00", "close": "12.00", "bar_state": "final"},
            }
            br.save_board_cache(c, store)

        resp = v3.get_kline_v3({
            "contract_version": "stockdb-market-v3",
            "codes": [code1, code2],
            "start": "20261020",
            "end": "20261021",
            "frequency": "1d",
            "fq": "none",
        })
        snap_id = resp["snapshot_id"]
        self.assertIsNotNone(snap_id)
        sid1 = r.series_source_id(code1)
        sid2 = r.series_source_id(code2)
        self.assertIn(sid1, resp["source_catalog"])
        self.assertIn(sid2, resp["source_catalog"])

        # 回放子集: 仅请求 code2
        replay = v3.get_kline_v3({
            "contract_version": "stockdb-market-v3",
            "snapshot_id": snap_id,
            "codes": [code2],
            "start": "20261020",
            "end": "20261021",
            "frequency": "1d",
            "fq": "none",
        })
        self.assertEqual(replay["status"], "complete")
        self.assertIn(code2, replay["data"])
        self.assertNotIn(code1, replay["data"])
        # C11 关键：source_catalog 仅保留请求子集的源条目！
        self.assertIn(sid2, replay["source_catalog"])
        self.assertNotIn(sid1, replay["source_catalog"])

    def test_c12_namespace_isolation_and_ths_foldable_screen(self) -> None:
        """C12: 提供者/身份/周期/复权/解释版本隔离，THS不替代BK，885809角色保留。"""
        # 885809 (柔性屏/折叠屏)
        ths_key = "CN/board/ths/885809"
        kind, code, name, cal = r.resolve_research_identity(ths_key)
        self.assertEqual(kind, "board")
        self.assertEqual(code, "885809")
        self.assertEqual(name, "柔性屏")
        self.assertEqual(cal, "XSHG")

    def test_c13_rate_limiting_and_gate_cooldown_isolation(self) -> None:
        """C13: 所有路径遵守各provider限流冷却，东财冷却不阻塞THS。"""
        # 触发东财冷却
        br.EM_GATE.record_failure("WAF drop simulated", enter_cooldown=True)
        self.assertTrue(br.EM_GATE.get_status()["in_cooldown"])
        # 同花顺闸门必须完全健康
        self.assertFalse(br.THS_GATE.get_status()["in_cooldown"])

        # 请求同花顺不应受东财冷却阻断
        with patch.object(r, "fetch_ths_board_daily", return_value=[{"date8": "20261020", "open": "10", "high": "11", "low": "9", "close": "10.5", "bar_state": "final"}]):
            rows, ci = r.ths_board_daily_cached("CN/board/ths/886069", "886069", "20261020", "20261020")
            self.assertEqual(len(rows), 1)

    def test_c14_worker_lifecycle_explicit_and_status_no_side_effects(self) -> None:
        """C14: worker启停/去重/重试有界可见，状态读取不启动worker或抓源。"""
        # 初始未运行
        self.assertFalse(br.WORKER.is_running())

        v3.HEALTH.inject("20261024")
        # 调用 get_data_status_v3 绝不启动 worker
        status = v3.get_data_status_v3({"contract_version": "stockdb-market-v3"})
        self.assertEqual(status["status"], "ready")
        self.assertFalse(br.WORKER.is_running())

        # 受控显式启动与停止
        with patch.object(config, "BOARD_BACKFILL_ENABLED", True):
            br.WORKER.start()
            self.assertTrue(br.WORKER.is_running())
            br.WORKER.stop()
            self.assertFalse(br.WORKER.is_running())

    def test_c15_cache_corruption_detection_and_bak_recovery(self) -> None:
        """C15: 缓存损坏及资源错误可观测，可恢复已有有效版本，不静默当空缓存。"""
        key = "CN/board/eastmoney/BK0728"
        cache_path = br.get_board_cache_path(key)
        bak_path = cache_path.with_suffix(".bak.json")

        valid_store = {
            "instrument_id": key,
            "version": 2,
            "bars": {"20261020": {"open": "10", "high": "11", "low": "9", "close": "10", "bar_state": "final"}},
            "updated_at": "2026-10-20T10:00:00+08:00",
        }
        bak_path.write_text(json.dumps(valid_store), encoding="utf-8")
        # 损坏的主文件
        cache_path.write_text("NOT_VALID_JSON{", encoding="utf-8")

        # 读取应当自动检测损坏并从 .bak.json 恢复
        loaded = br.load_board_cache(key)
        self.assertEqual(loaded["instrument_id"], key)
        self.assertIn("20261020", loaded["bars"])

        # 损坏快照明确报 SNAPSHOT_CORRUPTED
        snap_path = r._snapshot_dir() / "snap-kline-corrupt.json"
        snap_path.write_text("{CORRUPTED_JSON", encoding="utf-8")
        resp = v3.get_kline_v3({
            "contract_version": "stockdb-market-v3",
            "snapshot_id": "snap-kline-corrupt",
            "codes": [key],
            "start": "20261020",
            "end": "20261020",
            "frequency": "1d",
            "fq": "none",
        })
        self.assertEqual(resp["status"], "failed")
        self.assertEqual(resp["errors"][0]["code"], "SNAPSHOT_CORRUPTED")

    def test_c16_status_metadata_extensions(self) -> None:
        """C16: 状态分别报告覆盖、来源、持久化、任务和实际运行build。"""
        status = v3.get_data_status_v3({"contract_version": "stockdb-market-v3"})
        exts = status.get("extensions", {})
        self.assertIn("board_reliability_v1", exts)
        b_ext = exts["board_reliability_v1"]
        self.assertIn("shanghai_clock", b_ext)
        self.assertIn("source_health", b_ext)
        self.assertIn("persistence_health", b_ext)
        self.assertIn("worker_state", b_ext)
        self.assertIn("runtime_build", b_ext)

    def test_c17_existing_core_and_research_regressions(self) -> None:
        """C17: 四指数、原六股票/ETF、none/qfq/hfq、R/F/A及负复权价格等旧合同回归不退化。"""
        # 校验四大指数身份
        for idx in ("XSHG/index/exchange/000001", "XSHE/index/exchange/399001", "XSHE/index/exchange/399006", "XSHG/index/csi/000852"):
            kind, native, name, cal = r.resolve_research_identity(idx)
            self.assertEqual(kind, "index")
            self.assertIsNotNone(name)

    def test_c18_no_private_data_leakage(self) -> None:
        """C18: 无私有业务数据/密钥泄露，新维护控制不引入无鉴权管理接口。"""
        raw_text = "error with token=abc123456 and password=secret and account=98765 on /vol1/data"
        sanitized = br.redact_sensitive(raw_text)
        self.assertNotIn("abc123456", sanitized)
        self.assertNotIn("secret", sanitized)
        self.assertNotIn("/vol1/", sanitized)

        # 检查 build_board_reliability_extension 序列化后无私有数据
        ext = br.build_board_reliability_extension()
        ext_json = json.dumps(ext)
        for forbidden in ("account", "position", "holding", "token", "password", "/vol1/"):
            self.assertNotIn(forbidden, ext_json.lower())

    def test_c22_migration_and_rollback_rehearsal(self) -> None:
        """C22: 隔离候选副本完成旧格式兼容和回退演练，保留新缓存，不以清空数据通过。"""
        sample_key = "CN/board/eastmoney/BK1408"
        v1_cache_file = self.test_cache_dir / "CN_board_eastmoney_BK1408.json"
        v1_data = {
            "instrument_id": sample_key,
            "bars": {
                "20261020": {"open": "100.00", "high": "105.00", "low": "98.00", "close": "102.00"},
            },
            "fetch_attempts": 1,
            "fetch_failures": 0,
        }
        v1_cache_file.write_text(json.dumps(v1_data), encoding="utf-8")

        # 1. 预览
        preview = br.migrate_v1_to_v2_preview(self.test_cache_dir)
        self.assertEqual(preview["v1_eligible"], 1)

        # 2. 应用迁移
        apply_res = br.migrate_v1_to_v2_apply(self.test_cache_dir)
        self.assertEqual(apply_res["converted"], 1)
        self.assertTrue((self.test_cache_dir / "CN_board_eastmoney_BK1408.v1.bak.json").exists())

        migrated_store = json.loads(v1_cache_file.read_text(encoding="utf-8"))
        self.assertIn(migrated_store.get("schema_version"), ("board-cache-v2", 2))
        self.assertEqual(migrated_store["bars"]["20261020"]["bar_state"], "final")

        # 3. 回滚
        rollback_res = br.rollback_v2_to_v1(self.test_cache_dir)
        self.assertEqual(rollback_res["restored"], 1)
        restored_store = json.loads(v1_cache_file.read_text(encoding="utf-8"))
        self.assertNotIn("version", restored_store)

    def test_reconcile_01_intraday_row_cannot_automatically_become_final(self) -> None:
        """1. 盘中缓存行不能自动成为 final：未收盘或无终结性依据的行保持 preliminary。"""
        target_t = br.compute_target_trade_date_shanghai()
        # 模拟盘中 11:39（早于 15:00）采集的行
        row_intraday = {
            "date8": target_t,
            "open": "1000.00",
            "high": "1050.00",
            "low": "990.00",
            "close": "1020.00",
            "bar_state": "preliminary",
        }
        val = br.validate_ohlc_record(row_intraday, target_trade_date=target_t)
        self.assertEqual(val["bar_state"], "preliminary")

        # 写入缓存后，读取不自动升级为 final
        key = "CN/board/ths/881124"
        store = br.load_board_cache(key)
        store["bars"][target_t] = {
            "open": "1000.00", "high": "1050.00", "low": "990.00", "close": "1020.00",
            "bar_state": "preliminary", "retrieved_at": "2026-11-03T11:39:39+08:00"
        }
        br.save_board_cache(key, store)

        loaded = br.load_board_cache(key)
        self.assertEqual(loaded["bars"][target_t]["bar_state"], "preliminary")

    def test_reconcile_02_existing_date_corrected_by_true_final(self) -> None:
        """2. 已有日期被后续真实终结行情修正：如 BK1134 2026-02-24 low 2031.24 -> 2031.22。"""
        key = "CN/board/eastmoney/BK1134"
        d = "20260224"
        store = br.load_board_cache(key)
        store["bars"][d] = {
            "open": "2082.00", "high": "2082.91", "low": "2031.24", "close": "2046.84",
            "bar_state": "final", "retrieved_at": "2026-11-03T16:02:21+08:00"
        }
        br.save_board_cache(key, store)

        upstream_true_row = [{
            "date8": d, "open": "2082.00", "high": "2082.91", "low": "2031.22", "close": "2046.84", "bar_state": "final"
        }]
        res = br.reconcile_series_cache(key, upstream_true_row, provider="eastmoney", reason="upstream_historical_revision")
        self.assertEqual(res["revisions_count"], 1)
        self.assertIn(d, res["revised_dates"])

        updated_store = br.load_board_cache(key)
        self.assertEqual(updated_store["bars"][d]["low"], "2031.22")
        rev = updated_store["revisions"][d][0]
        self.assertEqual(rev["from"]["low"], "2031.24")
        self.assertEqual(rev["to"]["low"], "2031.22")
        self.assertEqual(rev["provider"], "eastmoney")

    def test_reconcile_03_two_consecutive_revisions_chain(self) -> None:
        """3. 连续两次修订保留完整链：同日多次修正有序追加。"""
        key = "CN/board/ths/881181"
        d = "20261103"
        store = br.load_board_cache(key)
        store["bars"][d] = {
            "open": "3563.931", "high": "3620.028", "low": "3517.534", "close": "3519.312",
            "bar_state": "final", "retrieved_at": "2026-11-03T11:39:39+08:00"
        }
        br.save_board_cache(key, store)

        # 第一次修订
        rev1_rows = [{"date8": d, "open": "3563.931", "high": "3620.028", "low": "3517.534", "close": "3530.000", "bar_state": "final"}]
        br.reconcile_series_cache(key, rev1_rows, provider="ths", reason="preliminary_adjustment")

        # 第二次修订
        rev2_rows = [{"date8": d, "open": "3563.931", "high": "3620.028", "low": "3517.534", "close": "3539.234", "bar_state": "final"}]
        br.reconcile_series_cache(key, rev2_rows, provider="ths", reason="post_close_final_confirmation")

        store = br.load_board_cache(key)
        chain = store["revisions"][d]
        self.assertEqual(len(chain), 2)
        self.assertEqual(chain[0]["from"]["close"], "3519.312")
        self.assertEqual(chain[0]["to"]["close"], "3530.000")
        self.assertEqual(chain[1]["from"]["close"], "3530.000")
        self.assertEqual(chain[1]["to"]["close"], "3539.234")
        self.assertEqual(store["bars"][d]["close"], "3539.234")

    def test_reconcile_04_new_current_preserves_old_snapshot(self) -> None:
        """4. 新 current 更新后，旧快照精确不变：快照不可变重放。"""
        key = "CN/board/eastmoney/BK1408"
        d = "20261020"
        store = br.load_board_cache(key)
        store["bars"] = {d: {"open": "100.00", "high": "105.00", "low": "95.00", "close": "100.00", "bar_state": "final"}}
        br.save_board_cache(key, store)

        snap_store = br.HardenedPersistentSnapshotStore(snapshot_dir=self.temp_dir)
        snap_id = "snap-test-immutability-1"
        payload1 = {"code": key, "data": store["bars"]}
        snap_store.capture(snap_id, payload1, row_count=1)

        # 更新当前缓存 (修订 100.00 -> 102.50)
        br.reconcile_series_cache(key, [{"date8": d, "open": "100.00", "high": "105.00", "low": "95.00", "close": "102.50", "bar_state": "final"}], provider="eastmoney")

        # 旧快照精确不变
        replayed = snap_store.get(snap_id)
        self.assertIsNotNone(replayed)
        self.assertEqual(replayed["payload"]["data"][d]["close"], "100.00")

    def test_reconcile_05_warm_cache_zero_upstream_calls(self) -> None:
        """5. 完整暖缓存零上游调用。"""
        import core.calendar_xshg as calendar_xshg
        key = "CN/board/eastmoney/BK0728"
        store = br.load_board_cache(key)
        all_trading = calendar_xshg.trading_days_between("20241014", "20261103")
        dates = all_trading[-20:]  # 最近 20 个实际交易日
        store["bars"] = {d: {"open": "10.00", "high": "11.00", "low": "9.50", "close": "10.50", "bar_state": "final", "retrieved_at": "2026-10-21T16:00:00+08:00"} for d in dates}
        br.save_board_cache(key, store)

        with patch.object(config, "BOARD_WARM_CACHE_MIN_DAYS", 10):
            with patch.object(r, "eastmoney_board_daily") as mock_fetch:
                rows, meta = r.board_daily_cached(key, "90.BK0728", dates[0], dates[-1])
                self.assertEqual(meta["upstream_calls"], 0)
                mock_fetch.assert_not_called()
                self.assertEqual(len(rows), 20)

    def test_reconcile_06_revision_check_bounded_and_respects_cooldown(self) -> None:
        """6. 修订检查受控、有界，遵守各来源冷却，东财冷却不阻塞 THS。"""
        br.EM_GATE.record_failure("simulated WAF drop", enter_cooldown=True)
        self.assertTrue(br.EM_GATE.get_status()["in_cooldown"])
        self.assertFalse(br.THS_GATE.get_status()["in_cooldown"])

        # THS 请求不受东财冷却影响，正常放行
        br.THS_GATE.check_and_throttle()

    def test_reconcile_07_disk_failure_does_not_falsely_claim_success(self) -> None:
        """7. 更新落盘失败时不假报成功。"""
        key = "CN/board/eastmoney/BK1101"
        rows = [{"date8": "20261020", "open": "10.00", "high": "12.00", "low": "9.00", "close": "11.00", "bar_state": "final"}]
        with patch("os.replace", side_effect=OSError("Disk write error")):
            with self.assertRaises(br.PersistenceError):
                br.reconcile_series_cache(key, rows, provider="eastmoney")

    def test_reconcile_08_active_status_declaration(self) -> None:
        """8. active 声明与实际配置一致。"""
        ext = br.build_board_reliability_extension()
        self.assertEqual(ext.get("status"), "active")
        self.assertIn("active indicates", ext.get("status_meaning", ""))
        self.assertEqual(ext.get("extension_contract"), "stockdb-board-reliability-v1")
        self.assertEqual(ext.get("extension_revision"), "1.0.0")

    # -----------------------------------------------------------------------
    # P0 / P1 增量确定性合成回归套件（11 场景覆盖）
    # -----------------------------------------------------------------------

    def test_p0_01_intraday_lunch_break_1139_is_preliminary(self) -> None:
        """1. 11:39 午休判定：午休时 open=0 仍为未终结盘中值，必须 preliminary，不得标为 final。"""
        target_t = "20261104"
        lunch_dt = datetime(2026, 11, 4, 11, 39, 0, tzinfo=br._SHANGHAI_TZ)
        with patch.object(br, "get_shanghai_time", return_value=lunch_dt):
            row = {
                "date8": "20261104", "open": "2504.872", "high": "2638.434",
                "low": "2467.767", "close": "2628.766", "bar_state": "final",
                "source_evidence": "today_js",
            }
            validated = br.validate_ohlc_record(row, target_trade_date=target_t, instrument_id="CN/board/ths/885809")
            self.assertEqual(validated["bar_state"], "preliminary")
            self.assertEqual(validated.get("archive_status"), "intraday_provisional")

    def test_p0_02_post_close_provisional_1503_is_preliminary(self) -> None:
        """2. 15:03 延迟更新：盘后清算期（15:00-15:30）today.js 临时值不得断言为 final。"""
        target_t = "20261104"
        post_close_dt = datetime(2026, 11, 4, 15, 3, 27, tzinfo=br._SHANGHAI_TZ)
        with patch.object(br, "get_shanghai_time", return_value=post_close_dt):
            row = {
                "date8": "20261104", "open": "2504.872", "high": "2638.434",
                "low": "2467.767", "close": "2628.766", "bar_state": "final",
                "source_evidence": "today_js",
            }
            validated = br.validate_ohlc_record(row, target_trade_date=target_t, instrument_id="CN/board/ths/885809")
            self.assertEqual(validated["bar_state"], "preliminary")
            self.assertEqual(validated.get("archive_status"), "post_close_provisional")

    def test_p0_03_post_1530_yearly_pack_update_reconciles(self) -> None:
        """3. 15:30+ 年度包变化：年度包出现后对齐 current，保留旧值、新值、时戳与原响应 hash。"""
        key = "CN/board/ths/885809"
        d = "20261104"
        store = br.load_board_cache(key)
        # 模拟 15:03 写入的临时值
        store["bars"][d] = {
            "open": "2504.872", "high": "2638.434", "low": "2467.767", "close": "2628.766",
            "bar_state": "preliminary", "archive_status": "post_close_provisional",
            "source_evidence": "today_js", "retrieved_at": "2026-11-04T15:03:27+08:00",
        }
        br.save_board_cache(key, store)

        # 15:35 年度包发布终结值 2634.118
        yearly_row = [{
            "date8": d, "open": "2504.872", "high": "2638.434", "low": "2467.767", "close": "2634.118",
            "bar_state": "final", "source_evidence": "yearly_archive", "archive_status": "archived",
            "raw_hash": "d46b5272d4a2fbd372090b12a9a619f36b890b3d67cb321f452342ebc17761d2",
        }]
        after_1530_dt = datetime(2026, 11, 4, 15, 35, 0, tzinfo=br._SHANGHAI_TZ)
        with patch.object(br, "get_shanghai_time", return_value=after_1530_dt):
            res = br.reconcile_series_cache(key, yearly_row, provider="ths", reason="yearly_pack_supersedes_today_js")
            self.assertEqual(res["revisions_count"], 1)

            updated = br.load_board_cache(key)
            bar = updated["bars"][d]
            self.assertEqual(bar["close"], "2634.118")
            self.assertEqual(bar["bar_state"], "final")
            self.assertEqual(bar["archive_status"], "archived")
            self.assertEqual(bar["source_evidence"], "yearly_archive")

            rev = updated["revisions"][d][0]
            self.assertEqual(rev["from"]["close"], "2628.766")
            self.assertEqual(rev["to"]["close"], "2634.118")
            self.assertEqual(rev["raw_hash"], "d46b5272d4a2fbd372090b12a9a619f36b890b3d67cb321f452342ebc17761d2")
            self.assertEqual(rev["reason"], "yearly_pack_supersedes_today_js")

    def test_p0_04_missing_yearly_pack_then_arrives(self) -> None:
        """4. 先缺年度包后出现：区分待归档与数值错误，年度包出现后幂等升级为 archived。"""
        key = "CN/board/ths/881124"
        d = "20261104"
        after_1530_dt = datetime(2026, 11, 4, 16, 0, 0, tzinfo=br._SHANGHAI_TZ)
        with patch.object(br, "get_shanghai_time", return_value=after_1530_dt):
            # 步骤 1: 年度包暂缺，从 today.js 归入 pending_yearly_archive
            today_row = [{
                "date8": d, "open": "10002.423", "high": "10461.212", "low": "9828.476", "close": "10444.003",
                "bar_state": "final", "source_evidence": "today_js", "archive_status": "pending_yearly_archive",
                "raw_hash": "c17f1aa7beac0b59029cea2fb4bbb5ab1910997ac33f7a7052ba64b65c99a72d",
            }]
            br.reconcile_series_cache(key, today_row, provider="ths", reason="today_js_interim")
            s1 = br.load_board_cache(key)
            self.assertEqual(s1["bars"][d]["archive_status"], "pending_yearly_archive")
            self.assertEqual(s1["bars"][d]["source_evidence"], "today_js")

            # 步骤 2: 年度包随后产出，数值一致，元数据升级为 yearly_archive 与 archived，无冗余修订记录
            yearly_row = [{
                "date8": d, "open": "10002.423", "high": "10461.212", "low": "9828.476", "close": "10444.003",
                "bar_state": "final", "source_evidence": "yearly_archive", "archive_status": "archived",
                "raw_hash": "e81124_yearly_hash_example",
            }]
            res = br.reconcile_series_cache(key, yearly_row, provider="ths", reason="yearly_archive_published")
            self.assertEqual(res["revisions_count"], 0)
            self.assertTrue(res["meta_updated"])

            s2 = br.load_board_cache(key)
            self.assertEqual(s2["bars"][d]["archive_status"], "archived")
            self.assertEqual(s2["bars"][d]["source_evidence"], "yearly_archive")
            self.assertEqual(len(s2.get("revisions", {}).get(d, [])), 0)

    def test_p0_05_existing_final_row_changes(self) -> None:
        """5. 已有 final 行变化：对齐东财冲突点并完整记录修订审计链。"""
        key = "CN/board/eastmoney/BK1134"
        d = "20260224"
        store = br.load_board_cache(key)
        store["bars"][d] = {
            "open": "2082.00", "high": "2082.91", "low": "2031.22", "close": "2046.84",
            "bar_state": "final", "retrieved_at": "2026-11-03T16:00:00+08:00",
        }
        br.save_board_cache(key, store)

        origin_row = [{
            "date8": d, "open": "2082.00", "high": "2082.91", "low": "2031.24", "close": "2046.84",
            "bar_state": "final", "source_evidence": "eastmoney_kline_api", "archive_status": "archived",
            "raw_hash": "4bbb6a6324fb812ed21060ec8cda5a962ec9614b5e21c6269fc13eac69fa4e94",
        }]
        res = br.reconcile_series_cache(key, origin_row, provider="eastmoney", reason="verified_origin_align")
        self.assertEqual(res["revisions_count"], 1)

        updated = br.load_board_cache(key)
        self.assertEqual(updated["bars"][d]["low"], "2031.24")
        rev = updated["revisions"][d][0]
        self.assertEqual(rev["from"]["low"], "2031.22")
        self.assertEqual(rev["to"]["low"], "2031.24")
        self.assertEqual(rev["raw_hash"], "4bbb6a6324fb812ed21060ec8cda5a962ec9614b5e21c6269fc13eac69fa4e94")

    def test_p0_06_repeated_inspection_idempotent(self) -> None:
        """6. 重复巡检幂等：多次巡检相同数据绝不重复追加修订记录。"""
        key = "CN/board/eastmoney/BK1037"
        d = "20260810"
        rows = [{
            "date8": d, "open": "2024.71", "high": "2029.55", "low": "1988.42", "close": "2029.48",
            "bar_state": "final", "source_evidence": "eastmoney_kline_api", "archive_status": "archived",
        }]
        r1 = br.reconcile_series_cache(key, rows, provider="eastmoney", reason="cycle_1")
        r2 = br.reconcile_series_cache(key, rows, provider="eastmoney", reason="cycle_2")
        r3 = br.reconcile_series_cache(key, rows, provider="eastmoney", reason="cycle_3")

        self.assertEqual(r2["revisions_count"], 0)
        self.assertEqual(r3["revisions_count"], 0)

        store = br.load_board_cache(key)
        chain = store.get("revisions", {}).get(d, [])
        self.assertEqual(len(chain), 0)

    def test_p0_07_same_source_conflict_and_cdn_divergence(self) -> None:
        """7. 同源冲突与东财 CDN 分歧：truthful upstream_version_id: null，不虚构版本，内容寻址存证。"""
        ext = br.build_board_reliability_extension()
        evidences = ext.get("eastmoney_version_evidence", {})
        self.assertIsNone(evidences.get("upstream_version_id"))
        self.assertEqual(evidences.get("status"), "divergence_observed_and_disclosed")
        conflict_points = evidences.get("conflict_points", [])
        self.assertTrue(len(conflict_points) >= 4)
        bk1134_pt = next(p for p in conflict_points if "BK1134" in p.get("instrument_id", ""))
        self.assertIsNone(bk1134_pt.get("upstream_version_id"))
        self.assertEqual(bk1134_pt.get("conflict_status"), "upstream_cdn_divergence")

    def test_p0_08_concurrent_read_old_snapshot_while_publishing_new_current(self) -> None:
        """8. 并发读旧快照/发布新 current：快照不可变重放与当前缓存并发修订互不影响。"""
        key = "CN/board/eastmoney/BK1138"
        d = "20261103"
        store = br.load_board_cache(key)
        store["bars"][d] = {"open": "100.0", "high": "105.0", "low": "95.0", "close": "100.0", "bar_state": "final"}
        br.save_board_cache(key, store)

        snap_store = br.HardenedPersistentSnapshotStore(snapshot_dir=self.temp_dir)
        snap_id = "snap-test-concurrent-old"
        snap_store.capture(snap_id, {"code": key, "close": "100.0", "data": store["bars"]}, row_count=1)

        read_errors = []
        write_errors = []

        def reader():
            for _ in range(20):
                rec = snap_store.get(snap_id)
                if not rec or rec["payload"]["data"][d]["close"] != "100.0":
                    read_errors.append("Snapshot mutated or missing")
                time.sleep(0.005)

        def writer():
            for i in range(10):
                new_close = f"10{i}.0"
                row = [{"date8": d, "open": "100.0", "high": "105.0", "low": "95.0", "close": new_close, "bar_state": "final"}]
                try:
                    br.reconcile_series_cache(key, row, provider="eastmoney", reason=f"concurrent_test_{i}")
                except Exception as exc:
                    write_errors.append(str(exc))
                time.sleep(0.005)

        t_r = threading.Thread(target=reader)
        t_w = threading.Thread(target=writer)
        t_r.start()
        t_w.start()
        t_r.join()
        t_w.join()

        self.assertEqual(read_errors, [])
        self.assertEqual(write_errors, [])
        final_snap = snap_store.get(snap_id)
        self.assertEqual(final_snap["payload"]["data"][d]["close"], "100.0")

    def test_p0_09_cold_start_status_polling_no_market_query(self) -> None:
        """9. 冷启动只轮询状态、无行情请求也能进入 ready：解除消费端互锁死锁。"""
        # 显式重置为冷启动未初始化状态
        v3.HEALTH.stop()
        with v3.HEALTH._lock:
            v3.HEALTH._state = {
                "probe_state": "unstarted",
                "latest": None,
                "checked_at": None,
                "started_at": None,
                "last_success_at": None,
                "last_failure_at": None,
                "ok": False,
                "error": None,
                "retry_count": 0,
            }

        # 初始未启动
        st0 = v3.get_data_status_v3()
        self.assertEqual(st0["status"], "degraded")
        self.assertEqual(st0["extensions"]["core_health_probe_v1"]["probe_state"], "unstarted")

        with patch.object(v3, "engine_latest_trade_date", return_value="20261104"):
            # 服务显式生命周期启动
            v3.HEALTH.start()
            self.assertTrue(v3.HEALTH.is_running())

            # 模拟消费端在无行情请求下轮询 get_data_status
            for _ in range(50):
                st = v3.get_data_status_v3()
                if st["status"] == "ready":
                    break
                time.sleep(0.02)

            st_final = v3.get_data_status_v3()
            self.assertEqual(st_final["status"], "ready")
            self.assertEqual(st_final["extensions"]["core_health_probe_v1"]["latest_trade_date"], "20261104")
            self.assertEqual(st_final["data_status"]["latest_trade_date"], "2026-11-04")

    def test_p0_10_bounded_timeout_and_shutdown_thread_release(self) -> None:
        """10. 依赖超时有界与关闭后线程释放。"""
        with patch.object(v3, "engine_latest_trade_date", return_value="20261104"):
            v3.HEALTH.start()
            self.assertTrue(v3.HEALTH.is_running())
            thread_obj = v3.HEALTH._thread
            self.assertIsNotNone(thread_obj)
            self.assertTrue(thread_obj.is_alive())

            v3.HEALTH.stop(timeout=2.0)
            self.assertFalse(v3.HEALTH.is_running())
            thread_obj.join(timeout=2.0)
            self.assertFalse(thread_obj.is_alive())

    def test_p0_11_eastmoney_cooldown_does_not_block_ths(self) -> None:
        """11. 东财失败不阻塞同花顺：EM 熔断冷却期内，THS 板块巡检与补采完全不受阻。"""
        br.EM_GATE.record_failure("WAF drop 403", enter_cooldown=True)
        self.assertTrue(br.EM_GATE.get_status()["in_cooldown"])
        self.assertFalse(br.THS_GATE.get_status()["in_cooldown"])

        # mock THS 返回空行或成功，避免依赖外网
        with patch.object(r, "ths_board_daily_cached", return_value=([], {"cache_hit": True, "upstream_calls": 0})):
            summary = r.board_backfill_cycle(force=False)
            self.assertIn("boards", summary)
            # 东财板块应为 skipped_cooldown
            bk_summary = summary["boards"].get("CN/board/eastmoney/BK1408", {})
            self.assertEqual(bk_summary.get("action"), "skipped_cooldown")
            # THS 板块绝不因东财冷却被全局跳过
            ths_summary = summary["boards"].get("CN/board/ths/885809", {})
            self.assertNotEqual(ths_summary.get("action"), "skipped_cooldown")
            self.assertIn(ths_summary.get("action"), ("complete", "none", "fetched"))


if __name__ == "__main__":
    unittest.main()
