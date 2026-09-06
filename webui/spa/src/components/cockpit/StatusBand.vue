<template>
  <!-- ═══════════ 状态带（W1 v0.3 重做）：聚合徽标 + 四格自解释灯 ═══════════
       每格三段：名称 + 状态词（与灯同色，不靠颜色记忆）+ 关键值（弱色）。
       纯展示：内容由 use-cockpit 的 lights 数组给定；点击 emit select(key)。 -->
  <div class="status-band">
    <div class="agg-chip" :class="worst">
      <span class="light-dot" :class="worst" />
      <span class="agg-word">{{ aggWord }}</span>
    </div>
    <span class="strip-divider" />
    <button
      v-for="l in lights"
      :key="l.key"
      type="button"
      class="light-cell"
      :class="{ clickable: l.tone !== 'ok' }"
      :title="l.detail"
      @click="$emit('select', l.key)"
    >
      <div class="lc-head">
        <span class="light-dot" :class="l.tone" />
        <span class="lc-name">{{ l.label }}</span>
        <span class="lc-state" :class="l.tone">{{ l.state }}</span>
      </div>
      <div class="lc-value">{{ l.value }}</div>
    </button>
  </div>
</template>

<script setup>
// 灯色 class 约定：ok 绿 / warn 黄 / err 红 / off 灰（card.css 全局）。
defineProps({
  lights: { type: Array, default: () => [] },   // [{key,label,tone,state,value,detail}]
  worst: { type: String, default: 'off' },
  aggWord: { type: String, default: '未知' },
})
defineEmits(['select'])
</script>

<style scoped>
.status-band {
  display: flex;
  align-items: center;
  gap: 18px;
  flex-wrap: wrap;
  background: var(--panel);
  border: 1px solid var(--line-soft);
  border-radius: var(--radius-md);
  box-shadow: var(--shadow-card);
  padding: 14px 20px;
}
/* 聚合徽标：带计数的药丸（全部正常 / 注意 N 项 / 故障 N 项），一眼分级 */
.agg-chip {
  display: inline-flex;
  align-items: center;
  gap: 8px;
  padding: 7px 16px;
  border-radius: var(--radius-full);
  font-weight: 700;
  font-size: 13px;
  border: 1px solid var(--line-soft);
  background: var(--panel2);
  color: var(--text);
}
.agg-chip.ok { background: color-mix(in srgb, var(--ok) 12%, var(--panel)); }
.agg-chip.warn { background: color-mix(in srgb, var(--warn) 14%, var(--panel)); }
.agg-chip.err { background: color-mix(in srgb, var(--err) 12%, var(--panel)); }
.agg-word {
  letter-spacing: 0.01em;
}
.strip-divider {
  width: 1px;
  height: 30px;
  background: var(--line);
}
/* 四格：上=名称行（点+名+状态词），下=关键值（弱色等宽） */
.light-cell {
  text-align: left;
  padding: 4px 10px;
  border-radius: var(--radius-sm);
  border: none;
  background: transparent;
  font: inherit;
  color: var(--text);
  cursor: default;
}
.light-cell.clickable {
  cursor: pointer;
}
.light-cell.clickable:hover {
  background: var(--panel2);
}
.lc-head {
  display: flex;
  align-items: center;
  gap: 7px;
}
.lc-name {
  font-weight: 700;
  font-size: 13px;
}
.lc-state {
  font-size: 12px;
  font-weight: 600;
}
.lc-state.ok { color: var(--ok); }
.lc-state.warn { color: var(--warn); }
.lc-state.err { color: var(--err); }
.lc-state.off { color: var(--muted); }
.lc-value {
  margin-top: 2px;
  padding-left: 17px;
  font-size: 12px;
  color: var(--muted);
  font-variant-numeric: tabular-nums;
}
</style>
