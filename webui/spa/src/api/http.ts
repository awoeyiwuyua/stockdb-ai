// http.ts — 统一 fetch 封装：超时、JSON 解析、非 2xx 抛 ApiError、token 门禁头。
// 学习点：AbortController 超时；后端 8 错误码约定（error 字段）直接透传给调用方。
// 0.10.27：token 门禁前端半边——每次请求附 X-StockDB-Token（localStorage），401 时
// 通知监听者（App 层挂 TokenGate 登录卡片）；token 未配置的后端（默认）零影响。

export class ApiError extends Error {
  status: number
  data: unknown
  url: string
  constructor(status: number, data: unknown, url: string) {
    super(typeof (data as { error?: string })?.error === 'string'
      ? (data as { error: string }).error
      : `HTTP ${status}`)
    this.name = 'ApiError'
    this.status = status
    this.data = data
    this.url = url
  }
}

// —— token 门禁（0.10.27）：localStorage 唯一持久化口 + 内存兜底 ——
// localStorage 不可用时（隐私模式/受限 webview/测试环境）退化为会话内存，
// 刷新页面后需重新输入——门禁语义不变，只是不再免输。
const TOKEN_KEY = 'webui-token'
const TOKEN_HEADER = 'X-StockDB-Token'
let _memToken = ''

export function getToken(): string {
  try {
    return localStorage.getItem(TOKEN_KEY)?.trim() || _memToken
  } catch {
    return _memToken
  }
}

export function setToken(t: string): void {
  _memToken = t.trim()
  try {
    localStorage.setItem(TOKEN_KEY, _memToken)
  } catch { /* 存储不可用：仅本次会话生效 */ }
}

// 401 广播：App.vue 注册弹登录卡片；置 null 注销
type UnauthorizedListener = () => void
let _unauthorizedListener: UnauthorizedListener | null = null
export function onUnauthorized(fn: UnauthorizedListener | null): void {
  _unauthorizedListener = fn
}

// 把 {a:1,b:undefined,c:'x'} 编成 '?a=1&c=x'（空值自动剔除）
export function buildQuery(params: Record<string, unknown> = {}): string {
  const parts: string[] = []
  for (const [k, v] of Object.entries(params)) {
    if (v === undefined || v === null || v === '') continue
    parts.push(`${encodeURIComponent(k)}=${encodeURIComponent(String(v))}`)
  }
  return parts.length ? `?${parts.join('&')}` : ''
}

// 在途请求表（key → Promise）：同请求并发共享，防切页风暴
const _inflight = new Map<string, Promise<unknown>>()

export async function request<T = unknown>(
  path: string,
  { method = 'GET', body, timeoutMs = 20000, signal, dedup = true }: {
    method?: string
    body?: unknown
    timeoutMs?: number
    signal?: AbortSignal
    dedup?: boolean
  } = {},
): Promise<T> {
  // 在途去重：相同 method+URL+body 的并发请求共享同一个 Promise。
  // 多标签/快速切页会短时间重复发起同一批接口，去重后后端只收到一路，
  // 其余等待同一结果——避免请求风暴把 ThreadingHTTPServer 拖死。
  const dedupKey = dedup ? `${method} ${path} ${body !== undefined ? JSON.stringify(body) : ''}` : null
  if (dedupKey && _inflight.has(dedupKey)) {
    return _inflight.get(dedupKey) as Promise<T>
  }
  const p = _doRequest<T>(path, { method, body, timeoutMs, signal }).finally(() => {
    if (dedupKey) _inflight.delete(dedupKey)
  })
  if (dedupKey) _inflight.set(dedupKey, p)
  return p
}

async function _doRequest<T>(
  path: string,
  { method, body, timeoutMs, signal }: {
    method: string
    body?: unknown
    timeoutMs?: number
    signal?: AbortSignal
  },
): Promise<T> {
  const ctrl = new AbortController()
  const timer = setTimeout(() => ctrl.abort(new Error('timeout')), timeoutMs)
  try {
    const token = getToken()
    const resp = await fetch(path, {
      method,
      headers: {
        // token 门禁：配了才带（后端未启用时带头无副作用）
        ...(token ? { [TOKEN_HEADER]: token } : {}),
        ...(body !== undefined ? { 'Content-Type': 'application/json' } : {}),
      },
      body: body !== undefined ? JSON.stringify(body) : undefined,
      signal: signal || ctrl.signal,
    })
    if (resp.status === 401) _unauthorizedListener?.() // 门禁拒绝 → 弹登录卡片
    const text = await resp.text()
    let data: unknown = null
    try {
      data = text ? JSON.parse(text) : null
    } catch {
      data = null
    }
    if (!resp.ok) throw new ApiError(resp.status, data ?? {}, path)
    return data as T
  } finally {
    clearTimeout(timer)
  }
}

// 错误 → 用户可读文案（catch (e: unknown) 的统一出口，替代散落的 e?.message || fallback）
export function errText(e: unknown, fallback: string): string {
  return e instanceof Error && e.message ? e.message : fallback
}

export const getJson = <T = unknown>(path: string, opts: Parameters<typeof request>[1] = {}) =>
  request<T>(path, { ...opts, method: 'GET' })
export const postJson = <T = unknown>(path: string, body?: unknown, opts: Parameters<typeof request>[1] = {}) =>
  request<T>(path, { ...opts, method: 'POST', body })
