"""services.warehouse_tasks — 仓库沉淀编排（0.10.0 W4；D11 同构：编排在服务层、执行在数据层）。

用例：warehouse_run（每日沉淀，默认 16:40 触发）/ warehouse_scheduler_loop（调度线程）。
流程：就绪门（data_latest >= today）→ 全市场快照（TRADED 行 = 当日日K）→
sink 写分区（factor_map 物化复权列）+ codes 刷新 → reconcile 对账（三板斧）→
records 日检 + 告警。
复权（0.10.10）：周一/首刷经 adjust_provider 注入因子事件 → _build_factor_map 展开为
{code: cum} 缓存，沉淀时物化 adj_factor+fq 列（一次计算多次复用，查询零 JOIN）；
事件不落 facts（内存输入，审计留档延后）。SDK 通道接入前 adjust_provider=None →
物化列 NULL 原价，不阻塞沉淀。

依赖纪律：不 import storage.warehouse（C3，层边界测试强制）——sink/reconcile/
availability 经注入点由 app.py（组合根）绑定；引擎快照/交易日判定同打板注入模式。
"""
from __future__ import annotations

import threading
import time
from datetime import datetime

import config
from ops.alerts import notify_alert
from ops.logging import log
from storage.records import append as _records_append

# ---- 依赖注入点（app.py 装配时绑定；测试直接赋值，见 test_warehouse W4 段） ----
query_snapshot = None   # interfaces.mcp.stockdb_mcp_server.query_point_snapshot
data_latest = None      # app.data_latest_date（最新已同步交易日探针）
is_trading_day = None   # app.is_trading_day
sink = None             # storage.warehouse.sink（模块；.catalog/.layout 为其子模块属性）
reconcile_daily = None  # storage.warehouse.reconcile.reconcile_daily
warehouse_root = None   # () -> Path（storage.warehouse.layout.root_dir）
availability = None     # storage.warehouse.availability
refresh_views = None    # storage.warehouse.engine.get_engine().refresh_views
backup_duckdb = None    # storage.warehouse.backup.backup_duckdb（0.10.8：warehouse.duckdb 日级备份）
adjust_provider = None  # () -> list[dict]（复权因子事件序列：{code,date,div,give,trans,mult,cum}；
                        # 未接 SDK 通道前为 None → 物化列 NULL 原价，延后项）
# 周度复权事件刷新：周一沉淀日顺带全量（事件小、全量幂等）
_ADJUST_WEEKDAYS = {0}

_wh_fired: dict = {}  # 日级防重守卫：{date: {"fired": bool, "attempts": int, "next_retry": ts}}
_wh_run_state: dict = {"running": False, "started": None, "finished": None, "result": None}
_RETRY_INTERVAL = 600  # 未就绪/失败重试间隔（10 分钟）
_BACKFILL_FLOOR = "20000101"  # 回填下界（引擎日K实测起点 2000 年）
_RETRY_UNTIL = "20:00"  # 超过此时刻放弃当日沉淀（告警收口）

# 0.10.10：每日累计因子缓存 {code: cum}（周度/首刷刷新；沉淀物化复用，一次计算）
_factor_map_cache: dict[str, float] = {}


def _now_iso() -> str:
    return datetime.now().isoformat(timespec="seconds")


def _build_factor_map(events: list[dict]) -> dict[str, float]:
    """复权事件序列 → {code: 截至当日最新累计因子 cum}（0.10.10 物化输入）。

    引擎事件按码返回（div/give/trans/mult/cum 每次分红/送转一条，cum 为累计因子）；
    事件未带 date 时以注入的刷新日为准。缺码/非有限值 → 不进入 map（物化列 NULL 原价）。
    """
    out: dict[str, float] = {}
    for ev in events or []:
        code = str(ev.get("code") or "").strip()
        cum = ev.get("cum")
        try:
            cum = float(cum)
        except (TypeError, ValueError):
            continue
        if code and cum is not None and cum == cum and abs(cum) != float("inf"):
            out[code] = cum
    return out


