"""services.hk_tasks — 港股自动同步编排（0.12.0 H1：每日港股收盘后定时同步重点标的）。

背景（ROADMAP 主线三）：港股数据此前只有手动通道，必然陈化——00700 停在
2026-09-11（恰为 0.10.41 手动同步截止日，欠 ~14 个港股交易日），且面板全绿
无感（diag/告警只盯 A 股 data_latest）。H1 = 清单驱动定时同步 + 状态落盘 +
新鲜度条件告警，把"数据新鲜度维护"延伸到港股。

用例：hk_run_sync（同步重点标的清单）/ hk_scheduler_loop（调度线程，默认 16:15——
港股 16:00 收盘、收市竞价至 ~16:10，与 A 股 16:30/16:40 任务错开）/
hk_status（GET /api/hk/status）/ hk_freshness_alert（看门狗巡更项）。

依赖纪律：不 import app / storage.warehouse（层边界测试强制）——同步执行体
hk_sync 经注入点由 app.py（组合根）绑定；交易日判定注入 calendar_market.HK
（**不得复用 A 股日历**：两市场休市日年差 6+11 天；台风/黑雨临时休市不入表，
拉不到数据即跳过该日，"数据即事实"）。同步为逐键覆写语义（幂等），进程重启
守卫清空后当日重跑无害。
"""
from __future__ import annotations

import json
import os
import time
from datetime import datetime, timedelta
from pathlib import Path

import config
from ops.alerts import _get_alerts
from ops.logging import log
from storage.records import append as _records_append

# ---- 依赖注入点（app.py 装配时绑定；测试直接赋值，见 test_hk_tasks） ----
sync_fn = None    # app.hk_sync (codes, years) -> {code: {ok, bars, latest, error?}}
calendar = None   # core.calendar_market.HK（MarketCalendar：is_trading_day /
                  # nearest_trading_day / trading_days_between；None = 未装配，
                  # 调度宽松放行、新鲜度不告警——与打板注入约定一致）

_hk_fired: dict = {}  # 日级防重守卫：{date: {"fired": bool, "attempts": int, "next_retry": ts}}
_hk_run_state: dict = {"running": False, "started": None, "finished": None, "result": None}
_RETRY_INTERVAL = 600    # 失败重试间隔（10 分钟）
_RETRIED_UNTIL = "20:00"  # 超过此时刻放弃当日重试（告警收口）；新鲜度告警同此刻线
_STATE_FILE = "hk_sync_state.json"  # 落 config.DATA_DIR（诊断/告警的数据源）


def _now_iso() -> str:
    return datetime.now().isoformat(timespec="seconds")


def _state_path() -> Path:
    return Path(config.DATA_DIR) / _STATE_FILE


def watchlist() -> list[str]:
    """config.HK_SYNC_CODES（逗号分隔）→ 规范化代码清单（去 hk 前缀、数字补零 5 位）。"""
    out = []
    for raw in (config.HK_SYNC_CODES or "").split(","):
        c = raw.strip().lower()
        if not c:
            continue
        if c.startswith("hk"):
            c = c[2:]
        out.append(c.zfill(5) if c.isdigit() else c)
    return out


def _hk_due(now_hm: str, guard: dict) -> bool:
    """触发判定（纯函数，时间比较依赖 config 补零规范化的字典序）。

    到点未跑且不在重试等待期 → True。守卫语义沿 warehouse_tasks：
    {fired, attempts, next_retry}。
    """
    return (now_hm >= config.HK_SYNC_TIME and not guard["fired"]
            and time.time() >= guard["next_retry"])


def _write_state(state: dict) -> None:
    """原子落盘（tmp + os.replace，alerts.json 同款纪律）。失败不抛（观测数据）。"""
    try:
        path = _state_path()
        tmp = path.with_suffix(".json.tmp")
        tmp.write_text(json.dumps(state, ensure_ascii=False, indent=1), encoding="utf-8")
        os.replace(tmp, path)
    except Exception as exc:  # noqa: BLE001 - 状态落盘失败不阻塞同步结论
        log(f"⚠️ 港股同步状态落盘失败：{exc}")


