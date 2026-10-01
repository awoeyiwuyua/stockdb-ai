"""test_hk_tasks — 港股自动同步编排测试（0.12.0 H1）。

覆盖：_hk_due 触发判定（纯函数）/ watchlist 清单解析 / hk_run_sync 成功·失败·
未装配（状态落盘 + 日检留痕 + 运行态）/ hk_freshness 新鲜度评估（日历替身）/
hk_freshness_alert 条件式投影（滞后才挂、追平即撤、交易日观察线）。
依赖注入直接对 services.hk_tasks 模块属性赋值（warehouse/auction 同款约定）。
"""
from __future__ import annotations

import json
import pathlib
import tempfile
import unittest
from datetime import datetime
from unittest import mock

import config


class _FakeHKCalendar:
    """calendar_market.HK 替身：固定 session 序列（8 位串升序）。

    2026 国庆样本：09-28/29/30 交易、10-01 休市、10-02 交易（A股休/港股开）、
    10-03/04 周末、10-05/06 交易。
    """

    SESSIONS = ["20260928", "20260929", "20260930", "20261002", "20261005", "20261006"]

    def _s(self, d) -> str:
        return d.strftime("%Y%m%d") if hasattr(d, "strftime") else str(d)

    def is_trading_day(self, d) -> bool:
        return self._s(d) in self.SESSIONS

    def nearest_trading_day(self, d):
        s = self._s(d)
        cand = [x for x in self.SESSIONS if x <= s]
        return cand[-1] if cand else None

    def trading_days_between(self, start, end):
        return [x for x in self.SESSIONS if start <= x <= end]


class _FakeAlerts:
    """告警中心替身：add/resolve 全记录（不落盘）。"""

    def __init__(self):
        self.added: list = []
        self.resolved: list = []

    def add(self, level, source, message):
        self.added.append((level, source, message))
        return True

    def resolve(self, source, prefix):
        self.resolved.append((source, prefix))
        return 1


class _HkTasksBase(unittest.TestCase):
    """公共底座：tmp DATA_DIR + 注入点保存/还原 + 告警替身。"""

    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.root = pathlib.Path(self._tmp.name)
        import services.hk_tasks as ht
        self.ht = ht
        self._saved = {k: getattr(ht, k) for k in
                       ("sync_fn", "calendar", "_get_alerts", "_records_append")}
        self._saved_fired = dict(ht._hk_fired)
        self._saved_run_state = dict(ht._hk_run_state)
        ht._hk_fired = {}
        ht._hk_run_state = {"running": False, "started": None, "finished": None, "result": None}
        self.alerts = _FakeAlerts()
        ht._get_alerts = lambda: self.alerts
        self.records: list = []
        ht._records_append = lambda rec: self.records.append(rec)
        self._cm = mock.patch.multiple(config, DATA_DIR=self.root)
        self._cm.start()

    def tearDown(self):
        self._cm.stop()
        for k, v in self._saved.items():
            setattr(self.ht, k, v)
        self.ht._hk_fired = self._saved_fired
        self.ht._hk_run_state = self._saved_run_state
        self._tmp.cleanup()


class HkDueTest(_HkTasksBase):
    """_hk_due 触发判定（纯函数，无 mock 时钟）。"""

    def test_before_time_not_due(self):
        guard = {"fired": False, "attempts": 0, "next_retry": 0.0}
        self.assertFalse(self.ht._hk_due("16:14", guard))

    def test_after_time_due(self):
        guard = {"fired": False, "attempts": 0, "next_retry": 0.0}
        self.assertTrue(self.ht._hk_due("16:15", guard))
        self.assertTrue(self.ht._hk_due("23:59", guard))

    def test_fired_not_due(self):
        guard = {"fired": True, "attempts": 1, "next_retry": 0.0}
        self.assertFalse(self.ht._hk_due("18:00", guard))

    def test_retry_wait_blocks(self):
        guard = {"fired": False, "attempts": 1, "next_retry": self.ht.time.time() + 600}
        self.assertFalse(self.ht._hk_due("17:00", guard))

    def test_retry_wait_expired_due(self):
        guard = {"fired": False, "attempts": 3, "next_retry": self.ht.time.time() - 1}
        self.assertTrue(self.ht._hk_due("17:00", guard))