def _snapshot_points(date: str) -> list[dict]:
    """全市场单日快照（limit=0 = 不截断，一次往返）+ 字段适配。

    快照通道的 prev_close 映射为引擎原生 pre_close（0.10.7 sink 纯镜像引擎字段，
    改名适配归通道侧）。"""
    points = (query_snapshot({"date": date, "limit": 0}) or {}).get("points") or []
    return [{**p, "pre_close": p.get("pre_close", p.get("prev_close"))} for p in points]


def warehouse_run(days: int = 1, reconcile_sample: int = 10,
                  require_today: bool = False, backfill: bool = False) -> dict:
    """沉淀任务（幂等；days>1 供小范围测试通道，上限 5）。

    就绪门：require_today=True（调度路径）时要求 data_latest >= 今日（当日 K 线
    已同步——与打板收口同判定），未就绪返回 reason 以"未就绪："开头（调度层重试）；
    手动路径（HTTP 运维口）不设此门，沉淀到最新已同步日为止。
    backfill=True（0.10.3）：历史回填模式——向 watermark 之前回看 days 个交易日
    （跳过非交易日；已有分区幂等跳过；watermark 不回退）。
    对账：行数 + 同源字段回读 + （有异源行时）异源开盘/昨收；issues 非空 → 告警 + 日检记录。
    """
    if availability is None or not availability()[0]:
        return {"ok": False, "reason": "warehouse 不可用（duckdb 缺失或 WAREHOUSE_ENABLED=0）"}
    days_cap = 9999 if backfill else 5  # backfill=全量回填意图，放开上限
    days = max(1, min(days_cap, int(days)))
    root = warehouse_root()
    try:
        latest = str(data_latest(force=True) or "").replace("-", "")
        if not latest:
            return {"ok": False, "reason": "未就绪：无法确定最新已同步交易日"}
        if require_today:
            today = datetime.now().strftime("%Y%m%d")
            if latest < today:
                return {"ok": False,
                        "reason": f"未就绪：数据未同步（最新 {latest} < 今日 {today}）"}
        watermark = sink.catalog.get_watermark(root, "daily") if hasattr(sink, "catalog") else None

        results = []
        # 目标日集合（0.10.3 增 backfill 模式）：
        #   默认：watermark 之后的前向缺口（正常调度语义——不重复沉淀）
        #   backfill=True：从已沉淀最早日（无沉淀则 latest）向更早回看 days 个**交易日**
        #     （跳过非交易日；已有分区由 sink 跳过，幂等；watermark 只前进不受影响）
        targets = []
        # 目标日集合（0.10.3 增 backfill；0.10.6 改**缺口感知**）：
        #   默认：watermark 之后的前向缺口（正常调度语义——不重复沉淀）
        #   backfill=True：从最新已沉淀日向下扫全部日历日——已有分区文件或已标记
        #     "空交易日"（catalog empty:）的日期跳过，其余（含中断留下的历史空洞）
        #     全部补齐。旧语义只从 sedimented[0] 向下挖，进程中断后会漏掉其上方的
        #     大段空洞（实测：2000-08 与 2026-05 两段间 25 年被跳过）。
        if backfill:
            sedimented = sink.layout.list_daily_dates(root) if hasattr(sink, "layout") else []
            anchor = watermark or (sedimented[-1] if sedimented else latest)
            # 0.10.17：anchor 当天纳入回看——正常时它有分区（循环内 skip-existing）；
            # 分区文件缺失（迁移删文件/磁盘事故）时这正是要补的洞。此前从 anchor
            # 前一天起扫，NAS 08-29 迁移实证：删 0828 分区后该日成永久空洞且
            # 完整月校验拒绝聚合（月卡死）。
            cursor = anchor
            while len(targets) < days and cursor >= _BACKFILL_FLOOR:
                if is_trading_day is None or is_trading_day(
                        datetime.strptime(cursor, "%Y%m%d").date()):
                    has_file = any(sink.layout.daily_partition(root, cursor, m).exists()
                                   for m in ("sh", "sz", "bj"))
                    marked_empty = sink.catalog.get_meta(root, f"empty:{cursor}") is not None
                    if not has_file and not marked_empty:
                        targets.append(cursor)
                cursor = _prev_date(cursor)
            targets.sort()  # 旧 → 新
        else:
            target = latest
            for _ in range(days):
                if watermark and target <= watermark:
                    break  # 只补 watermark 之后的缺口，不重复沉淀
                targets.append(target)
                target = _prev_date(target)
        # 周度/首刷复权刷新（周一 or 缓存空）：事件经 adjust_provider 注入，
        # 展开为 factor_map（0.10.10：内存输入，不占 facts；审计留档延后）
        global _factor_map_cache
        try:
            if (datetime.now().weekday() in _ADJUST_WEEKDAYS or not _factor_map_cache):
                adjust_rows = _adjust_rows(latest)
                if adjust_rows:
                    _factor_map_cache = _build_factor_map(adjust_rows)
        except Exception as exc:  # noqa: BLE001 - 复权刷新失败不阻塞日K沉淀
            log(f"⚠️ 仓库复权刷新失败（不阻塞日K）：{exc}")

        # 每日沉淀：factor_map 物化复权列（一次计算多次复用；事件未就绪 → 原价 NULL）
        for t in sorted(targets):  # 旧 → 新（缺口感知语义下 targets 已升序，幂等保序）
            points = [p for p in _snapshot_points(t)
                      if isinstance(p, dict) and p.get("status") == "TRADED"]
            if not points:
                # 空交易日标记（catalog）：缺口感知回填据此跳过，节假日不再反复重探
                try:
                    sink.catalog.set_meta(root, f"empty:{t}", 1)
                except Exception:  # noqa: BLE001 - 标记失败不影响主流程
                    pass
                results.append({"date": t, "status": "empty"})
                continue
            w = sink.write_daily(root, t, points, factor_map=_factor_map_cache)
            sink.write_codes(root, [{"code": p.get("code"), "name": p.get("name")}
                                    for p in points])
            rec = reconcile_daily(root, t, points,
                                  sedimented_rows=w.get("rows", 0),
                                  dropped_nonfinite=w.get("dropped_nonfinite", 0),
                                  sample=reconcile_sample)
            results.append({"date": t, "write": w, "reconcile": rec})
            if not rec["ok"]:
                notify_alert("warn", "warehouse",
                             f"沉淀对账差异 {t}: {rec['issues'][:5]}")
            _records_append({"date": t, "task": "warehouse_sediment",
                             "ok": rec["ok"], "rows": w.get("rows", 0),
                             "traded": rec["traded"], "issues": rec["issues"][:5],
                             "at": _now_iso()})

        # 0.10.10/0.10.12 粒度阶梯：周K/月K 聚合物化——对沉淀覆盖到的每个自然周/月
        # （周完整/月完整才聚合，幂等；当前未走完的周期由 v_week_current/v_month_current
        # 派生视图实时聚合，见 engine）
        try:
            if results:
                _aggregate_weeks(root, results)
                _aggregate_months(root, results)
        except Exception as exc:  # noqa: BLE001 - 聚合失败不阻塞日K沉淀结论
            log(f"⚠️ 仓库周/月K聚合失败（不阻塞日K）：{exc}")

        if refresh_views is not None:
            try:
                refresh_views()
            except Exception:  # noqa: BLE001 - 视图刷新失败下次重建
                pass
        # 0.10.8：warehouse.duckdb 日级备份（沿 research_store 模式；失败静默不阻塞）
        if backup_duckdb is not None and results:
            try:
                path = backup_duckdb(root)
                if path is not None:
                    log(f"🗄️ warehouse.duckdb 备份完成：{path.name}")
            except Exception:  # noqa: BLE001 - 备份失败不影响沉淀结论
                log("⚠️ warehouse.duckdb 备份失败（已静默，不影响沉淀）")
        ok = all(r.get("reconcile", {}).get("ok", True) for r in results)
        return {"ok": ok, "days": results, "finished_at": _now_iso()}
    except Exception as exc:  # noqa: BLE001 - 单块降级
        log(f"⚠️ 仓库沉淀失败：{exc}")
        notify_alert("error", "warehouse", f"沉淀任务失败：{exc}")
        return {"ok": False, "reason": str(exc), "finished_at": _now_iso()}


