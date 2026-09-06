// api/snapshot.ts — 驾驶舱单通道聚合（0.10.27 四件套之一）。
// 一次请求拿全 overview + status + schedule + warehouse + timeline（同一瞬间
// 一致视图）；前端全局轮询从 5 路收敛为 1 路。
import { getJson } from './http'
import type { Snapshot } from '../types/api'

export const getSnapshot = (days = 7) => getJson<Snapshot>(`/api/snapshot?days=${days}`)
