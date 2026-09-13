#!/usr/bin/env python3
"""calendar_market — 多市场交易日历（A 股 XSHG / 港股 XHKG，休市表内嵌）。

来源：exchange_calendars（https://github.com/gerrymanoim/exchange_calendars）。
两市场**同一个库、同一套提取口径**，但**必须是两张表**——2026 年实测差异：
香港休而 A 股开 6 天（04-03/04-07/05-25/07-01/10-19/12-25）、
A 股休而香港开 11 天（含 10-02~10-07 国庆整周）。共用一张表两个市场都会错。

提取脚本：`stockdb-ai/scripts/extract_calendar_holidays.py`（仅维护期使用，
需 `pip install exchange_calendars`）。**运行期零依赖**：本模块纯标准库，
表是提取后内嵌的（webui 容器不背 pandas/numpy）。

本层为纯规则（core 依赖纪律）：不 import 其他层、不碰网络/文件/DB、无副作用日志。

口径与边界：
  - 判定 = 工作日（周一~周五）且 不在该年休市表内。交易所日历的 session 才是权威，
    故官方调休补班那类自然被排除。
  - **未收录年份按「工作日=交易日」放行**（沿用 0.8.x 起的历史语义）。这是**读取侧**
    的宽松语义：判成交易日只是多拉一次（拿到空数据，无害），判成休市才会漏数据。
    写入侧的严格防线在 `services/auction_tasks._auction_calendar_guard()`（超覆盖期
    拒绝任务并告警），不在本层。
  - **交易所日历不收录台风/黑色暴雨临时休市**（事后才知、无法预测）。本项目按
    「数据即事实」处理：拉不到数据就跳过该日，不预生成空行，也不伪造闭市标记。
  - **半日市**（圣诞前夕/除夕/年初一前夕）在日历里仍是 session（当天有成交），
    日K尺度无影响；做分钟K时需另判。

对外接口（与历史 calendar_xshg 逐位一致，另加多市场实例）：
    SH / HK                     市场日历实例（冻结配置，推荐用实例）
    is_trading_day(d)           模块级默认 = A 股（兼容旧调用）
    trading_days_between(s, e)  模块级默认 = A 股
    nearest_trading_day(d)      模块级默认 = A 股
"""

from __future__ import annotations

from datetime import date, datetime, timedelta

# 2024-2026 与 exchange_calendars 4.13.2 的 XSHG 逐年逐项一致（2026-09-13 核对：
# 20/20、18/18、19/19 全等）。2027 库尚未收录（last_session=2026-12-31），
# 官方次年安排公布后用提取脚本补条目并更新 THROUGH。
XSHG_HOLIDAYS: dict[str, set[str]] = {
    "2024": {"01-01", "02-09", "02-12", "02-13", "02-14", "02-15", "02-16",
             "04-04", "04-05", "05-01", "05-02", "05-03", "06-10", "09-16", "09-17",
             "10-01", "10-02", "10-03", "10-04", "10-07"},
    "2025": {"01-01", "01-28", "01-29", "01-30", "01-31", "02-03", "02-04",
             "04-04", "05-01", "05-02", "05-05", "06-02",
             "10-01", "10-02", "10-03", "10-06", "10-07", "10-08"},
    "2026": {"01-01", "01-02", "02-16", "02-17", "02-18", "02-19", "02-20", "02-23",
             "04-06", "05-01", "05-04", "05-05", "06-19", "09-25",
             "10-01", "10-02", "10-05", "10-06", "10-07"},
}
XSHG_HOLIDAYS_THROUGH = "2026-12-31"  # 休市表覆盖到的最后日期（XSHG 日历 last_session）

# 港股（XHKG）：提取自 exchange_calendars 4.13.2（last_session=2027-09-13）。
# 2026 表与香港政府宪报《2026年公眾假期》逐项吻合（12-26 为周六，被工作日过滤排除）。
# 注意港股**没有调休补假**，故休市工作日反而比 A 股少（2026 年 14 天 vs 19 天）。
XHKG_HOLIDAYS: dict[str, set[str]] = {
    "2026": {"01-01", "02-17", "02-18", "02-19", "04-03", "04-06", "04-07",
             "05-01", "05-25", "06-19", "07-01", "10-01", "10-19", "12-25"},
    "2027": {"01-01", "02-08", "02-09", "03-26", "03-29", "04-05", "05-13",
             "06-09", "07-01"},
}
XHKG_HOLIDAYS_THROUGH = "2027-09-13"  # XHKG 日历 last_session（比 A 股表多覆盖 9 个月）


def coerce_date(d: object) -> date:
    """公开的入参归一化：date（含 datetime）原样；'YYYYMMDD' 转 date；非法抛 ValueError。"""
    return MarketCalendar._as_date(d)


