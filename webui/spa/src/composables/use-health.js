// use-health.js — 系统健康页状态机（0.10.18 自 views/OpsHealth.vue 迁入；
// 第五批按归属表收敛为双源：diag 环境源随环境卡移除，其唯一的家在诊断中心）。
//
// 双源状态（health/status）并行拉取、各自降级；busy 互斥防轮询请求堆积；
// 容器日志懒加载（展开才拉）；重启为危险操作二次确认。无 DOM。
import { ref, computed } from 'vue'
import { ElMessage, ElMessageBox } from 'element-plus'
import { getHealth, getStatus, getContainerLogs, restartContainer } from '../api/status.js'

// 秒数 → 'X天X时X分'（容器卡运行时长用；不足 1 小时只显示分钟）
function fmtUptimeSec(sec) {
  if (sec == null || !Number.isFinite(sec) || sec < 0) return '—'
  const s = Math.floor(sec)
  const d = Math.floor(s / 86400)
  const h = Math.floor((s % 86400) / 3600)
  const m = Math.floor((s % 3600) / 60)
  return (d ? `${d}天` : '') + (h ? `${h}时` : '') + `${m}分`
}

export function useHealth() {
  const health = ref(null)          // /api/health：数据健康卡
  const status = ref(null)          // /api/status：容器/磁盘（取用其中子块）
  const containerLog = ref('')      // /api/container/logs 容器日志文本
  const containerLogOpen = ref(false) // 容器日志展开开关（懒加载）
  const loading = ref(true)         // 首拉/手动刷新中（骨架屏依据）
  const error = ref('')             // 最近一次失败文案（顶部弱提示）
  const restarting = ref(false)     // 重启请求进行中（按钮 loading）

  // 互斥：上一轮还没回来就跳过本轮，避免轮询请求堆积
  let busy = false

  // 是否已有任何数据：决定骨架屏 / 错误空态 / 正常态的分支走向
  const hasData = computed(() => health.value != null || status.value != null)
  // 容器子块（status.container），取不到给 null，模板里 ?. 兜底
  const container = computed(() => status.value?.container ?? null)

  // 滞后着色：≤1 天正常（ok）/ 2 天警告 / >2 天错误；未知（null）不给色
  const healthTone = computed(() => {
    const lag = health.value?.lag_days
    if (lag == null) return ''
    if (lag <= 1) return 'ok'
    if (lag === 2) return 'warn'
    return 'err'
  })
  // 健康状态文案与颜色：ok→正常 / stale→落后 / unknown→未知
  const statusLabel = computed(
    () => ({ ok: '正常', stale: '落后', unknown: '未知' })[health.value?.status] ?? '—'
  )
  const statusTone = computed(() => {
    const st = health.value?.status
    if (st === 'ok') return 'ok'
    if (st === 'stale') return 'warn'
    return 'err' // unknown：拿不到日期属于异常，标红提醒
  })

  // 磁盘：百分比（el-progress 需要 0~100 整数）；total 缺失/为 0 时给 null（不画条）
  const diskPct = computed(() => {
    const d = status.value?.disk
    if (!d || d.total_gb == null || !d.total_gb) return null
    return Math.round((d.used_gb / d.total_gb) * 100)
  })
  // 颜色走语义 CSS 变量（随主题自动切换）：>80% 红 / >60% 黄 / 否则绿
  const diskColor = computed(() =>
    diskPct.value > 80 ? 'var(--err)' : diskPct.value > 60 ? 'var(--warn)' : 'var(--ok)'
  )
  // 磁盘明细文案（note）：已用 / 共 / 可用
  const diskText = computed(() => {
    const d = status.value?.disk
    if (!d || d.total_gb == null) return ''
    return `已用 ${d.used_gb ?? '?'} GB / 共 ${d.total_gb} GB · 可用 ${d.free_gb ?? '?'} GB`
  })

  // 两个接口并行拉取、各自降级：单块失败只记 error，不影响其它卡片。
  async function loadHealth() {
    try {
      const data = await getHealth()
      if (data) health.value = data
      return true
    } catch (e) {
      error.value = e?.message || '健康接口不可用'
      return false
    }
  }
  async function loadStatus() {
    try {
      const data = await getStatus()
      if (data) status.value = data
      return true
    } catch (e) {
      error.value = e?.message || '状态接口不可用'
      return false
    }
  }

  // 整页刷新入口：manual=true 时按钮转圈（首拉/手动）；轮询静默
  async function loadAll(manual = false) {
    if (busy) return
    busy = true
    if (manual) loading.value = true
    try {
      const results = await Promise.all([loadHealth(), loadStatus()])
      // 两块全部成功才清错误文案（有旧数据时页面继续展示，只留顶部弱提示）
      if (results.every(Boolean)) error.value = ''
    } finally {
      loading.value = false
      busy = false
    }
  }

  // 容器日志：展开时才拉取（懒加载）；收起时保留旧内容；每次展开刷新一次
  async function toggleContainerLog() {
    containerLogOpen.value = !containerLogOpen.value
    if (containerLogOpen.value) {
      try {
        const r = await getContainerLogs(150)
        containerLog.value = r?.log || ''
        if (r?.error) containerLog.value += `\n\n${r.error}`
      } catch (e) {
        ElMessage.error(e?.message || '读取容器日志失败')
      }
    }
  }

  // 重启 stockdb：危险操作，ElMessageBox.confirm 二次确认（取消直接 return）
  async function doRestart() {
    try {
      await ElMessageBox.confirm(
        '确定重启 stockdb 进程？重启期间行情服务会短暂中断，建议避开交易时段执行。',
        '危险操作',
        { type: 'warning', confirmButtonText: '重启', cancelButtonText: '取消' },
      )
    } catch {
      return // 用户点了取消：什么都不做
    }
    restarting.value = true
    try {
      const r = await restartContainer()
      ElMessage.success(r?.msg || '已发送重启')
      // 立即刷新进程状态（后端容器探测有 5s 缓存，随后轮询继续跟进）
      await loadStatus()
    } catch (e) {
      ElMessage.error(e?.message || '重启失败')
    } finally {
      restarting.value = false
    }
  }

  // epoch 秒（进程启动时间）→ 'X天X时X分'（容器卡用）
  function fmtUptime(started) {
    if (!started) return '—'
    return fmtUptimeSec(Date.now() / 1000 - started)
  }

  return {
    health, status, containerLog, containerLogOpen, loading, error, restarting,
    hasData, container, healthTone, statusLabel, statusTone,
    diskPct, diskColor, diskText,
    loadAll, toggleContainerLog, doRestart, fmtUptime,
  }
}
