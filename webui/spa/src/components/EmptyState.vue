<template>
  <!-- 空态占位：图标进雾面圆盘（Apple 空状态范式），整体居中排版 -->
  <div class="empty-state">
    <div class="empty-icon-wrap">
      <!-- icon：默认 Document 图标；component :is 支持"图标名字符串"或"图标组件对象"两种写法 -->
      <el-icon class="empty-icon"><component :is="icon" /></el-icon>
    </div>
    <div v-if="title" class="empty-title">{{ title }}</div>
    <div v-if="description" class="empty-desc">{{ description }}</div>
    <!-- 默认插槽：调用方放入的操作按钮区；$slots.default 不存在时不渲染，避免留空白 -->
    <div v-if="$slots.default" class="empty-actions">
      <slot />
    </div>
  </div>
</template>

<script setup lang="ts">
// 学习点：默认插槽（<slot />）——父组件写在标签里的任意内容都会被塞到这里。
// 空态组件只负责"排版 + 文案"，按钮放什么由父组件决定，职责单一。
import type { Component } from 'vue'
import { Document } from '@element-plus/icons-vue'

withDefaults(defineProps<{
  // icon 默认给一个现成的 Document 图标组件对象：显式 import 自包含，
  // 不依赖"main.ts 全局注册图标"这个环境约定，组件更稳。
  icon?: string | Component
  title?: string
  description?: string
}>(), {
  icon: Document,
  title: '',
  description: '',
})
</script>

<style scoped>
.empty-state {
  display: flex;
  flex-direction: column;
  align-items: center;   /* 水平居中 */
  justify-content: center;
  text-align: center;    /* 文字多行时也居中 */
  padding: 44px 16px;
  gap: 8px;
}
/* 雾面圆盘：Apple 空状态的标准容器 */
.empty-icon-wrap {
  width: 72px;
  height: 72px;
  border-radius: 50%;
  background: var(--panel2);
  border: 1px solid var(--line-soft);
  display: flex;
  align-items: center;
  justify-content: center;
  margin-bottom: 6px;
}
.empty-icon {
  font-size: 30px;
  color: var(--muted);   /* 图标用弱化色，突出"空"的感觉 */
}
.empty-title {
  font-size: 17px;
  font-weight: 600;
  letter-spacing: -0.01em;
  color: var(--text);
}
.empty-desc {
  font-size: 13px;
  line-height: 1.6;
  color: var(--muted);
  max-width: 420px;      /* 说明文字过长时限制行宽，可读性更好 */
}
.empty-actions {
  margin-top: 10px;
  display: flex;
  gap: 8px;
}
</style>
