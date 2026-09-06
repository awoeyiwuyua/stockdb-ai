<template>
  <!-- 指标卡栅格（0.10.18 第三批自 5 处 scoped 重复提取，让 StatGrid 包 StatCard）：
       auto-fit + minmax 自动换行，宽屏一排 4 张（全站统一口径）。 -->
  <div class="stat-grid">
    <slot />
  </div>
</template>

<script setup>
// 0.10.18 第四批：dense 变体已随总览页去 StatCard 化删除（唯一消费方消失，
// 按克制纪律不保留死代码）。
</script>

<style scoped>
.stat-grid {
  display: grid;
  grid-template-columns: repeat(auto-fit, minmax(200px, 1fr));
  gap: 16px;
}
</style>
