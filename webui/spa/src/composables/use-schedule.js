// use-schedule.js — 定时计划表单状态机（0.10.18 自 views/OpsSync.vue 迁入）。
//
// schDirty 防吞机制（等价保留）：用户改过表单（@change 只在交互时触发，程序赋值
// 不会误标脏）后，30s 轮询的 loadSchedule 不再覆盖表单，避免吞掉未保存草稿。
//
// tradingToday：由编排壳传入（computed，源自 use-sync 的 status）——本 composable
// 不重复依赖另一域的状态源。
import { ref, computed } from 'vue'
import { ElMessage, ElMessageBox } from 'element-plus'
import { getSchedule, saveSchedule } from '../api/status.js'

// 定时时间点选项：每 15 分钟一个，00:00 ~ 23:45（配合 el-select allow-create 可输入任意 HH:MM）
const TIME_OPTIONS = Array.from({ length: 96 }, (_, i) => {
  const h = String(Math.floor(i / 4)).padStart(2, '0')
  const m = String((i % 4) * 15).padStart(2, '0')
  return `${h}:${m}`
})

export { TIME_OPTIONS }

export function useSchedule({ tradingToday, onError } = {}) {
  const schedule = ref(null)    // /api/schedule 定时配置
  const schEnabled = ref(false)
  const schTimes = ref([])
  const schTrading = ref(true)
  const schSaving = ref(false)
  const schDirty = ref(false)

  // 用户手动改过表单（@change 事件只在用户交互时触发，程序赋值不会误标脏）
  function markSchDirty() {
    schDirty.value = true
  }

  // 拉 /api/schedule：配置同步到表单——但用户有未保存草稿（schDirty）时跳过，
  // 避免 30s 轮询把正在编辑的时间点/开关重置掉（旧页也有同样的防吞机制）
  async function loadSchedule() {
    try {
      const data = await getSchedule()
      if (data?.schedule) {
        schedule.value = data.schedule
        if (!schDirty.value) {
          schEnabled.value = !!data.schedule?.enabled
          schTimes.value = data.schedule?.times || []
          schTrading.value = data.schedule?.trading_only !== false
        }
      }
    } catch (e) {
      // 与旧页一致：失败文案写入页面级 error（由壳经 onError 回调落 error ref）
      onError?.(e?.message || '定时配置接口不可用')
    }
  }

  // 保存定时计划：先做客户端校验（空时间点），再二次确认（写入操作），最后调 saveSchedule
  async function saveSch() {
    if (!schTimes.value.length) {
      ElMessage.warning('至少保留一个执行时间点（HH:MM）')
      return
    }
    try {
      await ElMessageBox.confirm(
        `确认保存定时计划？将按 ${schTimes.value.length} 个时间点、${schTrading.value ? '仅交易日' : '每天'} 触发。`,
        '保存定时计划',
        { type: 'warning', confirmButtonText: '保存', cancelButtonText: '取消' },
      )
    } catch {
      return // 用户点了取消
    }
    schSaving.value = true
    try {
      const r = await saveSchedule(schEnabled.value, schTimes.value, schTrading.value)
      ElMessage.success(r?.msg || '定时计划已保存')
      // 后端返回最新 schedule，直接同步回来（含 next_trigger 等派生字段）
      if (r?.schedule) {
        schedule.value = r.schedule
        schEnabled.value = !!r.schedule?.enabled
        schTimes.value = r.schedule.times || []
        schTrading.value = r.schedule.trading_only !== false
      }
      schDirty.value = false // 保存成功 = 草稿已落盘，恢复轮询同步
    } catch (e) {
      ElMessage.error(e?.message || '保存失败')
    } finally {
      schSaving.value = false
    }
  }

  // 定时提示：启用 + 仅交易日 且 今天不是交易日 → 提示会跳过
  // tradingToday 支持传 getter 函数（推荐，壳里 () => status.value?.trading_today）
  const _tradingToday = () => (typeof tradingToday === 'function' ? tradingToday() : tradingToday?.value)
  const schTodayNote = computed(() => {
    if (!schEnabled.value || !schTrading.value) return ''
    const t = _tradingToday()
    if (t == null) return ''
    return t ? '' : '今日非交易日，定时将跳过'
  })

  return {
    schedule, schEnabled, schTimes, schTrading, schSaving, schDirty,
    markSchDirty, loadSchedule, saveSch, schTodayNote,
  }
}
