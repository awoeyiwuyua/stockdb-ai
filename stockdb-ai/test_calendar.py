#!/usr/bin/env python3
"""test_calendar — 多市场交易日历（core/calendar_market.py）全离线单元测试。

覆盖：
  - A 股（XSHG）语义不回退：周末 / 元旦 / 国庆假期 / 未收录年份宽松放行
  - 港股（XHKG）新能力：春节区间与 A 股错开、**圣诞/复活节/七一休市**（A 股开）、
    **国庆整周 A 股休而港股开**、2027 年已收录（覆盖到 2027-09-13）
  - 两市场表**必须不同**（共用一张表两个市场都会错——本用例即该结论的护栏）
  - 兼容壳：core/calendar_xshg 同名导出与 app.is_trading_day 行为逐位一致
  - 边界：日期归一化、start>end、nearest 命中休市区间、未知市场名报错
  - 表与上游对表（可选）：本机装了 exchange_calendars 时逐项核对内嵌表

运行：cd stockdb-ai && python -m unittest test_calendar -v
"""
from __future__ import annotations

import datetime
import unittest

import app  # noqa: E402 - 兼容层行为（app.is_trading_day 委托 calendar_market.SH）
from core import calendar_market as cm  # noqa: E402
from core import calendar_xshg  # noqa: E402 - 兼容壳

# 2026 年两市场实测差异（NAS/库核对结论，见 docs/design/warehouse.md §2.2 附近说明）
HK_ONLY_HOLIDAYS_2026 = ("20260403", "20260407", "20260525", "20260701",
                         "20261019", "20261225")   # 香港休、A 股开
SH_ONLY_HOLIDAYS_2026 = ("20261002", "20261005", "20261006", "20261007",
                         "20260216", "20260220", "20260223")  # A 股休、香港开


class MarketCalendarShTests(unittest.TestCase):
    """A 股：0.10.43 泛化后行为与历史逐位一致。"""

    def test_weekend_not_trading(self):
        self.assertFalse(cm.SH.is_trading_day("20260815"))  # 周六
        self.assertFalse(cm.SH.is_trading_day("20260816"))  # 周日

    def test_new_year_and_national_day_holidays(self):
        self.assertFalse(cm.SH.is_trading_day("20260101"))
        self.assertFalse(cm.SH.is_trading_day("20261001"))
        self.assertFalse(cm.SH.is_trading_day("20261007"))
        self.assertTrue(cm.SH.is_trading_day("20261008"))   # 假期后首个交易日

    def test_normal_weekday_is_trading(self):
        self.assertTrue(cm.SH.is_trading_day("20260813"))   # 周四

    def test_unlisted_year_is_lenient(self):
        """未收录年份（2027 起）按工作日=交易日放行——读取侧宽松语义，非疏漏。"""
        self.assertTrue(cm.SH.is_trading_day("20270104"))   # 周一，未收录
        self.assertFalse(cm.SH.is_trading_day("20270102"))  # 周六仍排除

    def test_trading_days_between(self):
        self.assertEqual(cm.SH.trading_days_between("20260810", "20260814"),
                         ["20260810", "20260811", "20260812", "20260813", "20260814"])
        self.assertEqual(cm.SH.trading_days_between("20260814", "20260810"), [])

    def test_nearest_trading_day(self):
        self.assertEqual(cm.SH.nearest_trading_day("20260815"), "20260814")
        self.assertEqual(cm.SH.nearest_trading_day("20260814"), "20260814")

    def test_coverage_through_exposed(self):
        self.assertEqual(cm.SH.through, "2026-12-31")
        self.assertIsNone(cm.SH.holidays_of(2027))


