<template>
  <div class="wh-drawer">
    <!-- 顶部操作栏 -->
    <div class="wh-toolbar">
      <div class="wh-summary-row" v-if="inv">
        <span class="wh-kpi">
          <b>总记录</b> {{ num(inv.totals?.total_rows) }} 行
        </span>
        <span class="wh-kpi">
          <b>标准数仓</b> {{ humanBytes(inv.totals?.standard_bytes) }}
        </span>
        <span class="wh-kpi">
          <b>LevelDB 导入层</b> {{ humanBytes(inv.totals?.leveldb_disk_bytes) }}
          <span class="muted">（原始 KV {{ humanBytes(inv.totals?.leveldb_raw_bytes) }}）</span>
        </span>
        <span v-if="inv.latest_change" class="wh-kpi delta-tag">
          <b>最近变化</b> {{ signedNum(inv.latest_change.delta_rows) }} 行 · {{ inv.latest_change.summary_text }}
        </span>
      </div>
      <div v-else class="muted">正在读取数仓资产统计…</div>
      <el-button size="small" :icon="Refresh" :loading="loading" @click="load(true)">
        强制刷新
      </el-button>
    </div>

    <el-alert
      v-if="error"
      type="warning"
      show-icon
      :closable="false"
      :title="error"
    />

    <!-- 进行中批量导入实时进度 -->
    <div v-if="inv?.active_jobs?.length" class="active-banner">
      <div v-for="job in inv.active_jobs" :key="`${job.snapshot_id}:${job.source_db}`" class="active-job">
        <span class="light-dot ok" />
        <b>正在导入 {{ job.source_db }}</b>
        <span>（{{ job.snapshot_id }}）</span>
        <span>已落盘 <b>{{ job.committed_batches }}</b> 批 / <b>{{ num(job.committed_rows) }}</b> 行 / {{ humanBytes(job.committed_bytes) }}</span>
      </div>
    </div>

    <el-tabs v-model="activeTab">
      <!-- ① 按数据类型查看范围与体量 -->
      <el-tab-pane label="数据类型与范围" name="types">
        <div class="tab-controls">
          <span class="muted">
            主数据类型 {{ primaryDatasets.length }} 项
            <template v-if="indexDatasets.length"> · 二级索引 {{ indexDatasets.length }} 项（{{ num(inv?.index_rows ?? 0) }} 行）</template>
          </span>
          <el-switch v-model="showIndex" active-text="显示二级索引 (kidx)" size="small" />
        </div>

        <el-table :data="displayedDatasets" size="small" stripe empty-text="暂无已沉淀或导入的数据类型">
          <el-table-column label="数据类型" min-width="150">
            <template #default="{ row }">
              <div class="type-cell">
                <span class="type-name">{{ row.label }}</span>
                <span class="type-key">{{ row.type_key }}</span>
              </div>
            </template>
          </el-table-column>
          <el-table-column label="覆盖范围" min-width="185">
            <template #default="{ row }">
              <span class="mono-num">{{ row.range_text || '无日期' }}</span>
            </template>
          </el-table-column>
          <el-table-column label="记录行数" width="110" align="right">
            <template #default="{ row }">
              <span class="mono-num">{{ num(row.rows) }}</span>
            </template>
          </el-table-column>
          <el-table-column label="覆盖 / 标的数量" width="120" align="right">
            <template #default="{ row }">
              <span class="mono-num">
                {{ typeof row.codes === 'number' ? `${num(row.codes)} 只` : (row.units_text || '—') }}
              </span>
            </template>
          </el-table-column>
          <el-table-column label="体积" width="95" align="right">
            <template #default="{ row }">
              <span class="mono-num">{{ row.bytes > 0 ? humanBytes(row.bytes) : '—' }}</span>
            </template>
          </el-table-column>
          <el-table-column label="来源与状态" width="135">
            <template #default="{ row }">
              <el-tag size="small" :type="statusTagType(row.status)">
                {{ row.layer === 'standard' ? '标准数仓' : row.source }} · {{ statusLabel(row.status) }}
              </el-tag>
            </template>
          </el-table-column>
        </el-table>
      </el-tab-pane>

      <!-- ② 导入变化审计与批量任务 -->
      <el-tab-pane label="导入变化与任务" name="changes">
        <h4 class="sub-title">导入前后变化流水</h4>
        <el-table :data="inv?.changes ?? []" size="small" stripe empty-text="暂无历史变更记录">
          <el-table-column label="时间 (UTC)" width="165">
            <template #default="{ row }">
              <span class="mono-num">{{ formatTs(row.recorded_at) }}</span>
            </template>
          </el-table-column>
          <el-table-column label="触发动作" width="100">
            <template #default="{ row }">
              <el-tag size="small" :type="triggerTagType(row.trigger_kind)">
                {{ triggerLabel(row.trigger_kind) }}
              </el-tag>
            </template>
          </el-table-column>
          <el-table-column label="类型与范围变化摘要" min-width="230" prop="summary_text" />
          <el-table-column label="行增量" width="110" align="right">
            <template #default="{ row }">
              <span class="mono-num" :class="{ 'delta-pos': row.delta_rows > 0 }">
                {{ row.delta_rows === 0 ? '0' : signedNum(row.delta_rows) }}
              </span>
            </template>
          </el-table-column>
          <el-table-column label="累计总行数" width="115" align="right">
            <template #default="{ row }">
              <span class="mono-num">{{ num(row.total_rows) }}</span>
            </template>
          </el-table-column>
        </el-table>

        <h4 class="sub-title" style="margin-top: 16px;">批量导入任务明细</h4>
        <el-table :data="inv?.jobs ?? []" size="small" stripe empty-text="尚无批量导入任务">
          <el-table-column label="快照标识" prop="snapshot_id" min-width="160" />
          <el-table-column label="源库" prop="source_db" width="80" />
          <el-table-column label="状态" width="95">
            <template #default="{ row }">
              <el-tag size="small" :type="statusTagType(row.status)">{{ statusLabel(row.status) }}</el-tag>
            </template>
          </el-table-column>
          <el-table-column label="批次数" width="85" align="right">
            <template #default="{ row }">
              <span class="mono-num">
                {{ row.committed_batches }}{{ row.expected_batches ? `/${row.expected_batches}` : '' }}
              </span>
            </template>
          </el-table-column>
          <el-table-column label="入仓行数" width="105" align="right">
            <template #default="{ row }">
              <span class="mono-num">{{ num(row.committed_rows) }}</span>
            </template>
          </el-table-column>
          <el-table-column label="Parquet 体积" width="105" align="right">
            <template #default="{ row }">
              <span class="mono-num">{{ humanBytes(row.committed_bytes) }}</span>
            </template>
          </el-table-column>
          <el-table-column label="核验结果" min-width="150">
            <template #default="{ row }">
              <span v-if="row.verify_summary">
                {{ (row.verify_summary as { passed?: boolean }).passed ? '0 错 0 缺通过' : '发现异常' }}
              </span>
              <span v-else class="muted">—</span>
            </template>
          </el-table-column>
        </el-table>
      </el-tab-pane>
    </el-tabs>
  </div>
