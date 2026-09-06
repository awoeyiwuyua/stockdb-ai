// use-cockpit.js — 驾驶舱状态机（W1 单页驾驶舱，docs/design/webui-cockpit-redesign.md）。
//
// 数据源四路：数据灯读全局 store（App 层 30s 轮询，不重复建源）；同步/仓库/磁盘灯
// 页面级拉取（本文件，静默降级——取不到灯显灰 off，不弹全局错误）；轮询节拍由视图层
// usePolling 驱动（这里不含定时器）。
//
// 四灯判定口径（§2.2 写死，改口径必须升定义书版本）：
//   数据: lag=0 绿 / 1~2 黄 / ≥3 红 / 未知灰
//   同步: enabled 且最近触发 exit=0 且无 retry_pending 绿 / retry_pending 挂起 黄 /
//         最近触发 exit≠0 红 / 未启用 灰（enabled 但从未触发视为绿=已武装）
//   仓库: watermark==latest 且最近对账 ok 绿 / watermark 落后 黄 / 对账失败或
//         24h 内 warehouse error 告警 红 / warehouse 不可用 灰
//   磁盘: used<80% 绿 / <90% 黄 / ≥90% 红 / 未知灰
import { ref, computed } from 'vue'
import { getSchedule, getStatus, getWarehouseStatus } from '../api/status.js'
import { useGlobalStore } from '../stores/global.js'

const WARN_H = 80   // 磁盘黄灯阈值（使用率 %）
const ERR_H = 90    // 磁盘红灯阈值

export function useCockpit() {
  const store = useGlobalStore()

  const schedule = ref(null)   // /api/schedule → .schedule
  const status = ref(null)     // /api/status（磁盘子块）
  const warehouse = ref(null)  // /api/warehouse/status

  async function loadSchedule() {
    try {
      schedule.value = (await getSchedule())?.schedule ?? null
    } catch { /* 灯显灰 */ }
  }
  async function loadStatus() {
    try { status.value = await getStatus() } catch { /* 灯显灰 */ }
  }
  async function loadWarehouse() {
    try { warehouse.value = await getWarehouseStatus() } catch { /* 灯显灰 */ }
  }
  const loadAll = () => { loadSchedule(); loadStatus(); loadWarehouse() }

  // 24h 内的 warehouse error 告警 → 仓库红灯（schema 落后守护会重复投递，取时窗）
  const whError24h = computed(() => {
    const recent = store.overview?.alerts?.recent ?? []
    const dayAgo = Date.now() - 24 * 3600 * 1000
    return recent.some(
      (a) => a?.source === 'warehouse' && a?.level === 'error' &&
             new Date(a.ts).getTime() > dayAgo
    )
  })

  const lights = computed(() => {
    // 日期短格式：MM-DD（年份隐含，tooltip 有全值）
    const short = (d8) => (d8 && d8.length === 8 ? `${d8.slice(4, 6)}-${d8.slice(6, 8)}` : d8)

    // —— 数据灯（全局 store）——
    const lag = store.lagDays
    const data = lag == null ? 'off' : lag === 0 ? 'ok' : lag <= 2 ? 'warn' : 'err'
    const latest8 = (store.health?.latest || '').replaceAll('-', '')
    const dataState = lag == null ? '未知' : lag === 0 ? '最新' : `落后 ${lag} 天`
    const dataValue = latest8 ? `日K 至 ${short(latest8)}` : '—'

    // —— 同步灯 ——
    const sch = schedule.value
    let sync = 'off'
    let syncState = '未启用'
    let syncValue = '—'
    if (sch?.enabled) {
      if (sch.retry_pending) {
        sync = 'warn'
        syncState = '重试挂起'
        syncValue = `将于 ${sch.retry_pending.slice(11) || ''} 重试`
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
    const w = warehouse.value
    let wh = 'off'
    let whState = '未接入'
    let whValue = '—'
    if (w?.available) {
      const wm = w.watermark_daily
      const latest = (store.health?.latest || '').replaceAll('-', '')
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
    if (wh !== 'err' && whError24h.value) {
      wh = 'err'
      whState = '告警活跃'
      whValue = '24h 内 warehouse error（详见诊断）'
    }

    // —— 磁盘灯 ——
    const d = status.value?.disk
    let disk = 'off'
    let diskState = '未知'
    let diskValue = '—'
    if (d?.total_gb) {
      const pct = Math.round((d.used_gb / d.total_gb) * 100)
      disk = pct < WARN_H ? 'ok' : pct < ERR_H ? 'warn' : 'err'
      diskState = pct < WARN_H ? '充裕' : pct < ERR_H ? '偏高' : '吃紧'
      diskValue = `已用 ${pct}%（${Math.round(d.used_gb)}G）`
    }

    return [
      { key: 'data', label: '数据', tone: data, state: dataState, value: dataValue,
        detail: store.health?.note || '' },
      { key: 'sync', label: '同步', tone: sync, state: syncState, value: syncValue,
        detail: sch?.enabled ? `每日 ${JSON.stringify(sch.times)}（仅交易日）` : '未启用定时同步' },
      { key: 'warehouse', label: '仓库', tone: wh, state: whState, value: whValue,
        detail: 'Parquet 沉淀水位（批次见下方时间线）' },
      { key: 'disk', label: '磁盘', tone: disk, state: diskState, value: diskValue,
        detail: d ? `共 ${Math.round(d.total_gb)}G` : '' },
    ]
  })

  // 聚合灯 = 四灯最差色（err > warn > ok > off）
  const worst = computed(() => {
    const rank = { err: 3, warn: 2, ok: 1, off: 0 }
    const tones = lights.value.map((l) => l.tone)
    return tones.reduce((a, b) => (rank[b] > rank[a] ? b : a), 'off')
  })
  const aggWord = computed(() => {
    const errs = lights.value.filter((l) => l.tone === 'err').length
    const warns = lights.value.filter((l) => l.tone === 'warn').length
    if (errs) return `故障 ${errs} 项`
    if (warns) return `注意 ${warns} 项`
    return '全部正常'
  })

  return { lights, worst, aggWord, loadAll, schedule, status, warehouse }
}
