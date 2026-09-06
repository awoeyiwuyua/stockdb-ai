// types/ui.ts — 前端自有 UI 契约类型（与后端无关的展示层类型）。
// 后端 HTTP 契约在 types/api.ts；灯是 W1 驾驶舱的前端概念（§2.2），放这里。

export type LightTone = 'ok' | 'warn' | 'err' | 'off'

// 驾驶舱抽屉群（批 3）：alerts / logs / diag（含 MCP 标签）/ query
export type DrawerName = 'alerts' | 'logs' | 'diag' | 'query'

export interface Light {
  key: 'data' | 'sync' | 'warehouse' | 'disk'
  label: string
  tone: LightTone
  state: string  // 状态词（自解释三段之一）
  value: string  // 关键值（自解释三段之二）
  detail?: string
}