</template>

<script setup lang="ts">
import { computed, onMounted, ref } from 'vue'
import { Refresh } from '@element-plus/icons-vue'
import { getWarehouseInventory } from '../../api/ops'
import { usePolling } from '../../composables/use-polling'
import type { WarehouseInventory } from '../../types/api'

const inv = ref<WarehouseInventory | null>(null)
const loading = ref(false)
const error = ref<string | null>(null)
const activeTab = ref('types')
const showIndex = ref(false)

async function load(force = false) {
  loading.value = true
  try {
    inv.value = await getWarehouseInventory(force)
    error.value = null
  } catch (e) {
    error.value = (e as Error)?.message || '读取数仓清单失败'
  } finally {
    loading.value = false
  }
}

onMounted(() => load(false))
usePolling(() => load(false), { fast: 5_000 })

const primaryDatasets = computed(() => (inv.value?.datasets ?? []).filter((d) => !d.is_index))
const indexDatasets = computed(() => (inv.value?.datasets ?? []).filter((d) => d.is_index))
const displayedDatasets = computed(() =>
  showIndex.value ? (inv.value?.datasets ?? []) : primaryDatasets.value,
)

const num = (v: unknown) => (typeof v === 'number' ? v.toLocaleString('zh-CN') : '—')
const signedNum = (v: unknown) => {
  if (typeof v !== 'number') return '—'
  return `${v > 0 ? '+' : ''}${v.toLocaleString('zh-CN')}`
}

