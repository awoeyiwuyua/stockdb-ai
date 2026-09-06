<template>
  <!-- 同步历史表（0.10.18 自 views/OpsSync.vue 拆出）。纯展示：props 进，无自取数。 -->
  <section class="card">
    <h3 class="card-title">同步历史</h3>
    <!-- 空态：还没有任何同步记录 -->
    <EmptyState
      v-if="!history.length"
      icon="Clock"
      title="暂无同步历史"
      description="启动一次同步后，这里会记录每次任务的触发来源 / 模式 / 结果 / 耗时。"
    />
    <el-table v-else :data="history" size="small" border>
      <!-- 展开列：点行首箭头看失败原因 / 警告等详情（旧页 historyRow 的点击展开） -->
      <el-table-column type="expand">
        <template #default="{ row }">
          <div class="hist-detail">
            <div v-if="row.reason">失败原因：{{ row.reason }}</div>
            <div v-if="row.warn">⚠ 警告：{{ row.warn }}</div>
            <div v-if="row.deletes != null">删除文件：{{ row.deletes }} 个</div>
            <div v-if="!row.reason && !row.warn && row.deletes == null" class="hint">（无额外详情）</div>
          </div>
        </template>
      </el-table-column>
      <el-table-column prop="ts" label="时间" width="170" />
      <el-table-column label="触发" width="110">
        <template #default="{ row }">{{ TRIGGER_LABEL[row.trigger] || row.trigger }}</template>
      </el-table-column>
      <el-table-column label="模式" width="110">
        <template #default="{ row }">{{ row.mode === 'strict' ? '严格(停服)' : '热更新' }}</template>
      </el-table-column>
      <el-table-column label="结果" width="90">
        <template #default="{ row }">
          <el-tag :type="resultTagType(row)" size="small">{{ resultText(row) }}</el-tag>
        </template>
      </el-table-column>
      <el-table-column prop="downloads" label="下载" width="70">
        <template #default="{ row }">{{ row.downloads ?? '—' }}</template>
      </el-table-column>
      <el-table-column label="校验" width="80">
        <template #default="{ row }">{{ VERIFIED_LABEL[row.verified] || '—' }}</template>
      </el-table-column>
      <el-table-column label="耗时" width="90">
        <template #default="{ row }">{{ row.duration_sec != null ? `${row.duration_sec}s` : '—' }}</template>
      </el-table-column>
      <el-table-column label="数据最新" min-width="100">
        <template #default="{ row }">{{ fmtYMD(row.data_latest) }}</template>
      </el-table-column>
    </el-table>
  </section>
</template>

<script setup lang="ts">
import EmptyState from '../EmptyState.vue'
import { fmtYMD } from '../../utils/format'
import type { SyncHistoryRow } from '../../types/api'

withDefaults(defineProps<{ history?: SyncHistoryRow[] }>(), { history: () => [] })

// 触发来源 → 中文（后端 trigger 字段）
const TRIGGER_LABEL: Record<string, string> = { scheduled: '⏰ 定时', 'scheduled-retry': '↻ 定时·重试', manual: '手动' }
// 校验结果 → 中文（后端 verified 字段）
const VERIFIED_LABEL: Record<string, string> = { pass: '通过', fail: '失败', skipped: '跳过' }

// 结果标签类型：成功/未生效/运行中/失败 四态
function resultTagType(row: SyncHistoryRow): 'success' | 'warning' | 'info' | 'danger' {
  if (row.exit_code === 0) return row.warn ? 'warning' : 'success'
  if (row.exit_code == null) return 'info'
  return 'danger'
}
function resultText(row: SyncHistoryRow): string {
  if (row.exit_code === 0) return row.warn ? '未生效' : '成功'
  if (row.exit_code == null) return '运行中'
  return '失败'
}
</script>

<style scoped>
.hist-detail {
  padding: 4px 12px;
  font-size: 12px;
  color: var(--muted);
  line-height: 1.7;
}
</style>
