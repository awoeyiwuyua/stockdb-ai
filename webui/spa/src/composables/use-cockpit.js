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
    // —— 数据灯（全局 store）——
    const lag = store.lagDays
    const data = lag == null ? 'off' : lag === 0 ? 'ok' : lag <= 2 ? 'warn' : 'err'
    const dataDetail = store.health?.latest
      ? `${store.health.latest}（滞后 ${lag ?? '—'} 天）`
      : '探针不可用'

    // —— 同步灯 ——
    const sch = schedule.value
    let sync = 'off'
    let syncDetail = '未启用定时'
    if (sch?.enabled) {
      if (sch.retry_pending) {
        sync = 'warn'
        syncDetail = `重试挂起 ${sch.retry_pending}`
      } else if (sch.last_trigger && sch.last_trigger.exit !== 0) {
        sync = 'err'
        syncDetail = `最近触发失败（${sch.last_trigger.t || ''}）`
      } else {
        sync = 'ok'
        syncDetail = sch.next_trigger ? `下次 ${sch.next_trigger}` : '已武装'
      }
    }

    // —— 仓库灯 ——
    const w = warehouse.value
    let wh = 'off'
    let whDetail = 'warehouse 不可用'
    if (w?.available) {
      const wm = w.watermark_daily
      const latest = (store.health?.latest || '').replaceAll('-', '')
      if (w.last_result?.ok === false) {
        wh = 'err'
        whDetail = '最近沉淀对账失败'
      } else if (!wm) {
        wh = 'off'
        whDetail = '尚无沉淀'
      } else if (latest && wm < latest) {
        wh = 'warn'
        whDetail = `水位 ${wm} 落后于数据 ${latest}`
      } else {
        wh = 'ok'
        whDetail = `水位 ${wm}`
      }
    }
    if (wh !== 'err' && whError24h.value) {
      wh = 'err'
      whDetail = '24h 内 warehouse error 告警（schema 落后/沉淀失败）'
    }

    // —— 磁盘灯 ——
    const d = status.value?.disk
    let disk = 'off'
    let diskDetail = '—'
    if (d?.total_gb) {
      const pct = Math.round((d.used_gb / d.total_gb) * 100)
      disk = pct < WARN_H ? 'ok' : pct < ERR_H ? 'warn' : 'err'
      diskDetail = `已用 ${pct}%`
    }

    return [
      { key: 'data', label: '数据', tone: data, detail: dataDetail },
      { key: 'sync', label: '同步', tone: sync, detail: syncDetail },
      { key: 'warehouse', label: '仓库', tone: wh, detail: whDetail },
      { key: 'disk', label: '磁盘', tone: disk, detail: diskDetail },
    ]
  })

  // 聚合灯 = 四灯最差色（err > warn > ok > off）
  const worst = computed(() => {
    const rank = { err: 3, warn: 2, ok: 1, off: 0 }
    const tones = lights.value.map((l) => l.tone)
    return tones.reduce((a, b) => (rank[b] > rank[a] ? b : a), 'off')
  })
  const aggWord = computed(
    () => ({ ok: '正常', warn: '注意', err: '有故障', off: '未知' })[worst.value]
  )

  return { lights, worst, aggWord, loadAll, schedule, status, warehouse }
}
