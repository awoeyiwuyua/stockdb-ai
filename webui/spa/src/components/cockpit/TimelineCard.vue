<template>
  <!-- ═══════════ 同步矩阵卡（W1 §2.3 / 0.10.38 改版）═══════════
       每交易日一行（新→旧），行内只外推「需要动作」的同步步骤：
         · 真失败（verify_failed / data_source_error / not_effective）→ 红色胶囊外推
         · 成功/已自愈/被打断/等上游 → 折进 `+N 次成功`，并保留最后一次成功时间
       告警计数只在 >0 时占位（不再出现"告警 0"噪声）；行点击展开当日全部原始条目。 -->
  <section class="card">
    <div class="card-head">
      <h3 class="card-title">同步矩阵</h3>
      <span class="muted">最近 {{ rows.length }} 个交易日</span>
    </div>

    <EmptyState
      v-if="!rows.length"
      icon="Clock"
      title="暂无同步记录"
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

          <!-- 同步轨迹：失败外推 + 成功折叠 -->
          <span class="tl-ev tl-sync-cell">
            <template v-if="summaries[r.date] && (summaries[r.date].important.length || summaries[r.date].foldedCount)">
              <span
                v-for="(s, i) in summaries[r.date].important"
                :key="'bad' + i"
                class="tl-chip"
                :class="s.tone"
                :title="(s.row.detail as string) || (s.row.reason as string) || ''"
              >
                {{ s.text }}
              </span>
              <span
                v-if="summaries[r.date].foldedCount"
                class="tl-chip folded"
                :title="foldedTitle(r.date)"
              >
                {{ summaries[r.date].foldedText }}
                <template v-if="summaries[r.date].lastOk">
                  · 末次 {{ summaries[r.date].lastOk?.hhmm }}
                </template>
              </span>
            </template>
            <span v-else class="tl-none">无同步</span>
            <span v-if="r.awaiting" class="tl-chip warn">{{ r.action_hint || '等待定时同步' }}</span>
          </span>

          <span class="tl-ev" :class="r.backups ? 'ok' : 'off'">
            备份 {{ r.backups ? `×${r.backups.count}` : '—' }}
          </span>
          <!-- 告警：>0 才占位（0 条不再是噪声） -->
          <span v-if="r.alerts.count" class="tl-ev" :class="alertTone(r.alerts)">
            告警 {{ r.alerts.count }}<template v-if="r.alerts.err">（err {{ r.alerts.err }}）</template>
          </span>

          <el-icon class="tl-chev"><component :is="openKey === r.date ? 'ArrowUp' : 'ArrowDown'" /></el-icon>
        </button>

        <!-- 展开态：当日原始条目（含 0.10.38 的失败原因与分类） -->
        <div v-if="openKey === r.date" class="tl-detail">
          <div v-if="!r.sync.length && !r.sediment && !r.alerts.count" class="muted">当日无事件</div>
          <div v-if="summaries[r.date]?.needsAction && summaries[r.date]?.hint" class="tl-line-item hl">
            需处理 · {{ summaries[r.date]?.hint }}
          </div>
          <div v-for="(s, i) in r.sync" :key="'s' + i" class="tl-line-item">
            同步 · {{ s.ts }} · {{ s.label || '—' }} · trigger={{ s.trigger || '—' }} ·
            exit={{ s.exit_code }} · verified={{ s.verified || '—' }} ·
            {{ s.duration_sec ?? '—' }}s · 数据 {{ s.data_latest || '—' }}
            <template v-if="s.detail"> · {{ s.detail }}</template>
            <template v-else-if="s.reason"> · {{ s.reason }}</template>
            <template v-else-if="s.warn"> · {{ s.warn }}</template>
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

<script setup lang="ts">
// 纯展示 + 本地展开态；取数由驾驶舱视图层驱动（rows 传入）。
// 折叠/外推语义在 domain/timeline.ts（纯函数，Vitest 独打）。
import { ref, computed } from 'vue'
import EmptyState from '../EmptyState.vue'
import { summarizeSyncRow, type SyncRowSummary } from '../../domain/timeline'
import type { TimelineDay } from '../../types/api'
import type { LightTone } from '../../types/ui'

const props = withDefaults(defineProps<{ rows?: TimelineDay[] }>(), { rows: () => [] })
const openKey = ref('')

// 每行汇总（外推项 / 折叠数 / 行级提示）——一次算出，模板只读
const summaries = computed<Record<string, SyncRowSummary>>(() => {
  const out: Record<string, SyncRowSummary> = {}
  for (const r of props.rows) out[r.date] = summarizeSyncRow(r)
  return out
})

function foldedTitle(date: string): string {
  const s = summaries.value[date]
  if (!s?.foldedCount) return ''
  const last = s.lastOk ? `最后一次成功 ${s.lastOk.hhmm}` : '无成功记录'
  return `${s.foldedCount} 条非外推记录（成功/已自愈/被打断/等上游）· ${last}`
}

function toggle(date: string) {
  openKey.value = openKey.value === date ? '' : date
}

const WD = ['周日', '周一', '周二', '周三', '周四', '周五', '周六']
function fmtDate(d8: string) {
  if (!d8 || d8.length !== 8) return d8
  const d = new Date(`${d8.slice(0, 4)}-${d8.slice(4, 6)}-${d8.slice(6, 8)}T00:00:00`)
  return `${d8.slice(4, 6)}-${d8.slice(6, 8)} ${WD[d.getDay()]}`
}
function sedimentTone(r: TimelineDay): LightTone {
  if (!r.sediment) return 'off'
  return r.sediment.ok ? 'ok' : 'err'
}
function alertTone(a: TimelineDay['alerts']): LightTone {
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
.tl-sync-cell {
  gap: 6px;
  flex-wrap: wrap;
}
/* 胶囊：失败红 / 等上游黄 / 折叠灰 */
.tl-chip {
  display: inline-flex;
  align-items: center;
  gap: 4px;
  padding: 2px 10px;
  border-radius: var(--radius-full);
  border: 1px solid var(--line-soft);
  font-size: 12px;
  font-variant-numeric: tabular-nums;
  white-space: nowrap;
}
.tl-chip.err {
  color: var(--err);
  border-color: var(--err);
  background: color-mix(in srgb, var(--err) 10%, transparent);
  font-weight: 600;
}
.tl-chip.warn {
  color: var(--warn);
  border-color: var(--warn);
}
.tl-chip.ok {
  color: var(--text);
}
.tl-chip.folded {
  color: var(--muted);
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
.tl-line-item.hl {
  color: var(--err);
  font-weight: 600;
}
.muted {
  color: var(--muted);
  font-size: 13px;
}
</style>