def hk_state() -> dict | None:
    """读取最近一次同步状态（只读；供 diag 观测项与新鲜度告警用）。

    文件缺失/损坏 → None（调用方按"无记录"处理，不假造状态）；解析异常不外抛。
    """
    try:
        path = _state_path()
        if not path.is_file():
            return None
        data = json.loads(path.read_text(encoding="utf-8"))
        return data if isinstance(data, dict) else None
    except Exception:  # noqa: BLE001 - 观测数据，损坏按无记录降级
        return None


def hk_run_sync(codes: list[str] | None = None, years: int | None = None) -> dict:
    """同步任务本体：跑清单同步 → 状态落盘 + 日检留痕。

    不投递告警（调度层统一处理，避免手动路径刷告警）；失败原因在返回值
    failed/results 里。sync_fn 未装配 → {ok: False, reason}。任意单码失败不
    中断批次（hk_sync 逐码独立 try 的语义沿用）。
    """
    if sync_fn is None:
        return {"ok": False, "reason": "sync_fn 未装配（组合根未接线）"}
    codes = codes or watchlist()
    years = int(years or config.HK_SYNC_YEARS)
    _hk_run_state.update(running=True, started=_now_iso(), finished=None, result=None)
    results: dict = {}
    try:
        results = sync_fn(codes, years) or {}
    except Exception as exc:  # noqa: BLE001 - 任务函数单块降级，绝不外抛
        results = {c: {"ok": False, "error": str(exc)[:200]} for c in codes}
    finally:
        failed = sorted(c for c, r in results.items() if not (r or {}).get("ok"))
        latest = {c: (results.get(c) or {}).get("latest")
                  for c in results if (results.get(c) or {}).get("ok")}
        state = {
            "at": _now_iso(),
            "date": datetime.now().strftime("%Y%m%d"),
            "years": years,
            "latest": latest,
            "results": results,
        }
        _write_state(state)
        ok = not failed
        try:
            _records_append({"date": state["date"], "task": "hk_sync", "ok": ok,
                             "codes": list(codes), "failed": failed,
                             "latest": latest, "at": state["at"]})
        except Exception:  # noqa: BLE001 - 日检留痕失败不影响任务结论
            pass
        _hk_run_state.update(running=False, finished=_now_iso(),
                             result={"ok": ok, "failed": failed, "latest": latest})
    return {"ok": not failed, "failed": failed, "latest": latest, "results": results}


def hk_freshness(state: dict | None = None, *, today: datetime | None = None) -> dict:
    """新鲜度评估：各码 latest 落后港股日历期望 session 几个交易日。

    expected = calendar.nearest_trading_day(today)（≤今天的最近港股 session）；
    lag = latest 最小值与 expected 之间的港股交易日数（有洞即滞后，不看自然日）。
    日历未装配 / 无状态 / 无 latest → lag=None、stale=False（宽松放行，不误报）。
    """
    today = today or datetime.now()
    if calendar is None:
        return {"expected": None, "latest": {}, "lag": None, "stale": False}
    expected = calendar.nearest_trading_day(today.date())
    state = state if state is not None else hk_state()
    latest = {str(c): str(d) for c, d in ((state or {}).get("latest") or {}).items()
              if d}
    if not expected or not latest:
        return {"expected": expected, "latest": latest, "lag": None, "stale": False}
    min_latest = min(latest.values())
    try:
        # 严格晚于 latest 的期望交易日数：(latest+1日, expected] 区间内的 session 数
        # （两端统一 8 位串——与 expected 同形，日期类型不混用）
        start = (datetime.strptime(min_latest, "%Y%m%d")
                 + timedelta(days=1)).strftime("%Y%m%d")
        lag = len(calendar.trading_days_between(start, expected))
    except (TypeError, ValueError):
        return {"expected": expected, "latest": latest, "lag": None, "stale": False}
    return {"expected": expected, "latest": latest, "lag": lag, "stale": lag >= 1}