def _prev_date(d: str) -> str:
    from datetime import timedelta
    dt = datetime.strptime(d, "%Y%m%d") - timedelta(days=1)
    return dt.strftime("%Y%m%d")


def _week_end_of(d: str) -> str:
    """日期所在自然周的周五（YYYYMMDD）。周一=周五-4；跨月/跨年安全（datetime 运算）。"""
    from datetime import timedelta
    dt = datetime.strptime(d, "%Y%m%d")
    friday = dt + timedelta(days=(4 - dt.weekday()))
    return friday.strftime("%Y%m%d")


def _week_complete(root, week_end: str) -> bool:
    """该周是否完整：周内每个交易日均已有 daily 分区文件（或空交易日标记）。

    0.10.12 修复：旧实现只比较 watermark ≥ 周内最后交易日——周内某交易日因故
    缺失（数据缺口）时 watermark 仍可能推进，导致残缺周被聚合且幂等不再重写。
    现在逐交易日校验文件/标记存在，缺任何一天 → 不完整 → 等补齐后下次沉淀聚合。
    节假日周（周五非交易日）以 is_trading_day 判定，非交易日跳过。
    """
    from datetime import timedelta
    friday = datetime.strptime(week_end, "%Y%m%d")
    for i in range(5):  # 周一~周五（周五→周一扫描）
        d = friday - timedelta(days=i)
        if is_trading_day is not None and not is_trading_day(d.date()):
            continue  # 非交易日（周末/节假日）跳过
        d8 = d.strftime("%Y%m%d")
        has_file = any(sink.layout.daily_partition(root, d8, m).exists()
                       for m in ("sh", "sz", "bj", "hk"))
        marked_empty = sink.catalog.get_meta(root, f"empty:{d8}") is not None
        if not has_file and not marked_empty:
            return False  # 该交易日缺失 → 周不完整
    return True


