<template>
  <!-- 定时计划表单（0.10.18 自 views/OpsSync.vue 拆出）。纯展示：状态机在
       composables/use-schedule.js，本组件只做 props↔emit 双向透传。 -->
  <section class="card">
    <h3 class="card-title">定时自动同步</h3>
    <div class="sch-row">
      <el-switch
        :model-value="enabled"
        active-text="启用定时"
        @update:model-value="emit('update:enabled', $event)"
        @change="emit('mark-dirty')"
      />
      <el-switch
        :model-value="trading"
        active-text="仅交易日触发"
        @update:model-value="emit('update:trading', $event)"
        @change="emit('mark-dirty')"
      />
      <span v-if="todayNote" class="hint">{{ todayNote }}</span>
    </div>
    <div class="sch-row">
      <span class="sch-label">执行时间点（可多选，也可直接输入 HH:MM 回车添加）：</span>
      <el-select
        :model-value="times"
        multiple
        filterable
        allow-create
        default-first-option
        collapse-tags
        placeholder="选择或输入时间点"
        style="width: 380px"
        @update:model-value="emit('update:times', $event)"
        @change="emit('mark-dirty')"
      >
        <el-option v-for="t in TIME_OPTIONS" :key="t" :label="t" :value="t" />
      </el-select>
      <!-- 未保存提示：有草稿时 30s 轮询不再覆盖表单，避免吞掉用户输入 -->
      <span v-if="dirty" class="hint" style="color: var(--warn)">有未保存的修改，保存前轮询不覆盖表单</span>
    </div>
    <div class="sch-info hint">
      下次触发：{{ schedule?.next_trigger || '—' }}
      <template v-if="schedule?.last_trigger?.ts">
        ｜ 上次触发：{{ schedule.last_trigger.ts }}
        {{ schedule.last_trigger.exit === 0 ? '✅' : schedule.last_trigger.exit == null ? '⏳' : '❌' }}
      </template>
    </div>
    <el-button type="primary" :loading="saving" @click="emit('save')">保存定时计划</el-button>
  </section>
</template>

<script setup lang="ts">
// TIME_OPTIONS 从 composable 导入（单一来源：选项生成逻辑不复制两份）
import { TIME_OPTIONS } from '../../composables/use-schedule'
import type { ScheduleInfo } from '../../types/api'

withDefaults(defineProps<{
  schedule?: ScheduleInfo | null
  enabled?: boolean
  times?: string[]
  trading?: boolean
  saving?: boolean
  dirty?: boolean
  todayNote?: string
}>(), {
  schedule: null,
  enabled: false,
  times: () => [],
  trading: true,
  saving: false,
  dirty: false,
  todayNote: '',
})

const emit = defineEmits<{
  (e: 'update:enabled', v: boolean): void
  (e: 'update:times', v: string[]): void
  (e: 'update:trading', v: boolean): void
  (e: 'mark-dirty'): void
  (e: 'save'): void
}>()
</script>

<style scoped>
.sch-row {
  display: flex;
  align-items: center;
  gap: 12px;
  margin-bottom: 10px;
  flex-wrap: wrap;
}
.sch-label {
  font-size: 13px;
  color: var(--text);
}
.sch-info {
  margin-bottom: 12px;
}
</style>