class MarketCalendarHkTests(unittest.TestCase):
    """港股：与 A 股同一套规则、不同的表。"""

    def test_christmas_hk_closed(self):
        """圣诞港股休市（A 股照常开市）——这是两市场表必须分离的直接证据。"""
        self.assertFalse(cm.HK.is_trading_day("20261225"))
        self.assertTrue(cm.SH.is_trading_day("20261225"))

    def test_hk_only_holidays(self):
        for d in HK_ONLY_HOLIDAYS_2026:
            self.assertFalse(cm.HK.is_trading_day(d), f"港股应休市：{d}")
            self.assertTrue(cm.SH.is_trading_day(d), f"A股应开市：{d}")

    def test_sh_only_holidays(self):
        for d in SH_ONLY_HOLIDAYS_2026:
            self.assertFalse(cm.SH.is_trading_day(d), f"A股应休市：{d}")
            self.assertTrue(cm.HK.is_trading_day(d), f"港股应开市：{d}")

    def test_spring_festival_span_differs(self):
        """2026 春节：A 股 02-16~02-23 整段休，港股 02-17~02-19 三天。"""
        self.assertFalse(cm.SH.is_trading_day("20260216"))
        self.assertTrue(cm.HK.is_trading_day("20260216"))   # 港股年廿九仍开市
        self.assertFalse(cm.HK.is_trading_day("20260217"))
        self.assertTrue(cm.HK.is_trading_day("20260220"))   # A股仍休，港股已复市

    def test_hk_2027_covered(self):
        """港股表覆盖到 2027-09-13（比 A 股表多 9 个月，XHKG 日历已发布）。"""
        self.assertEqual(cm.HK.through, "2027-09-13")
        self.assertFalse(cm.HK.is_trading_day("20270101"))  # 元旦
        self.assertFalse(cm.HK.is_trading_day("20270701"))  # 七一
        self.assertTrue(cm.HK.is_trading_day("20270104"))
        self.assertIsNone(cm.HK.holidays_of(2028))         # 2028 仍未收录 → 宽松

    def test_nearest_trading_day_over_christmas(self):
        """圣诞区间（12-25 休、12-26 周六、12-27 周日）→ 回退到 12-24。"""
        self.assertEqual(cm.HK.nearest_trading_day("20261225"), "20261224")
        self.assertEqual(cm.HK.trading_days_between("20261224", "20261228"),
                         ["20261224", "20261228"])

    def test_hk_typhoon_day_follows_lenient_rule(self):
        """台风临时休市**不在任何日历表里**：日历仍判该日为交易日。

        这是刻意的——台风事后才知且无法预测，项目按"数据即事实"处理：
        拉不到数据就跳过该日。用例锁住这一语义，防止将来有人往表里塞猜的日期。
        """
        self.assertTrue(cm.HK.is_trading_day("20260813"))  # 若当日因风球停市，日历仍为 True


class CalendarTableIntegrityTests(unittest.TestCase):
    """表结构与两表差异护栏。"""

    def test_both_tables_present_and_nonempty(self):
        for name, cal in (("sh", cm.SH), ("hk", cm.HK)):
            self.assertTrue(cal.holidays, f"{name} 表为空")
            for year, days in cal.holidays.items():
                self.assertEqual(len(year), 4, f"{name} 年份键应为 YYYY：{year}")
                for md in days:
                    self.assertEqual(len(md), 5, f"{name} 日期应为 MM-DD：{md}")
                    self.assertEqual(md[2], "-")

    def test_tables_differ_sharply(self):
        """两表差异必须显著——若哪天变成同一张表，说明有人把市场参数写丢了。"""
        for year in ("2026", "2027"):
            sh = cm.SH.holidays.get(year)
            hk = cm.HK.holidays.get(year)
            if sh is None or hk is None:
                continue
            self.assertTrue(sh - hk, f"{year} 应存在「A股休/港股开」的日子")
            self.assertTrue(hk - sh, f"{year} 应存在「港股休/A股开」的日子")

    def test_market_instances_frozen_config(self):
        self.assertEqual(cm.SH.market, "sh")
        self.assertEqual(cm.HK.market, "hk")
        self.assertIs(cm.get_calendar("SH"), cm.SH)
        self.assertIs(cm.get_calendar("hk"), cm.HK)
        with self.assertRaises(ValueError):
            cm.get_calendar("us")       # 未知市场必须报错，不静默回落
        with self.assertRaises(ValueError):
            cm.get_calendar("")