def _aggregate_weeks(root, results) -> None:
    """沉淀结果 → 自动周K聚合（0.10.10）：对每个覆盖到的自然周，周完整才聚合。
    幂等由 sink.aggregate_weekly 保证（周分区存在跳过，watermark:week 只前进）。"""
    seen: set[str] = set()
    for r in results:
        d = r.get("date")
        if not d:
            continue
        week_end = _week_end_of(d)
        if week_end in seen:
            continue
        seen.add(week_end)
        if _week_complete(root, week_end):
            sink.aggregate_weekly(root, week_end)


def _month_end_of(d: str) -> str:
    """日期所在自然月的月末（YYYYMMDD）。跨年安全（datetime 运算）。"""
    from datetime import timedelta as _td
    dt = datetime.strptime(d, "%Y%m%d")
    year, month = dt.year, dt.month
    if month == 12:
        nxt = datetime(year + 1, 1, 1)
    else:
        nxt = datetime(year, month + 1, 1)
    return (nxt - _td(days=1)).strftime("%Y%m%d")


def _month_complete(root, month_end: str) -> bool:
    """该月是否完整：月内每个交易日均已有 daily 分区文件（或空交易日标记）。

    与 _week_complete 同策略（0.10.12）：逐交易日校验文件/标记存在，缺任何
    一天 → 不完整 → 等补齐后下次沉淀聚合。月末是日历日（可能非交易日），
    以 is_trading_day 判定跳过非交易日。
    """
    from datetime import timedelta as _td
    month_end_dt = datetime.strptime(month_end, "%Y%m%d")
    month_start_dt = datetime(month_end_dt.year, month_end_dt.month, 1)
    cursor = month_start_dt
    while cursor <= month_end_dt:
        if is_trading_day is not None and not is_trading_day(cursor.date()):
            cursor += _td(days=1)
            continue  # 非交易日（周末/节假日）跳过
        d8 = cursor.strftime("%Y%m%d")
        has_file = any(sink.layout.daily_partition(root, d8, m).exists()
                       for m in ("sh", "sz", "bj", "hk"))
        marked_empty = sink.catalog.get_meta(root, f"empty:{d8}") is not None
        if not has_file and not marked_empty:
            return False  # 该交易日缺失 → 月不完整
        cursor += _td(days=1)
    return True


