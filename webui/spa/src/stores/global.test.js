// global.test.js — 全局状态仓库（Pinia store）单测（Vitest，node 环境）。
// 学习点：
//   1) vi.stubGlobal('fetch', ...) 把全局 fetch 换成"假实现"，测试不用真发网络请求；
//   2) setActivePinia(createPinia()) 每个用例新建隔离的 Pinia，state 互不串扰。
// 0.10.27：refresh() 切换为 /api/snapshot 单通道（五块聚合）。

import { describe, it, expect, beforeEach, afterEach, vi } from 'vitest'
import { createPinia, setActivePinia } from 'pinia'
import { useGlobalStore } from './global'

// 模拟 /api/snapshot 的成功载荷：五块与后端 handlers.snapshot_payload 一一对应。
// 注意 version.webui.version 用字符串 '0.8.0'：版本号一般就是字符串（数字字面量写不了 0.6.0）
const payload = {
  generated_at: '2026-09-06T12:00:00',
  overview: {
    health: { latest: 20260814, lag_days: 0 },
    alerts: { count: 3, recent: [] },
    mcp: { total: 10, ok_rate: 90.0 },
    version: { webui: { version: '0.8.0' }, stale: false },
  },
  status: { disk: { total_gb: 100, used_gb: 40 }, webui: { version: '0.8.0', started: 'x', heartbeat: 'y' } },
  schedule: { enabled: true, times: ['15:30'] },
  warehouse: { available: true, watermark_daily: '20260904' },
  timeline: {
    days: [{ date: '20260904', sediment: null, sync: [], backups: null, alerts: { count: 0, err: 0, warn: 0 } }],
    totals: { sediment_days: 73, weeks: 14, months: 3, backups: { count: 7, last_mtime: 1e9 } },
  },
}

// 造一个"长得很像 Response"的对象：http.ts 只用到 ok / status / text 三个属性
const mockFetchResolve = (body, { ok = true, status = 200 } = {}) =>
  vi.fn().mockResolvedValue({
    ok,
    status,
    text: async () => JSON.stringify(body),
  })

beforeEach(() => {
  // 每个用例一个全新 Pinia：store 的 state 从零开始，不会把上个用例的数据带过来
  setActivePinia(createPinia())
})

afterEach(() => {
  // 撤掉全局 fetch 假实现，避免污染其他测试文件（vi.stubGlobal 的配套清理）
  vi.unstubAllGlobals()
})

describe('refresh() 成功（snapshot 单通道）', () => {
  it('写入 snapshot 并刷新各 getter', async () => {
    // 用假 fetch 返回"成功响应"，测试只关心 store 如何消费数据
    const fetchMock = mockFetchResolve(payload)
    vi.stubGlobal('fetch', fetchMock)

    const store = useGlobalStore()
    await store.refresh()

    // 单通道：一次 refresh 只发一路 /api/snapshot
    expect(fetchMock).toHaveBeenCalledTimes(1)
    expect(fetchMock.mock.calls[0][0]).toBe('/api/snapshot?days=7')
    // overview 块 getter 透传（0.8.0 起旧契约不变）
    expect(store.overview).toEqual(payload.overview)
    expect(store.alertCount).toBe(3)
    expect(store.lagDays).toBe(0)
    // 0.10.27 新增块 getter
    expect(store.status?.disk?.total_gb).toBe(100)
    expect(store.schedule?.enabled).toBe(true)
    expect(store.warehouse?.watermark_daily).toBe('20260904')
    expect(store.timelineDays).toHaveLength(1)
    expect(store.whTotals?.sediment_days).toBe(73)
    expect(store.serverVersion).toBe('0.8.0')
    // 刷新时间是真实 Date 实例（顶栏"上次刷新"依赖它）
    expect(store.lastRefresh).toBeInstanceOf(Date)
    // 成功时错误文案被清空
    expect(store.error).toBeNull()
  })
})

describe('refresh() 失败', () => {
  it('fetch 拒绝时 error 为文案、snapshot 保持 null', async () => {
    // 假 fetch 直接 reject：模拟断网 / 超时，http.ts 里 fetch(...) 会抛出原始错误
    vi.stubGlobal('fetch', vi.fn().mockRejectedValue(new Error('network down')))

    const store = useGlobalStore()
    await store.refresh()

    // refresh 内部 catch 住了异常：error 拿到文案，而不是把异常抛给调用方
    expect(typeof store.error).toBe('string')
    expect(store.error?.length).toBeGreaterThan(0)
    // 失败时数据保持初始 null，绝不出现"错误数据当成成功"的假象
    expect(store.snapshot).toBeNull()
    expect(store.overview).toBeNull()
    // 失败不算刷新成功，刷新时间不更新
    expect(store.lastRefresh).toBeNull()
  })

  it('fetch 返回 ok:false 且含 error 字段时，ApiError 文案落入 error', async () => {
    // 后端错误：HTTP 500 + 业务 error 字段；http.ts 会 new ApiError(status, data)，
    // ApiError.message 优先取 data.error —— 所以 store.error 应拿到后端文案
    vi.stubGlobal('fetch', mockFetchResolve({ error: '后端炸了' }, { ok: false, status: 500 }))

    const store = useGlobalStore()
    await store.refresh()

    expect(store.error).toBe('后端炸了')
    expect(store.error).toBeTruthy()
    expect(store.snapshot).toBeNull()
  })
})