function humanBytes(v: unknown): string {
  if (typeof v !== 'number' || Number.isNaN(v)) return '—'
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

function formatTs(ts: string | null | undefined): string {
  if (!ts) return '—'
  return ts.slice(0, 19).replace('T', ' ')
}

function statusLabel(st: string): string {
  const map: Record<string, string> = {
    ready: '可用',
    verified: '已核验',
    completed: '待核验',
    uploading: '导入中',
  }
  return map[st] || st
}

function statusTagType(st: string): 'success' | 'warning' | 'info' {
  if (st === 'verified' || st === 'ready') return 'success'
  if (st === 'uploading' || st === 'completed') return 'warning'
  return 'info'
}

function triggerLabel(k: string): string {
  const map: Record<string, string> = {
    baseline: '基线快照',
    bulk_batch: '导入中',
    bulk_complete: '批量封口',
    bulk_verify: '核验完成',
    sediment: '盘后沉淀',
    daily_backfill: '日线回填',
    dedup_daily: '原库去重',
    minute_backfill: '分钟回填',
    dedup_minute: '分钟去重',
    prune_namespace: '原库清理',
  }
  return map[k] || k
}

function triggerTagType(k: string): 'success' | 'warning' | 'info' {
  if (
    k === 'bulk_verify'
    || k === 'sediment'
    || k === 'daily_backfill'
    || k === 'dedup_daily'
    || k === 'minute_backfill'
    || k === 'dedup_minute'
    || k === 'prune_namespace'
  ) {
    return 'success'
  }
  if (k === 'bulk_batch' || k === 'bulk_complete') return 'warning'
  return 'info'
}
</script>

<style scoped>
.wh-drawer {
  display: flex;
  flex-direction: column;
  gap: 12px;
}
.wh-toolbar {
  display: flex;
  align-items: center;
  justify-content: space-between;
  gap: 12px;
  flex-wrap: wrap;
}
.wh-summary-row {
  display: flex;
  align-items: center;
  gap: 14px;
  flex-wrap: wrap;
  font-size: 13px;
}
.wh-kpi {
  font-variant-numeric: tabular-nums;
}
.delta-tag {
  padding: 2px 8px;
  border-radius: 999px;
  background: rgba(16, 185, 129, 0.12);
  color: #059669;
  font-size: 12px;
}
.active-banner {
  padding: 10px 12px;
  border-radius: 8px;
  background: rgba(64, 158, 255, 0.08);
  border: 1px solid rgba(64, 158, 255, 0.25);
  display: flex;
  flex-direction: column;
  gap: 6px;
}
.active-job {
  display: flex;
  align-items: center;
  gap: 6px;
  font-size: 13px;
  flex-wrap: wrap;
}
.tab-controls {
  display: flex;
  align-items: center;
  justify-content: space-between;
  margin-bottom: 10px;
}
.type-cell {
  display: flex;
  align-items: baseline;
  gap: 6px;
}
.type-name {
  font-weight: 600;
}
.type-key {
  font-size: 11px;
  color: var(--muted);
  font-family: ui-monospace, SFMono-Regular, Menlo, monospace;
}
.mono-num {
  font-variant-numeric: tabular-nums;
}
.delta-pos {
  color: #059669;
  font-weight: 600;
}
.sub-title {
  margin: 0 0 8px 0;
  font-size: 13px;
  font-weight: 600;
}
.muted {
  color: var(--muted);
  font-size: 12px;
}
</style>
