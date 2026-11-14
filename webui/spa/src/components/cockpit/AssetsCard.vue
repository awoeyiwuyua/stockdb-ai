<template>
  <!-- ═══════════ 数据资产卡（0.10.38 三栏改版）═══════════
       三栏并排：行情底座 / 分析数仓 / 私有存储。取数原则：**每个字段都有真实来源**，
       取不到就显示「未监控」——不再出现设计稿里那样的编造值（"11 张基础数据表"、
       "连接池 2/10"、"无坏块" 均无数据支撑，已剔除）。
       数据来源：status（code_stats/coverage/disk）、warehouse（watermark/last_result）、
       timeline.totals（沉淀日/周月K/仓库备份）、assets（研究库/双备份/磁盘分层）。 -->
  <section class="assets-grid">
    <!-- ① 行情底座（引擎 LevelDB） -->
    <div class="card asset-col">
      <div class="ac-head">
        <h3 class="card-title">行情底座</h3>
        <span class="ac-sub">引擎 LevelDB</span>
      </div>
      <div class="ac-status">
        <span class="light-dot" :class="engineTone" />
        {{ engineOk ? '正常' : '不可用' }}
      </div>
      <dl class="ac-facts">
        <div class="fact"><dt>股票标的</dt><dd>{{ num(cs.stock) }}</dd></div>
        <div class="fact"><dt>ETF 基金</dt><dd>{{ num(cs.etf) }}</dd></div>
        <div class="fact"><dt>其他标的</dt><dd>{{ num(cs.other) }}</dd></div>
        <div class="fact"><dt>覆盖范围</dt><dd>{{ coverageText }}</dd></div>
        <div class="fact"><dt>最新水位</dt><dd>{{ latestShort }}</dd></div>
        <div class="fact"><dt>磁盘占用</dt><dd>{{ gb(disk?.groups?.market_data) }}</dd></div>
      </dl>
    </div>

    <!-- ② 分析数仓（Parquet + DuckDB：按数据类型与范围展示 + 导入变化入口） -->
    <div class="card asset-col">
      <div class="ac-head ac-head-between">
        <div class="ac-head-left">
          <h3 class="card-title">分析数仓</h3>
          <span class="ac-sub">Parquet + DuckDB</span>
        </div>
        <button type="button" class="ac-more-btn" @click="emit('openWarehouse')">
          明细与变化 ›
        </button>
      </div>
      <div class="ac-status">
        <span class="light-dot" :class="warehouseTone" />
        {{ warehouseStatusLabel }}
        <span v-if="latestDeltaBadge" class="delta-pill" :title="whInv?.latest_change?.summary_text || ''">
          {{ latestDeltaBadge }}
        </span>
      </div>
      <dl class="ac-facts">
        <template v-if="visibleHighlights.length">
          <div v-for="item in visibleHighlights" :key="item.key" class="fact">
            <dt>{{ item.label }}</dt>
            <dd>
              <span>{{ item.range_text }}</span>
              <span v-if="item.units_text" class="fact-sub">（{{ item.units_text }}）</span>
            </dd>
          </div>
          <div v-if="hiddenTypeCount > 0 || (whInv?.index_rows ?? 0) > 0" class="fact">
            <dt>其余类型 / 索引</dt>
            <dd>
              <button type="button" class="ac-inline-link" @click="emit('openWarehouse')">
                <template v-if="hiddenTypeCount > 0">+{{ hiddenTypeCount }} 类主数据</template>
                <template v-if="hiddenTypeCount > 0 && (whInv?.index_rows ?? 0) > 0"> · </template>
                <template v-if="(whInv?.index_rows ?? 0) > 0">索引 {{ num(whInv?.index_rows) }} 行</template>
              </button>
            </dd>
          </div>
        </template>
        <template v-else>
          <div class="fact"><dt>历史沉淀</dt><dd>{{ totals?.sediment_days ?? '未监控' }} 个交易日</dd></div>
          <div class="fact"><dt>聚合周期</dt>
            <dd>周K {{ totals?.weeks ?? '未监控' }} · 月K {{ totals?.months ?? '未监控' }}</dd>
          </div>
        </template>
        <div class="fact"><dt>仓库备份</dt>
          <dd>{{ bk.warehouse?.count ?? '未监控' }} 份<template v-if="whBackupAge">（{{ whBackupAge }}）</template></dd>
        </div>
        <div class="fact"><dt>对账与核验</dt><dd>{{ reconcileText }}</dd></div>
        <div class="fact"><dt>磁盘占用</dt><dd>{{ gb(disk?.groups?.warehouse) }}</dd></div>
      </dl>
    </div>

    <!-- ③ 私有存储（研究主库 + 引擎私有 KV） -->
    <div class="card asset-col">
      <div class="ac-head">
        <h3 class="card-title">私有存储</h3>
        <span class="ac-sub">研究成果库 + 引擎 mydb</span>
      </div>
      <div class="ac-status">
        <span class="light-dot" :class="researchTone" />
        {{ researchText }}
      </div>
      <dl class="ac-facts">
        <div class="fact"><dt>研究库</dt>
          <dd>{{ res.mode === 'mydb' ? 'mydb 回滚模式（未监控）' : `${res.metrics ?? 0} 指标日 / ${res.snapshots ?? 0} 快照` }}</dd>
        </div>
        <div class="fact"><dt>序列 / 清单</dt>
          <dd>{{ res.mode === 'mydb' ? '未监控' : `${res.series ?? 0} / ${res.lists ?? 0}` }}</dd>
        </div>
        <div class="fact"><dt>研究库备份</dt>
          <dd>{{ bk.research?.count ?? '未监控' }} 份<template v-if="rsBackupAge">（{{ rsBackupAge }}）</template></dd>
        </div>
        <div class="fact"><dt>库体量</dt>
          <dd>{{ gb(disk?.groups?.research_db) }} + mydb {{ gb(disk?.groups?.mydb) }}</dd>
        </div>
        <div class="fact"><dt>数据卷</dt>
          <dd>{{ volumeText }}</dd>
        </div>
      </dl>
    </div>
  </section>
