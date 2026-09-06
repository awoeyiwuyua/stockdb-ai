// http.test.js — 在途去重 + token 门禁头 + 401 广播（0.10.27）。
import { describe, it, expect, vi, afterEach, beforeEach } from 'vitest'
import { request, getJson, buildQuery, setToken, getToken, onUnauthorized } from './http'

afterEach(() => {
  vi.unstubAllGlobals()
  onUnauthorized(null)
  setToken('') // 清 token，防用例间串扰
})

const okResp = (payload) => ({
  ok: true,
  status: 200,
  text: async () => JSON.stringify(payload),
})

describe('request 在途去重', () => {
  it('并发同 URL 共享一次 fetch，结果一致', async () => {
    const fetchMock = vi.fn(async () => okResp({ v: 1 }))
    vi.stubGlobal('fetch', fetchMock)
    const [a, b] = await Promise.all([getJson('/api/snapshot'), getJson('/api/snapshot')])
    expect(fetchMock).toHaveBeenCalledTimes(1)
    expect(a).toEqual({ v: 1 })
    expect(b).toEqual({ v: 1 })
  })

  it('完成后再次请求会重新发起（去重表已清理）', async () => {
    const fetchMock = vi.fn(async () => okResp({ v: 2 }))
    vi.stubGlobal('fetch', fetchMock)
    await getJson('/api/health')
    await getJson('/api/health')
    expect(fetchMock).toHaveBeenCalledTimes(2)
  })

  it('dedup=false 时并发不共享', async () => {
    const fetchMock = vi.fn(async () => okResp({ v: 3 }))
    vi.stubGlobal('fetch', fetchMock)
    await Promise.all([
      request('/api/health', { method: 'GET', dedup: false }),
      request('/api/health', { method: 'GET', dedup: false }),
    ])
    expect(fetchMock).toHaveBeenCalledTimes(2)
  })

  it('buildQuery 剔除空值', () => {
    expect(buildQuery({ a: 1, b: '', c: undefined })).toBe('?a=1')
  })
})

describe('token 门禁（0.10.27）', () => {
  beforeEach(() => {
    setToken('') // 每用例从无 token 开始
  })

  it('未设置 token：请求头不带 X-StockDB-Token', async () => {
    const fetchMock = vi.fn(async () => okResp({}))
    vi.stubGlobal('fetch', fetchMock)
    await getJson('/api/status')
    const headers = fetchMock.mock.calls[0][1].headers
    expect(headers['X-StockDB-Token']).toBeUndefined()
  })

  it('setToken 后：请求头携带令牌（首尾空白被剔除）', async () => {
    setToken('  sekrit  ')
    expect(getToken()).toBe('sekrit')
    const fetchMock = vi.fn(async () => okResp({}))
    vi.stubGlobal('fetch', fetchMock)
    await getJson('/api/status')
    const headers = fetchMock.mock.calls[0][1].headers
    expect(headers['X-StockDB-Token']).toBe('sekrit')
  })

  it('401 响应：广播 onUnauthorized 并抛 ApiError', async () => {
    const fetchMock = vi.fn(async () => ({
      ok: false,
      status: 401,
      text: async () => JSON.stringify({ error: 'unauthorized：缺少或错误的 X-StockDB-Token' }),
    }))
    vi.stubGlobal('fetch', fetchMock)
    const spy = vi.fn()
    onUnauthorized(spy)
    await expect(getJson('/api/status')).rejects.toThrow('unauthorized')
    expect(spy).toHaveBeenCalledTimes(1)
  })

  it('非 401 的错误响应不触发广播', async () => {
    const fetchMock = vi.fn(async () => ({
      ok: false,
      status: 500,
      text: async () => JSON.stringify({ error: 'internal error' }),
    }))
    vi.stubGlobal('fetch', fetchMock)
    const spy = vi.fn()
    onUnauthorized(spy)
    await expect(getJson('/api/status')).rejects.toThrow('internal error')
    expect(spy).not.toHaveBeenCalled()
  })
})
