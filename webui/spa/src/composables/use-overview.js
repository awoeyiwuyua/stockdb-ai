// use-overview.js — 总览页「版本」独立数据源状态机（0.10.18 第三批自 views/Overview.vue 迁出）。
//
// 总览其余数据（健康/告警/MCP/版本 StatCard）全部读全局 store（App 层 30s 轮询），
// 本文件只管版本卡自己的 getVersion() 拉取：三态 refs + 加载函数。轮询节拍由
// 视图层 usePolling 驱动（这里不含定时器），刷新按钮的编排留在视图。
import { ref } from 'vue'
import { getVersion } from '../api/ops.js'

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

  return { ver, verLoading, verError, loadVersion }
}
