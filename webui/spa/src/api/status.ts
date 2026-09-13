// api/status.ts — 数据同步/系统域接口封装。
// 学习点：每个域的接口集中在一个模块，页面只 import 需要的函数，后端路径改了只动这里。
import { getJson, postJson } from './http'
import type { StatusPayload, ScheduleInfo, WarehouseStatus, TimelineDay, WarehouseTotals, SyncHistoryRow } from '../types/api'

export const getStatus = () => getJson<StatusPayload>('/api/status') // 状态总览（进程/同步/覆盖/磁盘/日历）
export const getHealth = () => getJson('/api/health') // 健康卡（最新日期/滞后天数/镜像）
export const getHistory = () => getJson<{ history: SyncHistoryRow[] }>('/api/history') // 同步历史
export const getSchedule = () => getJson<{ schedule: ScheduleInfo }>('/api/schedule') // 定时计划
export const getWarehouseStatus = () => getJson<WarehouseStatus>('/api/warehouse/status') // 仓库水位/最近沉淀/对账（W1 驾驶舱仓库灯）
export const getTimeline = (days = 7) =>
  getJson<{ days: TimelineDay[]; totals: WarehouseTotals | null }>(`/api/timeline?days=${days}`) // 驾驶舱时间线（W1 批 2）
// 保存定时：enabled 布尔；times 字符串数组（如 ['08:30','15:30']）；tradingOnly 布尔
export const saveSchedule = (enabled: boolean, times: string[], tradingOnly = true) =>
  getJson<{ msg: string; schedule: ScheduleInfo }>(`/api/schedule?action=save&enabled=${enabled}&times=${times.join(',')}&trading_only=${tradingOnly}`)
export const getLog = (n = 80) => getJson(`/api/log?n=${n}`) // 同步日志尾部
export const getContainerLogs = (tail = 150) => getJson(`/api/container/logs?tail=${tail}`)
export const restartContainer = () => postJson('/api/container/restart', {}) // 重启 stockdb（危险，需二次确认）
// 启动同步：hot=true 热更新（默认）；hot=false 停服严格模式
export const runSync = (hot = true) => postJson('/api/sync', { hot })
// 0.10.38「立即补录」三选项（语义不同，UI 必须区分并给后果说明）：
//   ① 重跑同步（=runSync，空转无害）② 仓库补沉淀（按水印缺口补日K分区）③ 打板指标回填
export const runWarehouse = (days = 3, backfill = false) =>
  postJson<{ ok: boolean; async?: boolean; reason?: string }>(
    '/api/warehouse/run', { days, backfill })
export const runAuctionBackfill = (days = 60) =>
  postJson<{ ok: boolean; async?: boolean; reason?: string }>(
    '/api/auction/run', { task: 'backfill', days })
// 港股日K 落盘：codes 代码数组；years 年数。返回 { 代码: {ok, bars, latest} | {ok:false, error} }
export interface HkSyncResultRow { ok: boolean; bars?: number; latest?: string; error?: string }
export const hkSync = (codes: string[], years = 2) =>
  postJson<Record<string, HkSyncResultRow>>('/api/hk/sync', { codes, years })
