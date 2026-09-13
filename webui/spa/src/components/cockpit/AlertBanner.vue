<template>
  <!-- ═══════════ 告警横幅（0.10.38 §改版）═══════════
       只在"有需处理项"时出现（不再有"全部正常"占位）：把同步矩阵里散落的失败
       提到顶部，带上快捷动作（一键重试 / 查看日志），不用翻表格找 ✕。
       判定口径在 domain/timeline.ts:pendingAlerts（只认后端 needs_action，不前端重判）。 -->
  <section v-if="items.length" class="banner">
    <div class="bn-head">
      <el-icon class="bn-icon"><WarningFilled /></el-icon>
      <b>需处理 {{ total }} 项</b>
      <span class="bn-sub">最近 {{ items[0].date.slice(4, 6) }}-{{ items[0].date.slice(6, 8) }}</span>
      <div class="bn-actions">
        <el-button size="small" :loading="retrying" @click="onRetry">一键重试</el-button>
        <el-button size="small" text @click="emit('open-logs')">查看日志</el-button>
      </div>
    </div>
    <ul class="bn-list">
      <li v-for="a in items" :key="a.date" class="bn-item">
        <span class="bn-date">{{ a.date.slice(4, 6) }}-{{ a.date.slice(6, 8) }}</span>
        <span class="bn-text">{{ a.hint || a.detail || '存在未决同步异常' }}</span>
        <span v-if="a.count > 1" class="bn-count">×{{ a.count }}</span>
      </li>
    </ul>
  </section>
</template>

<script setup lang="ts">
import { computed, ref } from 'vue'
import { WarningFilled } from '@element-plus/icons-vue'
import { ElMessage } from 'element-plus'
import { runSync } from '../../api/status'
import type { PendingAlert } from '../../domain/timeline'

const props = withDefaults(defineProps<{ items?: PendingAlert[] }>(), { items: () => [] })
const emit = defineEmits<{ (e: 'open-logs'): void; (e: 'retried'): void }>()

const retrying = ref(false)
const total = computed(() => props.items.reduce((n, a) => n + (a.count || 1), 0))

/** 一键重试 = 触发一次热更新同步（POST /api/sync，与数据同步页同源接口）。
 *  注意：这是"重跑同步"，不承诺能修数据问题——真失败要看日志/按详情处理。 */
async function onRetry() {
  retrying.value = true
  try {
    await runSync()
    ElMessage.success('已触发同步，请观察矩阵轨迹')
    emit('retried')
  } catch (e) {
    ElMessage.error(`触发失败：${(e as Error).message}`)
  } finally {
    retrying.value = false
  }
}
</script>

<style scoped>
.banner {
  border: 1px solid var(--warn);
  border-radius: var(--radius-md, 8px);
  background: color-mix(in srgb, var(--warn) 8%, transparent);
  padding: 10px 14px;
  display: flex;
  flex-direction: column;
  gap: 8px;
}
.bn-head {
  display: flex;
  align-items: center;
  gap: 10px;
  font-size: 13px;
}
.bn-icon {
  color: var(--warn);
}
.bn-sub {
  color: var(--muted);
  font-variant-numeric: tabular-nums;
}
.bn-actions {
  margin-left: auto;
  display: flex;
  align-items: center;
  gap: 6px;
}
.bn-list {
  list-style: none;
  margin: 0;
  padding: 0;
  display: flex;
  flex-direction: column;
  gap: 4px;
}
.bn-item {
  display: flex;
  align-items: baseline;
  gap: 10px;
  font-size: 13px;
  color: var(--text);
}
.bn-date {
  font-variant-numeric: tabular-nums;
  color: var(--err);
  font-weight: 600;
}
.bn-text {
  word-break: break-all;
}
.bn-count {
  color: var(--muted);
  font-size: 12px;
}
</style>