def hk_freshness_alert(now_dt: datetime | None = None, *, alerts=None) -> bool:
    """港股数据新鲜度看门狗（条件式投影：滞后才挂、追平即撤）。

    判定 = hk_freshness().stale 且已过观察线：
      - 今天非港股交易日 → 随时可报（上一 session 数据缺失即事实）；
      - 今天是港股交易日 → 20:00 后才报（给 16:15 同步 + 重试窗口留足时间，
        与 A 股 evening_stale_alert 同款时点语义）。
    日历未装配 / 无同步记录 → 撤旧警不新增（"无记录"≠"滞后"，不误报；
    任务自身的失败告警覆盖"从未跑成"的情形）。
    返回是否投递（测试用）。消息以「港股数据滞后」开头 = resolve 前缀契约。
    """
    now = now_dt or datetime.now()
    target = alerts if alerts is not None else _get_alerts()
    fresh = hk_freshness(today=now)
    if calendar is None or fresh["lag"] is None or not fresh["stale"]:
        target.resolve("港股", "港股数据滞后")
        return False
    is_hk_trading = calendar.is_trading_day(now.date())
    if is_hk_trading and now.strftime("%H:%M") < _RETRIED_UNTIL:
        target.resolve("港股", "港股数据滞后")  # 当日同步窗口未收口，暂不报
        return False
    target.add("warning", "港股",
               f"港股数据滞后 {fresh['lag']} 个交易日（最新 {min(fresh['latest'].values())}，"
               f"期望 {fresh['expected']}）")
    return True


def hk_status() -> dict:
    """运维状态（GET /api/hk/status）：配置 + 运行态 + 最近同步状态 + 新鲜度。"""
    return {"enabled": config.HK_SYNC_ENABLED, "sync_time": config.HK_SYNC_TIME,
            "codes": watchlist(), "years": config.HK_SYNC_YEARS,
            "running": _hk_run_state["running"], "started": _hk_run_state["started"],
            "finished": _hk_run_state["finished"],
            "last_result": _hk_run_state["result"],
            "state": hk_state(), "freshness": hk_freshness()}


def hk_scheduler_loop() -> None:
    """港股同步调度线程：5s 轮询，港股交易日 HK_SYNC_TIME 后触发（默认 16:15）。

    失败 → 10 分钟重试至 20:00，超时告警收口（error 级）；成功/彻底失败均置位
    守卫（当日不重复触发）。进程重启守卫清空，重跑幂等（hk_sync 覆写语义）。
    """
    while True:
        try:
            dt_now = datetime.now()
            if calendar is not None and not calendar.is_trading_day(dt_now.date()):
                time.sleep(5)
                continue
            today = dt_now.strftime("%Y%m%d")
            now_hm = dt_now.strftime("%H:%M")
            guard = _hk_fired.setdefault(today, {"fired": False, "attempts": 0, "next_retry": 0.0})
            if _hk_due(now_hm, guard):
                try:
                    res = hk_run_sync()
                    if res.get("ok"):
                        guard["fired"] = True
                        log(f"🇭🇰 港股同步完成（{today}）: latest={res.get('latest')}")
                    else:
                        guard["attempts"] += 1
                        guard["next_retry"] = time.time() + _RETRY_INTERVAL
                        if now_hm >= _RETRIED_UNTIL:
                            guard["fired"] = True
                            log(f"⚠️ 港股同步失败（当日收口）：{res.get('failed')}")
                            try:
                                _get_alerts().add(
                                    "error", "港股",
                                    f"{today} 港股同步重试至 {_RETRIED_UNTIL} 未成功："
                                    f"{'、'.join(res.get('failed') or [])}")
                            except Exception:  # noqa: BLE001 - 告警通道异常不阻塞调度
                                pass
                except Exception as exc:  # noqa: BLE001 - 调度线程绝不死
                    guard["fired"] = True
                    log(f"⚠️ 港股调度异常（当日收口）：{exc}")
                    try:
                        _get_alerts().add("error", "港股", f"港股同步调度异常：{exc}")
                    except Exception:  # noqa: BLE001
                        pass
            time.sleep(5)
        except Exception:  # noqa: BLE001 - 最外层兜底
            time.sleep(30)