class WatchlistTest(_HkTasksBase):
    """HK_SYNC_CODES 解析：去 hk 前缀 / 大小写 / 补零 / 空段跳过。"""

    def test_parse(self):
        with mock.patch.object(config, "HK_SYNC_CODES", "hk700, 00100 ,HK00100"):
            self.assertEqual(self.ht.watchlist(), ["00700", "00100", "00100"])

    def test_empty(self):
        with mock.patch.object(config, "HK_SYNC_CODES", ""):
            self.assertEqual(self.ht.watchlist(), [])


class HkRunSyncTest(_HkTasksBase):
    """hk_run_sync：任务本体（状态落盘 + 日检 + 运行态），不投告警。"""

    def test_sync_fn_not_wired(self):
        self.ht.sync_fn = None
        res = self.ht.hk_run_sync()
        self.assertFalse(res["ok"])
        self.assertIn("未装配", res["reason"])

    def test_success_writes_state_and_records(self):
        self.ht.sync_fn = lambda codes, years: {
            "00700": {"ok": True, "bars": 320, "latest": 20260930},
            "00100": {"ok": True, "bars": 179, "latest": 20260930},
        }
        res = self.ht.hk_run_sync(["00700", "00100"], years=2)
        self.assertTrue(res["ok"])
        self.assertEqual(res["failed"], [])
        # 状态落盘：latest 进 state（diag/新鲜度告警的数据源）
        state = self.ht.hk_state()
        self.assertIsNotNone(state)
        self.assertEqual(state["latest"], {"00700": 20260930, "00100": 20260930})
        self.assertEqual(state["date"], datetime.now().strftime("%Y%m%d"))
        # 日检留痕 + 运行态收口
        self.assertEqual(len(self.records), 1)
        self.assertTrue(self.records[0]["ok"])
        self.assertEqual(self.records[0]["task"], "hk_sync")
        self.assertFalse(self.ht._hk_run_state["running"])
        self.assertTrue(self.ht._hk_run_state["result"]["ok"])
        # 任务本体不投告警（调度层统一处理）
        self.assertEqual(self.alerts.added, [])

    def test_partial_failure_recorded(self):
        self.ht.sync_fn = lambda codes, years: {
            "00700": {"ok": True, "bars": 320, "latest": 20260930},
            "00100": {"ok": False, "error": "无数据"},
        }
        res = self.ht.hk_run_sync(["00700", "00100"])
        self.assertFalse(res["ok"])
        self.assertEqual(res["failed"], ["00100"])
        state = self.ht.hk_state()
        self.assertEqual(state["latest"], {"00700": 20260930})  # 只记成功码
        self.assertFalse(self.records[0]["ok"])

    def test_sync_fn_raise_degrades(self):
        def _boom(codes, years):
            raise RuntimeError("engine down")
        self.ht.sync_fn = _boom
        res = self.ht.hk_run_sync(["00700"])
        self.assertFalse(res["ok"])
        self.assertEqual(res["failed"], ["00700"])
        self.assertIn("engine down", res["results"]["00700"]["error"])


class HkFreshnessTest(_HkTasksBase):
    """hk_freshness：latest 落后期望 session 的港股交易日数（日历替身）。"""

    def _set_state(self, latest):
        self.ht._write_state({"at": "2026-10-01T10:00:00", "date": "20261001",
                              "years": 2, "latest": latest, "results": {}})

    def test_no_calendar_permissive(self):
        self.ht.calendar = None
        self._set_state({"00700": "20260101"})
        fresh = self.ht.hk_freshness()
        self.assertIsNone(fresh["lag"])
        self.assertFalse(fresh["stale"])

    def test_fresh_when_latest_is_expected(self):
        self.ht.calendar = _FakeHKCalendar()
        self._set_state({"00700": "20260930", "00100": "20260930"})
        # 10-01 休市日：期望 session = 09-30 → 无滞后
        fresh = self.ht.hk_freshness(today=datetime(2026, 10, 1, 10, 0))
        self.assertEqual(fresh["expected"], "20260930")
        self.assertEqual(fresh["lag"], 0)
        self.assertFalse(fresh["stale"])

    def test_stale_counts_hk_trading_days(self):
        self.ht.calendar = _FakeHKCalendar()
        # 10-02 交易日期望 20261002，数据停在 09-30：跨 10-01（休市）与 10-02 → 滞后 1
        self._set_state({"00700": "20260930", "00100": "20260930"})
        fresh = self.ht.hk_freshness(today=datetime(2026, 10, 2, 16, 0))
        self.assertEqual(fresh["lag"], 1)
        self.assertTrue(fresh["stale"])

    def test_no_state_permissive(self):
        self.ht.calendar = _FakeHKCalendar()
        fresh = self.ht.hk_freshness(today=datetime(2026, 10, 2, 16, 0))
        self.assertIsNone(fresh["lag"])
        self.assertFalse(fresh["stale"])