</template>

<script setup lang="ts">
// 展示层零直连：全部字段由驾驶舱从 snapshot 下发（含 0.10.38 新增 assets 块）。
import { computed } from 'vue'
import type {
  AssetsPayload,
  DiskUsage,
  StatusPayload,
  WarehouseInventory,
  WarehouseStatus,
  WarehouseTotals,
} from '../../types/api'

const props = withDefaults(defineProps<{
  status?: StatusPayload | null
  warehouse?: WarehouseStatus | null
  totals?: WarehouseTotals | null
  assets?: AssetsPayload | null
  latest?: string
}>(), {
  status: null,
  warehouse: null,
  totals: null,
  assets: null,
  latest: '',
})

const emit = defineEmits<{
  (e: 'openWarehouse'): void
}>()

const cs = computed(() => props.status?.code_stats ?? {})
const res = computed(() => props.assets?.research ?? {})
const bk = computed(() => props.assets?.backups ?? {})
const disk = computed(() => props.assets?.disk ?? null)
const whInv = computed<WarehouseInventory | null>(() => props.assets?.warehouse_inventory ?? null)

const MAX_CARD_TYPES = 5
const visibleHighlights = computed(() => (whInv.value?.highlights ?? []).slice(0, MAX_CARD_TYPES))
const hiddenTypeCount = computed(() => {
  const total = whInv.value?.highlights?.length ?? 0
  return total > MAX_CARD_TYPES ? total - MAX_CARD_TYPES : 0
})

const num = (v: unknown) => (typeof v === 'number' ? v.toLocaleString('zh-CN') : '未监控')

/** 字节 → 人类可读；null/undefined → 未监控（不显示 0 冒充真实值） */
function humanBytes(v: unknown): string {
  if (typeof v !== 'number') return '未监控'
  if (v <= 0) return '0 B'
  const units = ['B', 'KB', 'MB', 'GB', 'TB']
  let n = v
  let i = 0
  while (n >= 1024 && i < units.length - 1) {
    n /= 1024
    i += 1
  }
  return `${n >= 100 ? Math.round(n) : n.toFixed(1)} ${units[i]}`
}
const gb = (v: unknown) => humanBytes(v)

function ageText(mtime: unknown): string {
  if (typeof mtime !== 'number' || !mtime) return ''
  const h = Math.round((Date.now() - mtime * 1000) / 3600000)
  if (h < 1) return '1h 内'
  if (h < 48) return `${h}h 前`
  return `${Math.round(h / 24)}d 前`
}
const whBackupAge = computed(() => ageText(bk.value.warehouse?.last_mtime))
const rsBackupAge = computed(() => ageText(bk.value.research?.last_mtime))

const engineOk = computed(() => Boolean(props.status?.container?.ok))
const engineTone = computed(() => (engineOk.value ? 'ok' : 'err'))
const activeJobs = computed(() => whInv.value?.active_jobs ?? [])
const warehouseOk = computed(() => props.warehouse?.available !== false
  && Boolean(
    props.warehouse?.watermark_daily
    ?? props.totals?.sediment_days
    ?? whInv.value?.datasets?.length,
  ))
const warehouseTone = computed(() => (warehouseOk.value ? 'ok' : 'err'))
const warehouseStatusLabel = computed(() => {
  if (!warehouseOk.value) return '不可用'
  if (activeJobs.value.length > 0) {
    const j = activeJobs.value[0]
    return `导入中（${j.source_db} ${j.committed_batches} 批）`
  }
  return '正常'
})

