#!/usr/bin/env python3
"""calendar_xshg — A 股交易日历（**兼容壳**，0.10.43 起真身迁 core/calendar_market.py）。

历史：本模块曾是 XSHG 休市表与判定的唯一实现（app.py 另有一份内嵌拷贝）。
0.10.43 泛化为多市场后：
  - 表与判定 → `core/calendar_market.py`（XSHG_HOLIDAYS / SH 实例）
  - 本模块保留同名导出，**interfaces/mcp 与 services 的既有调用点零改动**
    （`calendar_xshg.is_trading_day(...)` / `trading_days_between(...)` /
    `nearest_trading_day(...)` / `XSHG_HOLIDAYS` / `XSHG_HOLIDAYS_THROUGH`）

新代码请直接 `from core.calendar_market import SH, HK` 或 `get_calendar(market)`。
"""

from __future__ import annotations

from core.calendar_market import (  # noqa: F401 - 兼容再导出
    XSHG_HOLIDAYS,
    XSHG_HOLIDAYS_THROUGH,
    is_trading_day,
    nearest_trading_day,
    trading_days_between,
)

__all__ = ["XSHG_HOLIDAYS", "XSHG_HOLIDAYS_THROUGH",
           "is_trading_day", "trading_days_between", "nearest_trading_day"]


if __name__ == "__main__":
    # 离线自检（保持输出与历史一致）
    print("20260101 is_trading_day:", is_trading_day("20260101"))
    print("20260105 is_trading_day:", is_trading_day("20260105"))
    print("nearest(20260101):", nearest_trading_day("20260101"))
    print("between 20260101-20260131:", trading_days_between("20260101", "20260131"))
