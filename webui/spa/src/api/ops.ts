// api/ops.ts — 运维域接口封装（告警/MCP 观测/版本）。
import { getJson, postJson } from './http'
import type { VersionPayload } from '../types/api'

export interface AlertMuteState {
  muted: boolean
  until: number | null
  remaining_sec?: number
  reason?: string | null
  preset?: string | null
}

export const getAlerts = (limit = 200) => getJson(`/api/alerts?limit=${limit}`)
export const clearAlerts = () => postJson('/api/alerts/clear', {})
// 0.10.38 告警静音：只影响"提醒强度"，不影响告警事实（count 恒定、timeline 照常）
export const getAlertMute = () => getJson<AlertMuteState>('/api/alerts/mute')
export const setAlertMute = (preset: string, reason?: string) =>
  postJson<AlertMuteState & { msg: string }>('/api/alerts/mute', { preset, reason })
export const clearAlertMute = () =>
  postJson<AlertMuteState & { msg: string }>('/api/alerts/mute', { clear: true })
export const getMcpStats = () => getJson('/api/mcp/stats') // 总调用/成功率/avg/p95/by_tool
export const getMcpCalls = (limit = 50) => getJson(`/api/mcp/calls?limit=${limit}`)
export const getVersion = () => getJson<VersionPayload>('/api/version') // webui/image/upstream/stale/ui_mode