class HkFreshnessAlertTest(_HkTasksBase):
    """hk_freshness_alert：条件式投影——滞后且过观察线才挂，追平/未装配即撤。"""

    def _set_state(self, latest):
        self.ht._write_state({"at": "2026-10-01T10:00:00", "date": "20261001",
                              "years": 2, "latest": latest, "results": {}})

    def test_stale_on_holiday_alerts_immediately(self):
        """休市日上一 session 缺失即事实（无观察线等待）。"""
        self.ht.calendar = _FakeHKCalendar()
        self._set_state({"00700": "20260929", "00100": "20260929"})
        fired = self.ht.hk_freshness_alert(datetime(2026, 10, 1, 10, 0), alerts=self.alerts)
        self.assertTrue(fired)
        self.assertEqual(len(self.alerts.added), 1)
        level, source, msg = self.alerts.added[0]
        self.assertEqual(level, "warning")
        self.assertEqual(source, "港股")
        self.assertIn("港股数据滞后", msg)
        self.assertIn("20260930", msg)  # 消息含期望 session

    def test_stale_on_trading_day_waits_until_2000(self):
        """交易日 20:00 前不报（16:15 同步 + 10 分钟重试窗口未收口）→ 撤而不加。"""
        self.ht.calendar = _FakeHKCalendar()
        self._set_state({"00700": "20260930", "00100": "20260930"})
        fired = self.ht.hk_freshness_alert(datetime(2026, 10, 2, 17, 0), alerts=self.alerts)
        self.assertFalse(fired)
        self.assertEqual(self.alerts.added, [])
        self.assertIn(("港股", "港股数据滞后"), self.alerts.resolved)

    def test_stale_on_trading_day_after_2000_alerts(self):
        self.ht.calendar = _FakeHKCalendar()
        self._set_state({"00700": "20260930", "00100": "20260930"})
        fired = self.ht.hk_freshness_alert(datetime(2026, 10, 2, 20, 30), alerts=self.alerts)
        self.assertTrue(fired)
        self.assertEqual(len(self.alerts.added), 1)

    def test_fresh_resolves(self):
        """数据追平 → 撤旧警不新增（0.10.36 自愈纪律）。"""
        self.ht.calendar = _FakeHKCalendar()
        self._set_state({"00700": "20260930", "00100": "20260930"})
        fired = self.ht.hk_freshness_alert(datetime(2026, 10, 1, 10, 0), alerts=self.alerts)
        self.assertFalse(fired)
        self.assertEqual(self.alerts.added, [])
        self.assertIn(("港股", "港股数据滞后"), self.alerts.resolved)

    def test_no_calendar_resolves(self):
        self.ht.calendar = None
        fired = self.ht.hk_freshness_alert(datetime(2026, 10, 2, 21, 0), alerts=self.alerts)
        self.assertFalse(fired)
        self.assertEqual(self.alerts.added, [])


class HkSchedulerLoopGuardTest(_HkTasksBase):
    """调度守卫：不真跑线程，只验证守卫置位语义（_hk_due 组合路径由单测覆盖）。"""

    def test_loop_guard_state_defaults(self):
        guard = self.ht._hk_fired.setdefault(
            "20261002", {"fired": False, "attempts": 0, "next_retry": 0.0})
        self.assertFalse(guard["fired"])
        guard["fired"] = True
        self.assertFalse(self.ht._hk_due("18:00", guard))


if __name__ == "__main__":
    unittest.main()
