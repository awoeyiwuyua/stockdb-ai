// stores/global.ts — 全局状态（Pinia）：单一数据仓库。
// 0.10.27「从零四件套」之一：refresh() 从 5 路散拉收敛为 1 路 /api/snapshot
// （overview + status + schedule + warehouse + timeline 同一瞬间一致视图）。
// getter 面向后端分块：顶栏/驾驶舱/抽屉组件各取所需，旧 getter 名（health/
// alertCount/mcp/version/lagDays）保持不变——消费方无感切换。
import { defineStore } from 'pinia'
import { getSnapshot } from '../api/snapshot'
import type {
  Snapshot, OverviewPayload, HealthStatus, AlertItem, VersionPayload,
  StatusPayload, ScheduleInfo, WarehouseStatus, TimelineDay, WarehouseTotals,
} from '../types/api'

export const useGlobalStore = defineStore('global', {
  state: () => ({
    snapshot: null as Snapshot | null, // /api/snapshot 全量载荷
    error: null as string | null, // 最近一次刷新错误文案（顶栏降级展示）
    lastRefresh: null as Date | null, // 最近成功刷新时间（Date）
  }),
  getters: {
    // —— overview 块（0.8.0 起旧 /api/overview getter 契约不变）——
    overview: (s): OverviewPayload | null => s.snapshot?.overview ?? null,
    health: (s): HealthStatus | null => s.snapshot?.overview?.health ?? null,
    alertCount: (s): number => s.snapshot?.overview?.alerts?.count ?? 0,
    alertsRecent: (s): AlertItem[] => s.snapshot?.overview?.alerts?.recent ?? [],
    mcp: (s): Record<string, unknown> | null => s.snapshot?.overview?.mcp ?? null,
    version: (s): VersionPayload | null => s.snapshot?.overview?.version ?? null,
    // 数据滞后天数（health.lag_days，未知视为 null）
    lagDays: (s): number | null => (s.snapshot?.overview?.health?.lag_days ?? null),
    // —— 0.10.27 新增块（原 use-cockpit 三路散拉的归宿）——
    status: (s): StatusPayload | null => s.snapshot?.status ?? null,
    schedule: (s): ScheduleInfo | null => s.snapshot?.schedule ?? null,
    warehouse: (s): WarehouseStatus | null => s.snapshot?.warehouse ?? null,
    timelineDays: (s): TimelineDay[] => s.snapshot?.timeline?.days ?? [],
    whTotals: (s): WarehouseTotals | null => s.snapshot?.timeline?.totals ?? null,
    // 面板自身版本（status.webui.version；资产卡/页脚展示用）
    serverVersion: (s): string | null => s.snapshot?.status?.webui?.version ?? null,
  },
  actions: {
    async refresh() {
      try {
        this.snapshot = await getSnapshot()
        this.error = null
        this.lastRefresh = new Date()
      } catch (e) {
        this.error = (e as Error)?.message || '接口不可用'
      }
    },
  },
})
