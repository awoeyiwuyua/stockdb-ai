// domain/lights.ts — 四灯判定纯函数（W1 定义书 §2.2 口径；0.10.27 自 use-cockpit 抽出）。
//
// 抽出动机（0.10.27「从零四件套」之二）：调度器存活恒假、布尔快照两个事故都出在
// 状态推导层而非渲染层——推导层值得独打。本模块零 Vue 依赖（入参 → 出灯），Vitest
// 直测口径；use-cockpit 只做"从 store 取数 → 喂进来"的接线。
//
// 四灯判定口径（§2.2 写死，改口径必须升定义书版本）：
//   数据: lag=0 绿 / 1~2 黄 / ≥3 红 / 未知灰
//   同步: enabled 且最近触发 exit=0 且无 retry_pending 绿 / retry_pending 挂起 黄 /
//         最近触发 exit≠0 红 / 未启用 灰（enabled 但从未触发视为绿=已武装）
//   仓库: watermark==latest 且最近对账 ok 绿 / watermark 落后 黄 / 对账失败或
//         24h 内 warehouse error 告警 红 / warehouse 不可用 灰
//   磁盘: used<80% 绿 / <90% 黄 / ≥90% 红 / 未知灰
import type { ScheduleInfo, StatusPayload, WarehouseStatus, AlertItem } from '../types/api'
import type { Light, LightTone } from '../types/ui'

const WARN_H = 80   // 磁盘黄灯阈值（使用率 %）
const ERR_H = 90    // 磁盘红灯阈值

// 日期短格式：MM-DD（年份隐含，tooltip 有全值）
const short = (d8: string) => (d8 && d8.length === 8 ? `${d8.slice(4, 6)}-${d8.slice(6, 8)}` : d8)

const ymd = (v: string | null | undefined) => (v || '').replaceAll('-', '')

export interface LightsInput {
  lagDays: number | null            // store.lagDays（数据灯）
  healthLatest: string              // store.health?.latest ?? ''
  healthNote: string                // store.health?.note ?? ''
  schedule: ScheduleInfo | null     // snapshot.schedule（同步灯）
  warehouse: WarehouseStatus | null // snapshot.warehouse（仓库灯）
  disk: StatusPayload['disk']       // snapshot.status.disk（磁盘灯）
  whError24h: boolean               // 24h 内 warehouse error 告警（仓库灯红灯位）
}

