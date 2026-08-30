<template>
  <!-- 指标卡栅格（0.10.18 第三批自 5 处 scoped 重复提取，让 StatGrid 包 StatCard）：
       auto-fit + minmax 自动换行，宽屏一排 4 张（全站统一口径）。 -->
  <div class="stat-grid" :class="{ dense }">
    <slot />
  </div>
</template>

<script setup>
// props 化变体：dense = 总览驾驶舱密度（栅格 180px 起步 + StatCard 内部压缩）。
// StatCard 内部压缩必须经 :deep 穿透（scoped 生成 [data-v] 属性选择器，优先级
// 高于 StatCard 自身 scoped 规则）；这条链全局 CSS 做不稳（同优先级看源序），
// 所以收进组件而不是 card.css。
defineProps({
  dense: { type: Boolean, default: false },
})
</script>

<style scoped>
/* 常规密度：OpsSync/OpsMydb/OpsHealth/OpsMcp 口径 */
.stat-grid {
  display: grid;
  grid-template-columns: repeat(auto-fit, minmax(200px, 1fr));
  gap: 12px;
}
/* 驾驶舱密度：Overview 口径——卡片更密（180px 起步），值行压缩 */
.stat-grid.dense {
  grid-template-columns: repeat(auto-fit, minmax(180px, 1fr));
  gap: 10px;
}
.stat-grid.dense :deep(.stat-card) {
  padding: 12px;
  gap: 3px;
}
.stat-grid.dense :deep(.stat-value) {
  font-size: 22px; /* 旧版 28px → 22px，驾驶舱更紧凑 */
}
.stat-grid.dense :deep(.stat-sub) {
  font-size: 11px;
}
</style>