class CalendarInputTests(unittest.TestCase):
    """入参归一化与异常语义。"""

    def test_accepts_date_and_datetime_and_str(self):
        self.assertTrue(cm.SH.is_trading_day("20260813"))
        self.assertTrue(cm.SH.is_trading_day(datetime.date(2026, 8, 13)))
        self.assertTrue(cm.SH.is_trading_day(datetime.datetime(2026, 8, 13, 15, 0)))

    def test_rejects_bad_input(self):
        for bad in ("2026-08-13", "2026813", "abcdefgh", 20260813, None, object()):
            with self.assertRaises(ValueError):
                cm.SH.is_trading_day(bad)

    def test_coerce_date_public(self):
        self.assertEqual(cm.coerce_date("20260813"), datetime.date(2026, 8, 13))
        with self.assertRaises(ValueError):
            cm.coerce_date("2026-08-13")


class CalendarCompatTests(unittest.TestCase):
    """兼容壳：既有调用点零改动的保证。"""

    def test_calendar_xshg_reexports_same_objects(self):
        self.assertIs(calendar_xshg.XSHG_HOLIDAYS, cm.XSHG_HOLIDAYS)
        self.assertEqual(calendar_xshg.XSHG_HOLIDAYS_THROUGH, cm.XSHG_HOLIDAYS_THROUGH)
        self.assertEqual(calendar_xshg.is_trading_day("20260101"),
                         cm.SH.is_trading_day("20260101"))
        self.assertEqual(calendar_xshg.nearest_trading_day("20260815"),
                         cm.SH.nearest_trading_day("20260815"))
        self.assertEqual(calendar_xshg.trading_days_between("20260810", "20260814"),
                         cm.SH.trading_days_between("20260810", "20260814"))

    def test_module_level_defaults_are_sh(self):
        self.assertFalse(cm.is_trading_day("20260101"))
        self.assertTrue(cm.is_trading_day("20260813"))

    def test_app_is_trading_day_delegates_to_sh(self):
        for d in ("20260101", "20260813", "20260815", "20261002", "20261225"):
            self.assertEqual(app.is_trading_day(d), cm.SH.is_trading_day(d),
                             f"app 与 SH 判定不一致：{d}")
        self.assertEqual(app.XSHG_HOLIDAYS, cm.XSHG_HOLIDAYS)
        self.assertEqual(app.XSHG_HOLIDAYS_THROUGH, cm.XSHG_HOLIDAYS_THROUGH)

    def test_app_accepts_all_three_input_forms(self):
        """app.is_trading_day 历史上三种入参都在用：None / date / 8 位字符串。"""
        self.assertIsInstance(app.is_trading_day(), bool)
        self.assertTrue(app.is_trading_day(datetime.date(2026, 8, 13)))
        self.assertTrue(app.is_trading_day("20260813"))


class EmbeddedTableMatchesUpstreamTests(unittest.TestCase):
    """可选核对：本机装了 exchange_calendars 时，内嵌表必须与库逐年逐项一致。

    未安装则跳过（运行期零依赖是本项目的纪律，库只在维护期用）。
    """

    def _lib_holidays(self, calendar_name: str, year: int) -> list[str]:
        import exchange_calendars as xcals
        cal = xcals.get_calendar(calendar_name)
        d = max(datetime.date(year, 1, 1), cal.first_session.date())
        end = min(datetime.date(year, 12, 31), cal.last_session.date())
        out = []
        while d <= end:
            if d.weekday() < 5 and not cal.is_session(d):
                out.append(d.strftime("%m-%d"))
            d += datetime.timedelta(days=1)
        return out

    def _check(self, calendar_name: str, embedded: dict, years):
        try:
            import exchange_calendars  # noqa: F401
        except ImportError:
            self.skipTest("exchange_calendars 未安装（维护期依赖，可选）")
        for year in years:
            y = str(year)
            if y not in embedded:
                continue
            self.assertEqual(sorted(embedded[y]), self._lib_holidays(calendar_name, year),
                             f"{calendar_name} {y} 内嵌表与库不一致")

    def test_xshg_embedded_matches_library(self):
        self._check("XSHG", cm.XSHG_HOLIDAYS, (2024, 2025, 2026))

    def test_xhkg_embedded_matches_library(self):
        self._check("XHKG", cm.XHKG_HOLIDAYS, (2026, 2027))


if __name__ == "__main__":
    unittest.main()
