<template>
  <!-- ═══════════ 时间线卡（W1 §2.3）：最近 7 个交易日逐日一行 ═══════════
       纯展示：rows 由 /api/timeline 给定（新→旧）；行点击展开当日明细（§5）。
       行内事件点：沉淀（绿=对账 ok / 红=差异 / 灰=无记录）、同步（每条一枚，
       verified pass 绿 / 其他红）、备份、告警计数（>0 黄 / 有 err 红）。 -->
  <section class="card">
    <div class="card-head">
      <h3 class="card-title">时间线</h3>
      <span class="muted">最近 {{ rows.length }} 个交易日</span>
    </div>

    <EmptyState
      v-if="!rows.length"
      icon="Clock"
      title="暂无时间线数据"
      description="接口暂不可用或尚无记录"
    />

    <ul v-else class="tl-list">
      <li v-for="r in rows" :key="r.date" class="tl-row" :class="{ open: openKey === r.date }">
        <button type="button" class="tl-line" @click="toggle(r.date)">
          <span class="tl-date">{{ fmtDate(r.date) }}</span>
          <span class="tl-ev" :class="sedimentTone(r)">
            <span class="light-dot" :class="sedimentTone(r)" />
            沉淀 {{ r.sediment ? `${r.sediment.rows} 行${r.sediment.ok ? '' : ' · 对账差异'}` : '无' }}
          </span>
          <span class="tl-ev">
            <template v-if="r.sync.length">
              <span v-for="(s, i) in r.sync" :key="i" class="tl-sync" :class="syncTone(s)">
                同步 {{ hhmm(s.ts) }} {{ syncMark(s) }}
              </span>
            </template>
            <span v-else class="tl-none">无同步</span>
          </span>
          <span class="tl-ev" :class="r.backups ? 'ok' : 'off'">
            备份 {{ r.backups ? `×${r.backups.count}` : '—' }}
          </span>
          <span class="tl-ev" :class="alertTone(r.alerts)">
            告警 {{ r.alerts.count }}<template v-if="r.alerts.err">（err {{ r.alerts.err }}）</template>
          </span>
          <el-icon class="tl-chev"><component :is="openKey === r.date ? 'ArrowUp' : 'ArrowDown'" /></el-icon>
        </button>

        <!-- 展开态：当日原始条目 -->
        <div v-if="openKey === r.date" class="tl-detail">
          <div v-if="!r.sync.length && !r.sediment && !r.alerts.count" class="muted">当日无事件</div>
          <div v-for="(s, i) in r.sync" :key="'s' + i" class="tl-line-item">
            同步 · {{ s.ts }} · trigger={{ s.trigger || '—' }} · exit={{ s.exit_code }} ·
            verified={{ s.verified || '—' }} · {{ s.duration_sec ?? '—' }}s · 数据 {{ s.data_latest || '—' }}
          </div>
          <div v-if="r.sediment" class="tl-line-item">
            沉淀 · rows={{ r.sediment.rows }} · 对账 {{ r.sediment.ok ? '通过' : '存在差异' }}
          </div>
          <div v-if="r.backups" class="tl-line-item">
            备份 · {{ r.backups.last }} 等 {{ r.backups.count }} 份
          </div>
          <div v-if="r.alerts.count" class="tl-line-item">
            告警 · {{ r.alerts.count }} 条（error {{ r.alerts.err }} / warning {{ r.alerts.warn }}）
          </div>
        </div>
      </li>
    </ul>
  </section>
</template>

<script setup>
// 纯展示 + 本地展开态；取数由驾驶舱视图层驱动（rows 传入）。
import { ref } from 'vue'
import EmptyState from '../EmptyState.vue'

defineProps({ rows: { type: Array, default: () => [] } })
const openKey = ref('')

function toggle(date) {
  openKey.value = openKey.value === date ? '' : date
}

const WD = ['周日', '周一', '周二', '周三', '周四', '周五', '周六']
function fmtDate(d8) {
  if (!d8 || d8.length !== 8) return d8
  const d = new Date(`${d8.slice(0, 4)}-${d8.slice(4, 6)}-${d8.slice(6, 8)}T00:00:00`)
  return `${d8.slice(4, 6)}-${d8.slice(6, 8)} ${WD[d.getDay()]}`
}
function hhmm(ts) {
  return typeof ts === 'string' && ts.length >= 16 ? ts.slice(11, 16) : '—'
}
function sedimentTone(r) {
  if (!r.sediment) return 'off'
  return r.sediment.ok ? 'ok' : 'err'
}
function syncTone(s) {
  const bad = (s.exit_code ?? 0) !== 0 || s.verified === 'fail'
  return bad ? 'err' : 'ok'
}
function syncMark(s) {
  const bad = (s.exit_code ?? 0) !== 0 || s.verified === 'fail'
  return bad ? '✗' : '✓'
}
function alertTone(a) {
  if (!a || !a.count) return 'off'
  return a.err ? 'err' : 'warn'
}
</script>

<style scoped>
.tl-list {
  list-style: none;
  margin: 0;
  padding: 0;
  display: flex;
  flex-direction: column;
}
.tl-row {
  border-bottom: 1px solid var(--line-soft);
}
.tl-row:last-child {
  border-bottom: none;
}
.tl-line {
  display: flex;
  align-items: center;
  gap: 18px;
  flex-wrap: wrap;
  width: 100%;
  padding: 10px 6px;
  background: transparent;
  border: none;
  font: inherit;
  color: var(--text);
  cursor: pointer;
  text-align: left;
}
.tl-line:hover {
  background: var(--panel2);
  border-radius: var(--radius-sm);
}
.tl-date {
  font-weight: 600;
  font-size: 13px;
  font-variant-numeric: tabular-nums;
  min-width: 92px;
}
.tl-ev {
  display: inline-flex;
  align-items: center;
  gap: 6px;
  font-size: 13px;
  color: var(--text);
}
.tl-ev.ok { color: var(--text); }
.tl-ev.err { color: var(--err); }
.tl-ev.warn { color: var(--warn); }
.tl-ev.off { color: var(--muted); }
.tl-sync {
  margin-right: 10px;
}
.tl-none {
  color: var(--muted);
}
.tl-chev {
  margin-left: auto;
  color: var(--muted);
}
.tl-detail {
  padding: 4px 6px 12px 110px;
  display: flex;
  flex-direction: column;
  gap: 4px;
}
.tl-line-item {
  font-size: 12px;
  color: var(--muted);
  font-variant-numeric: tabular-nums;
  word-break: break-all;
}
.muted {
  color: var(--muted);
  font-size: 13px;
}
</style>
