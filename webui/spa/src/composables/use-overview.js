// use-overview.js — 总览页自管数据源状态机（0.10.18 自 views/Overview.vue 迁出，
// 第四批按 docs/design/webui.md 扩至双源）。
//
// 总览的 health/alerts/mcp/version 四块读全局 store（App 层 30s 轮询）；这里只管
// 两个页面级数据源：版本卡 getVersion() + 域灯/资产卡 getStatus()（进程/磁盘/
// code_stats/coverage/last_sync——原属同步页 payload，总览域灯行也需要）。
// SPA 单挂载特性下两页不同时在场，不构成重复轮询。轮询节拍由视图层 usePolling
// 驱动（这里不含定时器），刷新按钮的编排留在视图。
import { ref } from 'vue'
import { getVersion } from '../api/ops.js'
import { getStatus } from '../api/status.js'

export function useOverview() {
  // ver 为 null 表示「还没拿到 / 接口异常」——版本卡按三态渲染（骨架 / 不可用 / 正常）
  const ver = ref(null)
  const verLoading = ref(false)
  const verError = ref('')

  const loadVersion = async () => {
    verLoading.value = true
    try {
      ver.value = await getVersion()
      verError.value = ''
    } catch (e) {
      verError.value = e?.message || '版本接口不可用'
    } finally {
      verLoading.value = false
    }
  }

  // status 拉取静默降级：失败时保留旧值，域灯/资产卡显示 '—'（不另弹错误条，
  // 页面顶部已有 store.error 提醒全局轮询状态）
  const status = ref(null)
  const loadStatus = async () => {
    try {
      status.value = await getStatus()
    } catch {
      /* 保留旧值，灯显灰 */
    }
  }

  return { ver, verLoading, verError, loadVersion, status, loadStatus }
}