def _aggregate_months(root, results) -> None:
    """沉淀结果 → 自动月K聚合（0.10.12）：对每个覆盖到的自然月，月完整才聚合。
    幂等由 sink.aggregate_monthly 保证（月分区存在跳过，watermark:month 只前进）。"""
    seen: set[str] = set()
    for r in results:
        d = r.get("date")
        if not d:
            continue
        month_end = _month_end_of(d)
        if month_end in seen:
            continue
        seen.add(month_end)
        if _month_complete(root, month_end):
            sink.aggregate_monthly(root, month_end)


def _adjust_rows(latest: str) -> list[dict]:
    """复权因子事件序列（经注入的 adjust_provider；None → 未接通道，物化列 NULL 原价）。"""
    if adjust_provider is None:
        return []
    return adjust_provider() or []


def maybe_catchup_sediment() -> bool:
    """数据晚到自愈钩子（0.10.13）：同步推进数据后，若水印仍落后 → 补沉淀。

    场景（0.10.6 试运行 08-28 实证）：镜像晚发布 → 16:40 沉淀就绪门未过 →
    20:00 超时告警收口、守卫置位；数据 21:55 才到位后无人补沉淀，水印滞后一日
    （run_sql 跨周期查询少一天）。此钩子由 run_sync 成功收尾时调用：
    交易日、已过沉淀时间、数据已越过水印 → warehouse_run_async 补沉淀
    （不带就绪门——data_latest 即已同步的最新日；幂等 + 单飞，正在跑则跳过）。
    返回是否触发了补沉淀（测试用）；未装配/仓库不可用/无缺口一律静默 False。
    """
    try:
        if availability is None or not availability()[0]:
            return False
        if is_trading_day is not None and not is_trading_day(datetime.now().date()):
            return False
        if datetime.now().strftime("%H:%M") < config.WAREHOUSE_SEDIMENT_TIME:
            return False
        if _wh_run_state["running"]:
            return False
        latest = str(data_latest(force=True) or "").replace("-", "") if data_latest else ""
        if not latest:
            return False
        root = warehouse_root()
        watermark = (sink.catalog.get_watermark(root, "daily")
                     if sink is not None and hasattr(sink, "catalog") else None)
        if not watermark or watermark >= latest:
            return False
        res = warehouse_run_async(days=1)
        if res.get("ok"):
            log(f"📊 数据晚到补沉淀已触发（watermark {watermark} < data_latest {latest}）")
            return True
        return False
    except Exception as exc:  # noqa: BLE001 - 自愈钩子绝不外抛（同步收尾调用）
        log(f"⚠️ 补沉淀钩子异常（已忽略）: {exc}")
        return False


def warehouse_run_async(days: int = 1, reconcile_sample: int = 10,
                        backfill: bool = False) -> dict:
    """异步触发沉淀（HTTP 运维口用；单飞防重，状态进 _wh_run_state）。"""
    if _wh_run_state["running"]:
        return {"ok": False, "async": True, "reason": "沉淀任务已在运行中",
                "started": _wh_run_state["started"]}

    def _worker():
        _wh_run_state.update(running=True, started=_now_iso(), finished=None, result=None)
        try:
            result = warehouse_run(days=days, reconcile_sample=reconcile_sample,
                                   backfill=backfill)
        except Exception as exc:  # noqa: BLE001 - 状态落库，绝不外抛
            result = {"ok": False, "reason": str(exc)}
        _wh_run_state.update(running=False, finished=_now_iso(), result=result)
        log(f"📊 仓库沉淀完成：ok={result.get('ok')} {result.get('days') or result.get('reason') or ''}")

    threading.Thread(target=_worker, daemon=True).start()
    return {"ok": True, "async": True, "reason": "沉淀已启动（后台执行，GET /api/warehouse/status 查进度）"}


