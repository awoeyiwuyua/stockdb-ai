<template>
  <!-- 数据资产卡（0.10.18 第四批，原「状态总览」指标区拆出重排）：数据新鲜度 +
       标的规模 + 覆盖范围 + 行情响应。副标题按判据 4 全部人话化，不再暴露
       code_stats.* / coverage.* 等后端字段名。纯展示，status 由壳下发。 -->
  <section class="card">
    <h3 class="card-title">数据资产</h3>
    <StatGrid>
      <!-- 数据新鲜度：滞后天数从全局 store 读（health.lag_days，App 已 30s 轮询）；
           ≤1 正常 / 2 警告 / >2 错误 -->
      <StatCard
        label="数据最新"
        :value="status?.data_latest ? fmtYMD(status?.data_latest) : '—'"
        :sub="`镜像源标注 ${status?.mirror || '—'}`"
        :tone="freshTone"
      />
      <!-- 全市场标的数量：code_stats{stock,etf,other} 由后端 15s 缓存兜底 -->
      <StatCard label="股票数" :value="status?.code_stats?.stock ?? '—'" sub="全市场 A 股数量" />
      <StatCard label="ETF 数" :value="status?.code_stats?.etf ?? '—'" sub="场内交易基金" />
      <StatCard label="其他标的" :value="status?.code_stats?.other ?? '—'" sub="指数等其他证券" />
      <!-- 行情服务延迟：null 说明查询失败 → 标红 -->
      <StatCard
        label="行情响应"
        :value="status?.code_stats?.latency_ms != null ? `${status?.code_stats?.latency_ms} ms` : '不可用'"
        :tone="status?.code_stats?.latency_ms != null ? 'ok' : 'err'"
        sub="行情接口当前延迟"
      />
      <!-- 覆盖范围：coverage{earliest,latest} 是 8 位数字，只取年份展示 -->
      <StatCard label="数据覆盖" :value="coverageText" sub="历史数据覆盖年份" />
    </StatGrid>
  </section>
</template>

<script setup>
import { computed } from 'vue'
import StatCard from '../StatCard.vue'
import StatGrid from '../common/StatGrid.vue'
import { fmtYMD } from '../../utils/format.js'
import { useGlobalStore } from '../../stores/global.js'

const props = defineProps({
  status: { type: Object, default: null },
})

// 数据新鲜度着色：滞后天数读全局 store（App 已轮询 /api/overview）
const store = useGlobalStore()
const freshTone = computed(() => {
  const lag = store.health?.lag_days
  if (lag == null) return ''
  if (lag <= 1) return 'ok'
  if (lag === 2) return 'warn'
  return 'err'
})

// coverage{earliest,latest} 是 8 位数字 → 只取年份，如 '1990 ~ 2026'
const coverageText = computed(() => {
  const c = props.status?.coverage
  if (!c || !c.earliest) return '—'
  return `${String(c.earliest).slice(0, 4)} ~ ${String(c.latest).slice(0, 4)}`
})
</script>
