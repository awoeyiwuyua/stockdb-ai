<template>
  <!-- 苹果式指标瓦片：label 灰字 · 大号加粗等宽数字 · sub 辅助行 -->
  <div class="stat-card">
    <div class="stat-label">{{ label }}</div>
    <div class="stat-value" :class="tone ? `tone-${tone}` : ''">{{ value }}</div>
    <div v-if="sub" class="stat-sub">{{ sub }}</div>
  </div>
</template>

<script setup>
// 学习点：defineProps 声明"父组件可传入的属性"，且全部可选（都有默认值）。
// 组件尽量宽容：调用方只传自己关心的字段，其余用缺省值兜底，不会渲染出错。
defineProps({
  label: { type: String, default: '' },
  // value 既可能是数字也可能是字符串，用联合类型 [String, Number]；
  // 缺省显示占位符 —（与 utils/format.js 的'—'风格保持一致）
  value: { type: [String, Number], default: '—' },
  sub: { type: String, default: '' },
  // tone 限定四种语义色之一；validator 是运行时校验——传错值 Vue 会给出警告，
  // 帮助新手尽早发现拼写笔误（比如把 'warn' 写成 'warning'）
  tone: {
    type: String,
    default: '',
    validator: (v) => ['', 'ok', 'warn', 'err', 'brand'].includes(v),
  },
})
</script>

<style scoped>
/* 瓦片：白底 + 18px 圆角 + 发丝边 + 极轻投影（Apple 指标瓷砖） */
.stat-card {
  background: var(--panel);
  border: 1px solid var(--line-soft);
  border-radius: var(--radius-md);
  padding: 18px 20px;
  box-shadow: var(--shadow-card);
  display: flex;
  flex-direction: column;
  gap: 5px;
}
/* label：小号灰字（--muted） */
.stat-label {
  font-size: 12.5px;
  color: var(--muted);
}
/* value：大号加粗，默认 --text 颜色；行高收紧避免数字顶到容器 */
.stat-value {
  font-size: 32px;
  font-weight: 700;
  letter-spacing: -0.015em;
  line-height: 1.15;
  color: var(--text);
  font-variant-numeric: tabular-nums; /* 等宽数字：数据跳动时宽度不抖动 */
}
/* sub：辅助小字，同样用弱化色 */
.stat-sub {
  font-size: 12px;
  color: var(--muted);
}
/* 四种语义色：把 CSS 变量映射到类上，模板里只写类名不写具体颜色 */
.tone-ok    { color: var(--ok); }
.tone-warn  { color: var(--warn); }
.tone-err   { color: var(--err); }
.tone-brand { color: var(--brand); }
</style>
