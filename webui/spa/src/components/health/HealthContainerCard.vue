<template>
  <!-- 容器卡（0.10.18 自 views/OpsHealth.vue 拆出）：进程状态 + 危险重启按钮 +
       容器日志懒加载抽屉。状态机在 use-health（壳持有），动作经 emit。 -->
  <section class="card">
    <!-- 卡片标题行：左标题、右操作（危险重启按钮就近放置，一眼可见） -->
    <div class="card-head">
      <h3 class="card-title">容器（stockdb 进程）</h3>
      <div class="card-actions">
        <el-button text size="small" :icon="Document" @click="emit('toggle-log')">
          {{ logOpen ? '收起容器日志' : '展开容器日志' }}
        </el-button>
        <el-button
          type="danger"
          size="small"
          :icon="RefreshRight"
          :loading="restarting"
          :disabled="!!syncRunning"
          @click="emit('restart')"
        >重启 stockdb</el-button>
      </div>
    </div>

    <!-- el-descriptions 一行表格排版进程状态/时长/镜像/备注 -->
    <el-descriptions :column="2" border size="small">
      <el-descriptions-item label="进程状态">
        <el-tag :type="container?.ok ? 'success' : 'danger'" size="small">
          {{ container?.ok ? '运行中' : '已停止' }}
        </el-tag>
        <span class="hint" style="margin-left: 6px">{{ container?.note || '' }}</span>
      </el-descriptions-item>
      <el-descriptions-item label="运行时长">
        {{ fmtUptime(container?.started) }}
      </el-descriptions-item>
      <el-descriptions-item label="状态原文">
        {{ container?.status || '—' }}
      </el-descriptions-item>
      <el-descriptions-item label="镜像">
        {{ container?.image || '—' }}
      </el-descriptions-item>
    </el-descriptions>

    <!-- 容器日志：展开才拉取（懒加载，省请求）；pre 等宽 + 深色底 -->
    <pre v-if="logOpen" class="log-pre">{{ log || '（stockdb 日志为空）' }}</pre>
  </section>
</template>

<script setup lang="ts">
import { Document, RefreshRight } from '@element-plus/icons-vue'
import { fmtUptime } from '../../utils/format'
import type { StatusPayload } from '../../types/api'

withDefaults(defineProps<{
  container?: StatusPayload['container']
  log?: string
  logOpen?: boolean
  restarting?: boolean
  // 重启按钮禁用依据：同步进行中不允许重启（后端也有锁，前端先行拦截）
  syncRunning?: boolean
}>(), {
  container: null,
  log: '',
  logOpen: false,
  restarting: false,
  syncRunning: false,
})

const emit = defineEmits<{ (e: 'toggle-log'): void; (e: 'restart'): void }>()
</script>

<style scoped>
.card-head {
  display: flex;
  align-items: center;
  justify-content: space-between;
  margin-bottom: 10px;
}
.card-head .card-title {
  margin: 0;
}
.card-actions {
  display: flex;
  align-items: center;
  gap: 8px;
}
.log-pre {
  margin: 10px 0 0;
  padding: 12px 14px;
  max-height: 300px;
  overflow: auto;
  background: var(--panel2);
  border: 1px solid var(--line-soft);
  border-radius: 14px;
  font: 12px/1.55 var(--font-mono);
  color: var(--text);
  white-space: pre-wrap;
  word-break: break-all;
}
</style>
