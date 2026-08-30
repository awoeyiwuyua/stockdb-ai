<template>
  <!-- 磁盘卡（0.10.18 第四批，原「状态总览」磁盘块独立成分区）：数据卷用量容量条，
       >80% 变红提醒扩容。颜色走语义 CSS 变量（--err/--warn/--ok），随主题切换。 -->
  <section class="card">
    <div class="disk-head">
      <h3 class="card-title">磁盘</h3>
      <span class="hint">{{ diskText }}</span>
    </div>
    <el-progress
      v-if="diskPct != null"
      :percentage="diskPct"
      :stroke-width="12"
      :color="diskPct > 80 ? 'var(--err)' : diskPct > 60 ? 'var(--warn)' : 'var(--ok)'"
      :format="() => `${diskPct}%`"
    />
    <div v-else class="hint">磁盘信息不可用（shutil.disk_usage 失败时后端返回 null 字段）</div>
  </section>
</template>

<script setup>
import { computed } from 'vue'

const props = defineProps({
  status: { type: Object, default: null },
})

// 磁盘：百分比 + 文字（el-progress 需要 0~100 整数）
const diskPct = computed(() => {
  const d = props.status?.disk
  if (!d || d.total_gb == null || !d.total_gb) return null
  return Math.round((d.used_gb / d.total_gb) * 100)
})
const diskText = computed(() => {
  const d = props.status?.disk
  if (!d || d.total_gb == null) return '—'
  return `${d.used_gb} GB / ${d.total_gb} GB · ${d.free_gb ?? '?'} GB 可用`
})
</script>

<style scoped>
.disk-head {
  display: flex;
  justify-content: space-between;
  align-items: baseline;
  gap: 8px;
  flex-wrap: wrap;
}
</style>
