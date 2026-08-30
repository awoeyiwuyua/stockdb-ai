// use-sync.js — 数据同步页业务状态机（0.10.18 自 views/OpsSync.vue 迁入）。
//
// 承接（哲学 #2 复杂度定向转移）：4 个数据源（status/history/syncLog + 触发动作）
// 的状态与全部取数/操作逻辑，无 DOM（日志滚动锁在 SyncLogCard 组件内）。
// 取数走 api/status.js 封装（依赖方向：composable → api，渲染层不碰 fetch）。
//
// 轮询不在这里注册——由编排壳 usePolling 统一调度（可见性节拍在 use-polling）。
import { ref, computed } from 'vue'
// ElMessage / ElMessageBox 是"命令式"弹窗，不走模板组件，必须显式 import
import { ElMessage, ElMessageBox } from 'element-plus'
import { getStatus, getHistory, getLog, runSync } from '../api/status.js'

export function useSync() {
  const status = ref(null)      // /api/status 全量载荷（含 container/sync/disk/calendar...）
  const history = ref([])       // /api/history 同步历史（后端按时间正序追加，末尾最新）
  const syncLog = ref('')       // /api/log 同步日志尾部
  const loading = ref(false)    // 首拉/手动刷新中
  const error = ref(null)       // 最近一次失败文案（页面顶部 alert，不打断使用）
  const syncBusy = ref(false)   // 同步请求进行中（按钮 loading）

  // 拉 /api/status；失败把文案写进 error（页面级，不抛崩溃）
  async function loadStatus() {
    try {
      const data = await getStatus()
      if (data) {
        status.value = data
        error.value = null
      }
    } catch (e) {
      error.value = e?.message || '状态接口不可用'
    }
  }

  // 拉 /api/history：数组直接给表格（新→旧排列由模板 reverse 处理）
  async function loadHistory() {
    try {
      const data = await getHistory()
      history.value = (data?.history || []).slice().reverse()
    } catch (e) {
      error.value = e?.message || '同步历史接口不可用'
    }
  }

  // 拉 /api/log?n=80：同步日志尾部
  async function loadSyncLog() {
    try {
      const data = await getLog(80)
      syncLog.value = data?.log ?? '（暂无同步日志）'
    } catch (e) {
      error.value = e?.message || '同步日志接口不可用'
    }
  }

  // 整页刷新入口：manual=true 时显示 loading（按钮转圈），轮询静默
  async function loadAll(manual = false) {
    if (manual) loading.value = true
    try {
      await Promise.all([loadStatus(), loadHistory(), loadSyncLog()])
    } finally {
      loading.value = false
    }
  }

  // 启动同步：hot=true 热更新 / hot=false 停服严格。同步会向数据卷写入数据，
  // 属于"写入操作"，启动前必须先 ElMessageBox.confirm 二次确认（用户取消直接 return）。
  async function doSync(hot) {
    const mode = hot ? '热更新' : '停服严格同步'
    try {
      await ElMessageBox.confirm(
        `确认启动${mode}？${hot
          ? '热更新：stockdb 保持运行、增量同步后自动 reload（零中断）。'
          : '停服严格：按官方要求先停止服务再同步，期间行情服务会中断。'}`,
        '启动同步',
        { type: 'warning', confirmButtonText: '启动', cancelButtonText: '取消' },
      )
    } catch {
      return // 用户点了取消
    }
    syncBusy.value = true
    try {
      const r = await runSync(hot)
      const msg = r?.msg || '已启动同步'
      if (msg.includes('运行中')) {
        ElMessage.warning(msg) // 被定时任务占用：只提示，不做多余动作（与旧页一致）
      } else {
        ElMessage.success(msg)
        // 刚启动：立刻补拉一次状态 + 日志，不用等 30s 轮询
        await Promise.all([loadStatus(), loadSyncLog()])
      }
    } catch (e) {
      ElMessage.error(e?.message || '启动同步失败')
    } finally {
      syncBusy.value = false
    }
  }

  // 最近一次同步摘要（/api/status 里的 last_sync = 历史数组最后一条）
  const lastSync = computed(() => status.value?.last_sync ?? null)
  const lastExitCode = computed(() => status.value?.exit_code ?? null)
  const lastExitOk = computed(() => lastExitCode.value === 0)

  return {
    status, history, syncLog, loading, error, syncBusy,
    loadStatus, loadHistory, loadSyncLog, loadAll, doSync,
    lastSync, lastExitCode, lastExitOk,
  }
}
