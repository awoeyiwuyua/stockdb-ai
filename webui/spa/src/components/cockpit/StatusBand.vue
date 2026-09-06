<template>
  <!-- ═══════════ 状态带（W1 §2.2）：聚合灯 + 四灯胶囊，非绿可点 ═══════════
       纯展示：灯色/文案由 use-cockpit 的 lights 数组给定；点击 emit select(key)，
       抽屉联动在驾驶舱视图层接线（批 3/4），本组件不持有路由与业务。 -->
  <div class="status-band">
    <div class="agg-light">
      <span class="light-dot" :class="worst" />
      <span class="agg-word">{{ aggWord }}</span>
    </div>
    <span class="strip-divider" />
    <button
      v-for="l in lights"
      :key="l.key"
      type="button"
      class="domain-light"
      :class="{ clickable: l.tone !== 'ok' }"
      :title="l.detail"
      @click="$emit('select', l.key)"
    >
      <span class="light-dot" :class="l.tone" />
      <span class="domain-label">{{ l.label }}</span>
      <span class="domain-detail">{{ l.detail }}</span>
    </button>
  </div>
</template>

<script setup>
// 灯色 class 约定沿用旧健康灯行：ok 绿 / warn 黄 / err 红 / off 灰（base.css 全局）。
defineProps({
  lights: { type: Array, default: () => [] },   // [{key,label,tone,detail}]
  worst: { type: String, default: 'off' },
  aggWord: { type: String, default: '未知' },
})
defineEmits(['select'])
</script>

<style scoped>
.status-band {
  display: flex;
  align-items: center;
  gap: 22px;
  flex-wrap: wrap;
  background: var(--panel);
  border: 1px solid var(--line-soft);
  border-radius: var(--radius-md);
  box-shadow: var(--shadow-card);
  padding: 16px 22px;
}
.agg-light {
  display: flex;
  align-items: center;
  gap: 10px;
}
.agg-word {
  font-size: 15px;
  font-weight: 700;
  letter-spacing: -0.01em;
  color: var(--text);
}
.strip-divider {
  width: 1px;
  height: 20px;
  background: var(--line);
}
.domain-light {
  display: flex;
  align-items: center;
  gap: 7px;
  padding: 6px 14px;
  border-radius: var(--radius-full);
  border: 1px solid transparent;
  background: transparent;
  font: inherit;
  color: var(--text);
  cursor: default;
}
.domain-light.clickable {
  cursor: pointer;
}
.domain-light.clickable:hover {
  border-color: var(--line);
  background: var(--panel2);
}
.domain-label {
  font-weight: 600;
  font-size: 13px;
}
.domain-detail {
  font-size: 12px;
  color: var(--muted);
  max-width: 320px;
  overflow: hidden;
  text-overflow: ellipsis;
  white-space: nowrap;
}
</style>