export function deriveLights(inp: LightsInput): Light[] {
  // —— 数据灯 ——
  const lag = inp.lagDays
  const data = lag == null ? 'off' : lag === 0 ? 'ok' : lag <= 2 ? 'warn' : 'err'
  const latest8 = ymd(inp.healthLatest)
  const dataState = lag == null ? '未知' : lag === 0 ? '最新' : `落后 ${lag} 天`
  const dataValue = latest8 ? `日K 至 ${short(latest8)}` : '—'

  // —— 同步灯 ——
  const sch = inp.schedule
  let sync: LightTone = 'off'
  let syncState = '未启用'
  let syncValue = '—'
  if (sch?.enabled) {
    if (sch.retry_pending) {
      sync = 'warn'
      syncState = '重试挂起'
      syncValue = `将于 ${String(sch.retry_pending).slice(11) || ''} 重试`
    } else if (sch.last_trigger && sch.last_trigger.exit !== 0) {
      sync = 'err'
      syncState = '上次失败'
      syncValue = `最近触发 ${sch.last_trigger.t || '—'}`
    } else {
      sync = 'ok'
      syncState = '已排定'
      syncValue = `下次 ${sch.next_trigger || '—'}`
    }
  }

  // —— 仓库灯 ——
  const w = inp.warehouse
  let wh: LightTone = 'off'
  let whState = '未接入'
  let whValue = '—'
  if (w?.available) {
    const wm = w.watermark_daily
    const latest = ymd(inp.healthLatest)
    if (w.last_result?.ok === false) {
      wh = 'err'
      whState = '对账差异'
      whValue = wm ? `水位 ${short(wm)}` : '—'
    } else if (!wm) {
      wh = 'off'
      whState = '未沉淀'
      whValue = '—'
    } else if (latest && wm < latest) {
      wh = 'warn'
      whState = '落后'
      whValue = `水位 ${short(wm)} < 数据 ${short(latest)}`
    } else {
      wh = 'ok'
      whState = '正常'
      whValue = `水位 ${short(wm)}`
    }
  }
  if (wh !== 'err' && inp.whError24h) {
    wh = 'err'
    whState = '告警活跃'
    whValue = '24h 内 warehouse error（详见诊断）'
  }

  // —— 磁盘灯 ——
  const d = inp.disk
  let disk: LightTone = 'off'
  let diskState = '未知'
  let diskValue = '—'
  if (d?.total_gb) {
    const pct = Math.round(((d.used_gb ?? 0) / d.total_gb) * 100)
    disk = pct < WARN_H ? 'ok' : pct < ERR_H ? 'warn' : 'err'
    diskState = pct < WARN_H ? '充裕' : pct < ERR_H ? '偏高' : '吃紧'
    diskValue = `已用 ${pct}%（${Math.round(d.used_gb ?? 0)}G）`
  }

  return [
    { key: 'data', label: '数据', tone: data, state: dataState, value: dataValue,
      detail: inp.healthNote || '' },
    { key: 'sync', label: '同步', tone: sync, state: syncState, value: syncValue,
      detail: sch?.enabled ? `每日 ${JSON.stringify(sch.times)}（仅交易日）` : '未启用定时同步' },
    { key: 'warehouse', label: '仓库', tone: wh, state: whState, value: whValue,
      detail: 'Parquet 沉淀水位（批次见下方时间线）' },
    { key: 'disk', label: '磁盘', tone: disk, state: diskState, value: diskValue,
      detail: d ? `共 ${Math.round(d.total_gb ?? 0)}G` : '' },
  ]
}

// 聚合灯 = 多灯最差色（err > warn > ok > off）
export function worstTone(tones: LightTone[]): LightTone {
  const rank: Record<LightTone, number> = { err: 3, warn: 2, ok: 1, off: 0 }
  return tones.reduce((a, b) => (rank[b] > rank[a] ? b : a), 'off')
}

// 聚合词（§2.2 三段自解释）：有故障报故障数，其次注意数，否则全部正常
export function aggregateWord(lights: Light[]): string {
  const errs = lights.filter((l) => l.tone === 'err').length
  const warns = lights.filter((l) => l.tone === 'warn').length
  if (errs) return `故障 ${errs} 项`
  if (warns) return `注意 ${warns} 项`
  return '全部正常'
}

// 后端告警时间戳解析：格式为「YYYY-MM-DD HH:MM:SS」（本地时间，无时区标记）。
// 注意：`new Date('2026-09-06 15:00:00')` 在浏览器里按**本地时区**解释，但同一份
// 代码跑在 UTC（容器/CI）与 +08（用户机）会得到不同 epoch——0.10.38 的 Docker
// 构建（容器 TZ=UTC）把 lights.test.ts 里的时区假设打爆后发现的。这里统一按本地
// 解释（与后端写入时一致），非法值返回 NaN 由调用方剔除。
export function parseLocalTs(ts: unknown): number {
  if (typeof ts !== 'string' || !ts) return NaN
  const m = ts.match(/^(\d{4})-(\d{2})-(\d{2})[T ](\d{2}):(\d{2}):(\d{2})/)
  if (!m) return new Date(ts).getTime()
  return new Date(
    Number(m[1]), Number(m[2]) - 1, Number(m[3]),
    Number(m[4]), Number(m[5]), Number(m[6]),
  ).getTime()
}

// 24h 内 warehouse error 告警（schema 落后守护会重复投递，取时窗）——入参告警列表
export function hasWhError24h(recent: AlertItem[], now = Date.now()): boolean {
  const dayAgo = now - 24 * 3600 * 1000
  return recent.some((a) => {
    if (a?.source !== 'warehouse' || a?.level !== 'error') return false
    const at = parseLocalTs(a.ts)
    return Number.isFinite(at) && at > dayAgo
  })
}

// 供类型复用（types/ui 只放纯类型，无逻辑）
export type { Light, LightTone }