def warehouse_status() -> dict:
    """运维状态（GET /api/warehouse/status）：可用性 + 调度守卫 + 任务状态 + 仓库元数据。"""
    available = availability is not None and availability()[0]
    root = warehouse_root() if warehouse_root is not None else None
    out = {"available": available, "running": _wh_run_state["running"],
           "started": _wh_run_state["started"], "finished": _wh_run_state["finished"],
           "last_result": _wh_run_state["result"],
           "sediment_time": config.WAREHOUSE_SEDIMENT_TIME}
    if available and root is not None and sink is not None:
        try:
            out["watermark_daily"] = sink.catalog.get_watermark(root, "daily")
            out["factor_map_size"] = len(_factor_map_cache)  # 0.10.10：复权物化缓存规模
        except Exception as exc:  # noqa: BLE001 - 状态查询不抛
            out["catalog_error"] = str(exc)
    return out


def warehouse_scheduler_loop() -> None:
    """仓库沉淀调度线程：5s 轮询，交易日 WAREHOUSE_SEDIMENT_TIME 后触发（默认 16:40）。

    就绪门失败（数据未同步）→ 10 分钟重试至 20:00，超时告警收口；成功/彻底失败
    均置位守卫（当日不重复触发）。进程重启守卫清空，重跑幂等（sink 跳过已有分区）。
    """
    while True:
        try:
            dt_now = datetime.now()
            if is_trading_day is not None and not is_trading_day(dt_now.date()):
                time.sleep(5)
                continue
            today = dt_now.strftime("%Y%m%d")
            now_hm = dt_now.strftime("%H:%M")
            guard = _wh_fired.setdefault(today, {"fired": False, "attempts": 0, "next_retry": 0.0})
            if (now_hm >= config.WAREHOUSE_SEDIMENT_TIME and not guard["fired"]
                    and time.time() >= guard["next_retry"]):
                try:
                    if availability is None or not availability()[0]:
                        guard["fired"] = True  # 仓库不可用：当日不再空转
                        log("📊 仓库沉淀跳过：warehouse 不可用（duckdb 缺失或已关闭）")
                        continue
                    res = warehouse_run(days=1, require_today=True)
                    if res.get("ok"):
                        guard["fired"] = True
                        days = res.get("days") or []
                        w = days[-1].get("write", {}) if days else {}
                        log(f"📊 仓库沉淀完成（{today}）: rows={w.get('rows')} "
                            f"markets={w.get('markets')} reconcile={days[-1].get('reconcile', {}).get('ok') if days else None}")
                    elif str(res.get("reason") or "").startswith("未就绪："):
                        # 就绪门未过：数据同步未收口 → 延后重试
                        guard["attempts"] += 1
                        guard["next_retry"] = time.time() + _RETRY_INTERVAL
                        if now_hm >= _RETRY_UNTIL:
                            guard["fired"] = True
                            notify_alert("error", "warehouse",
                                         f"{today} 沉淀就绪门至 {_RETRY_UNTIL} 未过：{res.get('reason')}")
                    else:
                        guard["fired"] = True  # 硬失败：告警已发（warehouse_run 内），当日收口
                        log(f"⚠️ 仓库沉淀失败（当日收口）：{res.get('reason')}")
                except Exception as exc:  # noqa: BLE001 - 调度线程绝不死
                    guard["fired"] = True
                    log(f"⚠️ 仓库调度异常（当日收口）：{exc}")
                    notify_alert("error", "warehouse", f"沉淀调度异常：{exc}")
            time.sleep(5)
        except Exception:  # noqa: BLE001 - 最外层兜底
            time.sleep(30)
