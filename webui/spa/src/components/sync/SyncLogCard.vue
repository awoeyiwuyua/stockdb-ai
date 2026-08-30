<template>
  <!-- 同步日志卡（0.10.18 自 views/OpsSync.vue 拆出）。
       DOM 滚动职责锁在本组件内（哲学 #3：logEl 不再泄漏到页面层）。 -->
  <section class="card">
    <h3 class="card-title">
      同步日志
      <el-button text size="small" :icon="Bottom" @click="scrollLogBottom">回到底部</el-button>
    </h3>
    <!-- 日志为空时的占位；正常时 pre 等宽字体展示，30s 轮询自动追加 -->
    <pre v-if="!log" class="log-pre hint">（暂无同步日志）</pre>
    <pre v-else ref="logEl" class="log-pre">{{ log }}</pre>
  </section>
</template>

<script setup>
import { ref, nextTick, watch } from 'vue'
import { Bottom } from '@element-plus/icons-vue'

const props = defineProps({
  log: { type: String, default: '' },
})

const logEl = ref(null) // 日志 pre 的 DOM 引用（自动滚动用）

// 数据更新后把滚动条拉到底部（日志是往下长的，用户通常要看最新）
watch(() => props.log, async () => {
  await nextTick()
  if (logEl.value) logEl.value.scrollTop = logEl.value.scrollHeight
})

// 日志滚动回底部（手动按钮）
function scrollLogBottom() {
  if (logEl.value) logEl.value.scrollTop = logEl.value.scrollHeight
}
</script>

<style scoped>
/* 日志 pre：等宽字体 + 固定高度内部滚动，深色底与旧面板观感一致 */
.log-pre {
  margin: 8px 0 0;
  padding: 10px;
  max-height: 260px;
  overflow: auto;
  background: var(--panel2);
  border: 1px solid var(--line);
  border-radius: 8px;
  font: 12px/1.5 ui-monospace, 'SF Mono', Menlo, Consolas, monospace;
  color: var(--text);
  white-space: pre-wrap;
  word-break: break-all;
}
</style>