class MarketCalendar:
    """单个市场的交易日历（休市表 + 覆盖期，配置冻结）。

    只依赖传入的配置，无模块级状态——可安全地按市场各建一个实例，
    避免 `is_trading_day(d, market="sh")` 那种字符串参数被写错且不报错。
    """

    __slots__ = ("market", "holidays", "through")

    def __init__(self, market: str, holidays: dict[str, set[str]], through: str) -> None:
        self.market = market
        self.holidays = holidays
        self.through = through  # "YYYY-MM-DD"：休市表覆盖到的最后日期

    def __repr__(self) -> str:  # pragma: no cover - 诊断可读性
        return f"MarketCalendar({self.market!r}, through={self.through!r})"

    # ---- 内部 ----
    @staticmethod
    def _as_date(d: object) -> date:
        """入参归一化：date（含 datetime）原样；'YYYYMMDD' 转 date；非法抛 ValueError。"""
        if isinstance(d, date):
            return d
        if isinstance(d, str):
            if len(d) != 8 or not d.isdigit():
                raise ValueError(f"日期必须是 8 位 YYYYMMDD，当前 {d!r}")
            return datetime.strptime(d, "%Y%m%d").date()
        raise ValueError(f"日期必须是 date 或 8 位 YYYYMMDD 字符串，当前 {type(d).__name__}")

    def holidays_of(self, year: int) -> set[str] | None:
        """该年休市表；未收录年份返回 None（调用方按宽松语义处理）。"""
        return self.holidays.get(str(year))

    # ---- 对外 ----
    def is_trading_day(self, d: object) -> bool:
        """交易日判定：周六/周日 False；休市表内 False；未收录年份工作日视为 True。"""
        d = self._as_date(d)
        if d.weekday() >= 5:
            return False
        holidays = self.holidays_of(d.year)
        if holidays is None:
            return True  # 未收录年份：工作日即视为交易日（读取侧宽松，见模块 docstring）
        return d.strftime("%m-%d") not in holidays

    def trading_days_between(self, start: object, end: object) -> list[str]:
        """[start, end]（含端点）内全部交易日，升序 8 位字符串；start > end → []。"""
        start_d = self._as_date(start)
        end_d = self._as_date(end)
        if start_d > end_d:
            return []
        days: list[str] = []
        probe = start_d
        while probe <= end_d:
            if self.is_trading_day(probe):
                days.append(probe.strftime("%Y%m%d"))
            probe += timedelta(days=1)
        return days

    def nearest_trading_day(self, d: object) -> str | None:
        """<= d 的最近交易日（8 位字符串）；极早期找不到返回 None。"""
        probe = self._as_date(d)
        for _ in range(40000):  # 防御性上限（约 109 年），正常输入不会走到
            if probe.weekday() < 5:
                holidays = self.holidays_of(probe.year)
                if holidays is None or probe.strftime("%m-%d") not in holidays:
                    return probe.strftime("%Y%m%d")
            probe -= timedelta(days=1)
        return None


# 市场实例（推荐调用形态：SH.is_trading_day(...) / HK.trading_days_between(...)）
SH = MarketCalendar("sh", XSHG_HOLIDAYS, XSHG_HOLIDAYS_THROUGH)
HK = MarketCalendar("hk", XHKG_HOLIDAYS, XHKG_HOLIDAYS_THROUGH)
CALENDARS: dict[str, MarketCalendar] = {"sh": SH, "hk": HK}


def get_calendar(market: str) -> MarketCalendar:
    """按市场名取日历实例；未知市场抛 ValueError（不静默回落，防写错市场名）。"""
    key = str(market or "").strip().lower()
    if key not in CALENDARS:
        raise ValueError(f"未知市场 {market!r}（可用：{sorted(CALENDARS)}）")
    return CALENDARS[key]


# ---- 模块级默认 = A 股（兼容历史 calendar_xshg 调用点） ----
def is_trading_day(d: object) -> bool:
    """A 股交易日判定（兼容旧调用；多市场请用 SH/HK 实例）。"""
    return SH.is_trading_day(d)


def trading_days_between(start: object, end: object) -> list[str]:
    """A 股交易日区间（兼容旧调用）。"""
    return SH.trading_days_between(start, end)


def nearest_trading_day(d: object) -> str | None:
    """A 股 <= d 最近交易日（兼容旧调用）。"""
    return SH.nearest_trading_day(d)


if __name__ == "__main__":
    # 离线自检
    print("A股 20260101 is_trading_day:", SH.is_trading_day("20260101"))
    print("A股 nearest(20260101):", SH.nearest_trading_day("20260101"))
    print("A股 between 20260101-20260131:", SH.trading_days_between("20260101", "20260131"))
    print("港股 20261225 is_trading_day:", HK.is_trading_day("20261225"), "（圣诞，应为 False）")
    print("A股  20261225 is_trading_day:", SH.is_trading_day("20261225"), "（A股开市，应为 True）")
    print("港股 20261002 is_trading_day:", HK.is_trading_day("20261002"), "（A股休/港股开，应为 True）")
    print("A股  20261002 is_trading_day:", SH.is_trading_day("20261002"), "（应为 False）")
    print("港股覆盖至:", HK.through, "| A股覆盖至:", SH.through)
