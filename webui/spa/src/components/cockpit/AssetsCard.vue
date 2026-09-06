<template>
  <!-- ═══════════ 数据资产卡（W1 v0.4 重构）：三块资产清单 ═══════════
       定义（六维）：广度（标的）/ 深度（历史）/ 新鲜度 / 层级（类型）/
       质量（对账与缺口→由状态带与时间线承载）/ 私有资产。
       展示 = 三个存储各一行：行情库（引擎）/ 仓库（Parquet+DuckDB）/ 私有库（mydb）。
       服务类指标（行情响应延迟）已移至诊断抽屉——资产卡只讲资产，不讲服务。 -->
  <section class="card">
    <h3 class="card-title">数据资产</h3>

    <!-- ① 行情库（引擎 LevelDB）：广度 + 深度 + 新鲜度 -->
    <div class="asset-row">
      <div class="ar-name">
        <b>行情库</b>
        <span class="ar-type">引擎 LevelDB</span>
      </div>
      <div class="ar-facts">
        <span class="fact">股票 <b>{{ cs.stock ?? '—' }}</b></span>
        <span class="fact">ETF <b>{{ cs.etf ?? '—' }}</b></span>
        <span class="fact">其他 <b>{{ cs.other ?? '—' }}</b></span>
        <span class="fact">hk <b>{{ cs.hk ?? 0 }}</b></span>
        <span class="fact">深度 <b>{{ coverageText }}</b></span>
        <span class="fact">最新 <b>{{ latestShort }}</b></span>
      </div>
    </div>

    <!-- ② 仓库（Parquet + DuckDB）：水位 + 交易日 + 粒度 + 备份 -->
    <div class="asset-row">
      <div class="ar-name">
        <b>仓库</b>
        <span class="ar-type">Parquet + DuckDB</span>
      </div>
      <div class="ar-facts">
        <span class="fact">水位 <b>{{ wmShort }}</b></span>
        <span class="fact">沉淀 <b>{{ totals?.sediment_days ?? '—' }}</b> 个交易日</span>
        <span class="fact">周K <b>{{ totals?.weeks ?? '—' }}</b></span>
        <span class="fact">月K <b>{{ totals?.months ?? '—' }}</b></span>
        <span class="fact">备份 <b>{{ totals?.backups?.count ?? '—' }}</b> 份<template v-if="backupAge">（最近 {{ backupAge }}）</template></span>
      </div>
    </div>

    <!-- ③ 私有库（mydb）：自攒数据的表清单 -->
    <div class="asset-row ar-last">
      <div class="ar-name">
        <b>私有库</b>
        <span class="ar-type">引擎 mydb</span>
      </div>
      <div class="ar-facts">
        <span v-if="tables.length" v-for="t in tables" :key="t" class="fact chip">{{ t }}</span>
        <span v-else-if="tablesErr" class="fact off">—（接口不可用）</span>
        <span v-else class="fact off">暂无表</span>
      </div>
    </div>
  </section>
</template>

<script setup lang="ts">
// 数据源：status/warehouse/totals 由驾驶舱下发（snapshot 单通道，不新增请求）；
// 私有库表清单自取 /api/data/tables（低频，onMounted 一次）。
import { ref, computed, onMounted } from 'vue'
import { getTables } from '../../api/data'
import type { StatusPayload, WarehouseStatus, WarehouseTotals } from '../../types/api'

const props = withDefaults(defineProps<{
  status?: StatusPayload | null      // /api/status（code_stats/coverage）
  warehouse?: WarehouseStatus | null // /api/warehouse/status
  totals?: WarehouseTotals | null    // snapshot.timeline.totals
  latest?: string                    // health.latest（YYYY-MM-DD）
}>(), {
  status: null,
  warehouse: null,
  totals: null,
  latest: '',
})

const cs = computed(() => props.status?.code_stats ?? {})
const coverageText = computed(() => {
  const c = props.status?.coverage
  if (!c || !c.earliest) return '—'
  return `${String(c.earliest).slice(0, 4)} ~ ${String(c.latest).slice(0, 4)}`
})
const latestShort = computed(() => (props.latest || '').replaceAll('-', '').slice(4) || '—')
const wmShort = computed(() => {
  const wm = props.warehouse?.watermark_daily
  return wm && wm.length === 8 ? `${wm.slice(4, 6)}-${wm.slice(6, 8)}` : '—'
})
const backupAge = computed(() => {
  const m = props.totals?.backups?.last_mtime
  if (!m) return ''
  const h = Math.round((Date.now() - m * 1000) / 3600000)
  return h < 1 ? '1h 内' : `${h}h 前`
})

// 私有库表清单（上游保留表 + 自定义，与数据查询抽屉同源）
const tables = ref<string[]>([])
const tablesErr = ref(false)
onMounted(async () => {
  try {
    const d = await getTables()
    tables.value = d?.tables ?? []
    tablesErr.value = false
  } catch {
    tablesErr.value = true
  }
})
</script>

<style scoped>
.asset-row {
  display: flex;
  align-items: baseline;
  gap: 16px;
  padding: 10px 2px;
  border-bottom: 1px solid var(--line-soft);
}
.ar-last {
  border-bottom: none;
  padding-bottom: 0;
}
.ar-name {
  display: flex;
  flex-direction: column;
  gap: 2px;
  min-width: 132px;
}
.ar-name b {
  font-size: 13px;
}
.ar-type {
  font-size: 11px;
  color: var(--muted);
}
.ar-facts {
  display: flex;
  align-items: center;
  gap: 14px;
  flex-wrap: wrap;
  font-size: 13px;
}
.fact {
  color: var(--muted);
}
.fact b {
  color: var(--text);
  font-weight: 600;
  font-variant-numeric: tabular-nums;
}
.fact.chip {
  padding: 3px 12px;
  border: 1px solid var(--line-soft);
  border-radius: var(--radius-full);
  color: var(--text);
  font-size: 12px;
}
.fact.off {
  color: var(--muted);
}
</style>
