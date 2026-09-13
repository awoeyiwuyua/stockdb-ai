// lights.test.ts — 四灯判定纯函数单测（0.10.27「从零四件套」之二）。
// 动机：调度器存活恒假、布尔快照两个事故都出在状态推导层——推导层值得独打。
// 口径来源：W1 定义书 §2.2（改口径必须升定义书版本）；入参契约见 domain/lights.ts。
import { describe, it, expect } from 'vitest'
import { deriveLights, worstTone, aggregateWord, hasWhError24h } from './lights'

const baseInput = {
  lagDays: 0 as number | null,
  healthLatest: '2026-09-04',
  healthNote: '',
  schedule: null,
  warehouse: null,
  disk: undefined,
  whError24h: false,
}

const tone = (lights: ReturnType<typeof deriveLights>, key: string) =>
  lights.find((l) => l.key === key)!.tone

describe('数据灯（lag 口径）', () => {
  it('lag=null 灰「未知」；0 绿「最新」；1~2 黄；≥3 红', () => {
    expect(tone(deriveLights({ ...baseInput, lagDays: null }), 'data')).toBe('off')
    expect(tone(deriveLights(baseInput), 'data')).toBe('ok')
    expect(tone(deriveLights({ ...baseInput, lagDays: 2 }), 'data')).toBe('warn')
    expect(tone(deriveLights({ ...baseInput, lagDays: 3 }), 'data')).toBe('err')
  })
  it('关键值：日K 至 MM-DD（年份隐含）', () => {
    const [data] = deriveLights(baseInput)
    expect(data.value).toBe('日K 至 09-04')
  })
})

describe('同步灯（schedule 口径）', () => {
  it('未启用 灰；enabled 但从未触发 绿（已武装）', () => {
    expect(tone(deriveLights(baseInput), 'sync')).toBe('off')
    const armed = deriveLights({ ...baseInput, schedule: { enabled: true, times: ['15:30'], next_trigger: null } })
    expect(tone(armed, 'sync')).toBe('ok')
    expect(armed.find((l) => l.key === 'sync')!.state).toBe('已排定')
  })
  it('retry_pending 挂起 黄；上次 exit≠0 红', () => {
    const pending = deriveLights({ ...baseInput, schedule: { enabled: true, retry_pending: '2026-09-06 17:20:00' } })
    expect(tone(pending, 'sync')).toBe('warn')
    const failed = deriveLights({ ...baseInput, schedule: { enabled: true, last_trigger: { t: '15:50', exit: 2 } } })
    expect(tone(failed, 'sync')).toBe('err')
  })
})

describe('仓库灯（watermark 口径）', () => {
  it('available=false 灰「未接入」；无水位 灰「未沉淀」', () => {
    expect(tone(deriveLights(baseInput), 'warehouse')).toBe('off')
    const noWm = deriveLights({ ...baseInput, warehouse: { available: true, watermark_daily: null } })
    expect(tone(noWm, 'warehouse')).toBe('off')
    expect(noWm.find((l) => l.key === 'warehouse')!.state).toBe('未沉淀')
  })
  it('水位=最新 绿；落后 黄；对账失败 红', () => {
    const ok = deriveLights({ ...baseInput, warehouse: { available: true, watermark_daily: '20260904' } })
    expect(tone(ok, 'warehouse')).toBe('ok')
    const lag = deriveLights({ ...baseInput, warehouse: { available: true, watermark_daily: '20260903' } })
    expect(tone(lag, 'warehouse')).toBe('warn')
    const diff = deriveLights({ ...baseInput, warehouse: { available: true, watermark_daily: '20260904', last_result: { ok: false } } })
    expect(tone(diff, 'warehouse')).toBe('err')
  })
  it('24h 内 warehouse error 告警 → 红灯压过黄（对账失败仍是红）', () => {
    const alert = deriveLights({ ...baseInput, warehouse: { available: true, watermark_daily: '20260903' }, whError24h: true })
    expect(tone(alert, 'warehouse')).toBe('err')
    expect(alert.find((l) => l.key === 'warehouse')!.state).toBe('告警活跃')
  })
})

describe('磁盘灯（80/90 阈值）', () => {
  it('未知灰；<80 绿；80~89 黄；≥90 红', () => {
    expect(tone(deriveLights(baseInput), 'disk')).toBe('off')
    expect(tone(deriveLights({ ...baseInput, disk: { total_gb: 100, used_gb: 79 } }), 'disk')).toBe('ok')
    expect(tone(deriveLights({ ...baseInput, disk: { total_gb: 100, used_gb: 80 } }), 'disk')).toBe('warn')
    expect(tone(deriveLights({ ...baseInput, disk: { total_gb: 100, used_gb: 90 } }), 'disk')).toBe('err')
  })
})

describe('聚合', () => {
  it('worstTone：err > warn > ok > off', () => {
    expect(worstTone(['off', 'ok', 'warn'])).toBe('warn')
    expect(worstTone(['off', 'ok'])).toBe('ok')
    expect(worstTone(['err', 'warn'])).toBe('err')
    expect(worstTone(['off', 'off'])).toBe('off')
  })
  it('aggregateWord 三段自解释', () => {
    expect(aggregateWord([
      { key: 'data', label: '数据', tone: 'err', state: '', value: '' },
      { key: 'sync', label: '同步', tone: 'warn', state: '', value: '' },
    ])).toBe('故障 1 项')
    expect(aggregateWord([
      { key: 'data', label: '数据', tone: 'warn', state: '', value: '' },
      { key: 'sync', label: '同步', tone: 'warn', state: '', value: '' },
    ])).toBe('注意 2 项')
    expect(aggregateWord([
      { key: 'data', label: '数据', tone: 'ok', state: '', value: '' },
      { key: 'sync', label: '同步', tone: 'off', state: '', value: '' },
    ])).toBe('全部正常')
  })
})

describe('hasWhError24h', () => {
  // 时区无关构造：告警 ts 是「本地时间」字符串，故用本地字段拼相对时刻，
  // 不写死绝对时间（0.10.38：容器 TZ=UTC 时绝对时间假设会把 fresh/old 判反）
  const localTs = (offsetHours: number) => {
    const d = new Date(Date.now() + offsetHours * 3600 * 1000)
    const p = (n: number) => String(n).padStart(2, '0')
    return `${d.getFullYear()}-${p(d.getMonth() + 1)}-${p(d.getDate())} ` +
           `${p(d.getHours())}:${p(d.getMinutes())}:${p(d.getSeconds())}`
  }

  it('24h 内 warehouse error → true；更早 / 非 warehouse / 非 error → false', () => {
    const now = Date.now()
    const fresh = { ts: localTs(-3), source: 'warehouse', level: 'error' }
    const old = { ts: localTs(-30), source: 'warehouse', level: 'error' }
    const other = { ts: localTs(-3), source: 'sync', level: 'error' }
    const warn = { ts: localTs(-3), source: 'warehouse', level: 'warning' }
    expect(hasWhError24h([fresh], now)).toBe(true)
    expect(hasWhError24h([old], now)).toBe(false)
    expect(hasWhError24h([other], now)).toBe(false)
    expect(hasWhError24h([warn], now)).toBe(false)
    expect(hasWhError24h([], now)).toBe(false)
  })

  it('非法/缺失 ts 不误判（NaN 剔除）', () => {
    const bad = { ts: 'not-a-date', source: 'warehouse', level: 'error' }
    const missing = { source: 'warehouse', level: 'error' }
    expect(hasWhError24h([bad, missing], Date.now())).toBe(false)
  })
})
