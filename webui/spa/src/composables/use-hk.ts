// use-hk.ts — 港股日K同步域状态机（0.10.18 自 views/OpsSync.vue 迁入）。
// 等价迁移：解析代码列表 → 二次确认（写入操作） → hkSync → 拼结果行 + 分级提示。
import { ref } from 'vue'
import { ElMessage, ElMessageBox } from 'element-plus'
import { hkSync } from '../api/status'
import { errText } from '../api/http'
import { fmtYMD } from '../utils/format'

export interface HkResultRow {
  code: string
  ok: boolean
  detail: string
}

export function useHk() {
  const hkCodes = ref('')
  const hkYears = ref(2)
  const hkBusy = ref(false)
  const hkResult = ref<HkResultRow[]>([]) // 结果行：{code, ok, detail}

  // 港股同步：解析逗号/空格分隔的代码列表 → 二次确认（写入操作） → 调 hkSync → 拼结果行
  async function doHkSync() {
    const codes = hkCodes.value.split(/[,，\s]+/).map((s) => s.trim()).filter(Boolean)
    if (!codes.length) {
      ElMessage.warning('请输入港股代码（如 00700,00941）')
      return
    }
    try {
      await ElMessageBox.confirm(
        `确认开始港股同步（${codes.length} 只代码，保留最近 ${hkYears.value || 2} 年）？将写入私有表 hk日k:。`,
        '港股同步',
        { type: 'warning', confirmButtonText: '开始同步', cancelButtonText: '取消' },
      )
    } catch {
      return // 用户点了取消
    }
    hkBusy.value = true
    try {
      const r = await hkSync(codes, hkYears.value || 2)
      // 后端返回 { code: {ok, bars, latest} | {ok:false, error} } 的对象映射
      const rows: HkResultRow[] = Object.entries(r ?? {}).map(([code, v]) => ({
        code,
        ok: !!v?.ok,
        detail: v?.ok ? `写入 ${v.bars} 根日K，最新 ${fmtYMD(v.latest)}` : (v?.error || '失败'),
      }))
      hkResult.value = rows
      const failed = rows.filter((x) => !x.ok)
      if (failed.length === 0) ElMessage.success(`港股同步完成（${rows.length} 只全部成功）`)
      else if (failed.length === rows.length) ElMessage.error(`港股同步失败：${failed.map((x) => x.detail).join('；')}`)
      else ElMessage.warning(`${rows.length - failed.length} 只成功，${failed.length} 只失败（见结果表）`)
    } catch (e) {
      ElMessage.error(errText(e, '港股同步请求失败'))
    } finally {
      hkBusy.value = false
    }
  }

  return { hkCodes, hkYears, hkBusy, hkResult, doHkSync }
}
