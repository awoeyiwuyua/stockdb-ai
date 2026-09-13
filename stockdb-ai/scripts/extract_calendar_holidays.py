"""提取 exchange_calendars 休市日表（维护期工具；webui 运行时零依赖，纯标准库内嵌表）。

用法：
    python extract_calendar_holidays.py                        # 默认 XSHG 2024-2026
    python extract_calendar_holidays.py --calendar XHKG        # 港股，默认年份同上
    python extract_calendar_holidays.py --years 2027 2028
    python extract_calendar_holidays.py --calendar XHKG --years 2026 2027
    python extract_calendar_holidays.py --all                  # 两个市场 × 日历可用年份

产出：可直接粘进 `core/calendar_market.py` 的 `XSHG_HOLIDAYS` / `XHKG_HOLIDAYS`
字典项，并打印该日历的 `last_session`（内嵌表的 `*_HOLIDAYS_THROUGH` 取它的年报）。

口径（与历史 XSHG_HOLIDAYS 一致，未变）：只取**周一~周五但非交易日**的日期——
交易所日历里的 session 才是权威判定，"工作日调休补班"那类自然被排除。
港股多出的两类需人工知悉：
  - **半日市**（圣诞前夕/除夕/年初一前夕）：日历里仍是 session（当天有成交），
    日K尺度无影响；做分钟K时需另判。
  - **台风/黑色暴雨临时休市**：**交易所日历不收录**（事后才知，且无法预测），
    本项目按"数据即事实"处理——拉不到数据就跳过该日，不预生成空行。
"""
from __future__ import annotations

import argparse
import json
import sys
from datetime import date, timedelta

DEFAULT_YEARS = (2024, 2025, 2026)
CALENDARS = ("XSHG", "XHKG")


def non_session_weekdays(cal, year: int) -> list[str]:
    """该年「周一~周五 but 非 session」的 MM-DD 列表（升序）。

    按日历的 first/last_session **夹取**范围：越界调 is_session 会抛
    DateOutOfBounds（XSHG 只到 2026-12-31、XHKG 到 2027-09-13，都是**部分年份**）。
    故末年会截断到 last_session——截断年份的覆盖截止以打印的 THROUGH 为准。
    """
    d = max(date(year, 1, 1), cal.first_session.date())
    end = min(date(year, 12, 31), cal.last_session.date())
    out: list[str] = []
    while d <= end:
        if d.weekday() < 5 and not cal.is_session(d):
            out.append(d.strftime("%m-%d"))
        d += timedelta(days=1)
    return out


def dump(name: str, xcals, years) -> int:
    cal = xcals.get_calendar(name)
    print(f"### {name}")
    print(f"# first_session={cal.first_session.date()}  last_session={cal.last_session.date()}")
    print(f"{name}_HOLIDAYS: dict[str, set[str]] = {{")
    for y in years:
        items = non_session_weekdays(cal, y)
        body = ", ".join(f'"{x}"' for x in items)
        print(f'    "{y}": {{{body}}},')
    print("}")
    print(f'# {name}_HOLIDAYS_THROUGH = "{cal.last_session.date()}"')
    return 0


def main() -> int:
    ap = argparse.ArgumentParser(description="提取 exchange_calendars 休市表")
    ap.add_argument("--calendar", default="XSHG", choices=CALENDARS)
    ap.add_argument("--years", type=int, nargs="+", default=None)
    ap.add_argument("--all", action="store_true", help="两个市场都提（年份取日历可用范围）")
    args = ap.parse_args()

    try:
        import exchange_calendars as xcals
    except ImportError:
        print("需要 exchange_calendars：pip install exchange_calendars", file=sys.stderr)
        return 1

    names = CALENDARS if args.all else (args.calendar,)
    for i, name in enumerate(names):
        if i:
            print()
        if args.years:
            years = list(args.years)
        elif args.all:
            cal = xcals.get_calendar(name)
            years = list(range(cal.first_session.year, cal.last_session.year + 1))
        else:
            years = list(DEFAULT_YEARS)
        dump(name, xcals, years)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