const latestDeltaBadge = computed(() => {
  const lc = whInv.value?.latest_change
  if (!lc || !lc.delta_rows) return ''
  const sign = lc.delta_rows > 0 ? '+' : ''
  return `最近 ${sign}${lc.delta_rows.toLocaleString('zh-CN')} 行`
})

const researchTone = computed(() => (res.value.available ? 'ok' : res.value.mode === 'mydb' ? 'off' : 'err'))
const researchText = computed(() => {
  if (res.value.mode === 'mydb') return 'mydb 回滚模式'
  return res.value.available ? '正常' : '未监控'
})

const coverageText = computed(() => {
  const c = props.status?.coverage
  if (!c || !c.earliest) return '未监控'
  return `${String(c.earliest).slice(0, 4)} ~ ${String(c.latest).slice(0, 4)}`
})
const latestShort = computed(() => (props.latest || '').replaceAll('-', '').slice(4) || '未监控')

/** 对账与核验：优先读持久化日频对账 + 批量导入核验，回退运行期 last_result */
const reconcileText = computed(() => {
  if (activeJobs.value.length > 0) {
    const j = activeJobs.value[0]
    return `导入中 ${j.source_db}（${j.committed_rows.toLocaleString('zh-CN')} 行）`
  }
  const parts: string[] = []
  const persistedDaily = whInv.value?.reconcile?.daily
  const days = (props.warehouse?.last_result as { days?: unknown[] } | null)?.days
  const first = Array.isArray(days) ? (days[0] as Record<string, unknown> | undefined) : undefined
  const runtimeRec = first?.reconcile as { ok?: boolean; traded?: number; stored?: number } | undefined
  const dailyRec = persistedDaily ?? runtimeRec
  if (dailyRec) {
    if (dailyRec.ok) parts.push(`日频通过（${dailyRec.traded ?? '—'} 只）`)
    else parts.push(`日频差异（${dailyRec.traded ?? '—'}/${dailyRec.stored ?? '—'}）`)
  }
  const bulkRec = whInv.value?.reconcile?.bulk
  if (bulkRec && bulkRec.job_count > 0) {
    if (bulkRec.verified_count > 0) {
      parts.push(`导入已核验 ${bulkRec.verified_count} 批任务`)
    } else {
      parts.push(`导入待核验 ${bulkRec.job_count} 项`)
    }
  }
  return parts.length > 0 ? parts.join(' · ') : '未监控'
})

const volumeText = computed(() => {
  const v = disk.value?.volume as DiskUsage | null | undefined
  if (!v || typeof v.used_gb !== 'number') return '未监控'
  const pct = typeof v.total_gb === 'number' && v.total_gb > 0
    ? Math.round((v.used_gb / v.total_gb) * 100)
    : null
  return `${v.used_gb} GB${pct !== null ? ` / ${pct}%` : ''}`
})
</script>

<style scoped>
.assets-grid {
  display: grid;
  grid-template-columns: repeat(3, minmax(0, 1fr));
  gap: 16px;
}
@media (max-width: 1100px) {
  .assets-grid {
    grid-template-columns: 1fr;
  }
}
.asset-col {
  display: flex;
  flex-direction: column;
  gap: 8px;
}
.ac-head {
  display: flex;
  align-items: baseline;
  gap: 8px;
}
.ac-head-between {
  justify-content: space-between;
}
.ac-head-left {
  display: flex;
  align-items: baseline;
  gap: 8px;
}
.ac-sub {
  font-size: 11px;
  color: var(--muted);
}
.ac-more-btn,
.ac-inline-link {
  border: none;
  background: transparent;
  padding: 0;
  color: var(--el-color-primary, #409eff);
  font-size: 12px;
  font-weight: 600;
  cursor: pointer;
}
.ac-more-btn:hover,
.ac-inline-link:hover {
  text-decoration: underline;
}
.ac-status {
  display: inline-flex;
  align-items: center;
  flex-wrap: wrap;
  gap: 6px;
  font-size: 13px;
  color: var(--text);
}
.delta-pill {
  font-size: 11px;
  font-weight: 600;
  padding: 1px 7px;
  border-radius: 999px;
  background: rgba(16, 185, 129, 0.14);
  color: #059669;
  font-variant-numeric: tabular-nums;
}
.ac-facts {
  margin: 0;
  display: flex;
  flex-direction: column;
  gap: 6px;
}
.fact {
  display: flex;
  align-items: baseline;
  justify-content: space-between;
  gap: 10px;
  font-size: 13px;
}
.fact dt {
  color: var(--muted);
  white-space: nowrap;
}
.fact dd {
  margin: 0;
  font-weight: 600;
  font-variant-numeric: tabular-nums;
  text-align: right;
}
.fact-sub {
  font-weight: 400;
  font-size: 11px;
  color: var(--muted);
}
</style>
