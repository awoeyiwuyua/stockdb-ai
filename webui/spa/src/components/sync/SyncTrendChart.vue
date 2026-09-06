<template>
  <!-- 同步耗时趋势卡（0.10.18 自 views/OpsSync.vue 拆出；第四批按 docs/design/webui.md
       判据 5 改为默认折叠的钻取项：图表是排查工具，不占运维页首屏）。
       直接复用 /api/history 的同一份数组做图，不额外请求接口：
       x = 每次同步的 ts（时间），左轴 = 耗时 duration_sec（秒），右轴 = 下载文件数 downloads。
       折线断点 = 那次同步没有数值（如 exit_code=null 的"运行中"记录，字段缺失）。
       三态齐备：有图 / 无任何历史（EmptyState）/ 有历史但无数值（EmptyState）。 -->
  <section class="card">
    <el-collapse class="trend-collapse">
      <el-collapse-item name="trend">
        <template #title>
          <span class="trend-title">同步耗时趋势（点击展开）</span>
        </template>
        <p class="card-hint">
          最近 {{ history.length }} 次同步的耗时与下载量；断点表示该次同步未产生数值（运行中或异常中断）。
        </p>
        <EChart v-if="history.length && chartHasData" :option="chartOption" height="280px" />
        <EmptyState
          v-else-if="!history.length"
          icon="TrendCharts"
          title="暂无同步历史"
          description="启动一次同步后，这里会展示每次任务的耗时与下载量趋势。"
        />
        <EmptyState
          v-else
          icon="DataLine"
          title="暂无趋势数据"
          description="历史记录暂缺耗时/下载数字段，等待一次完整同步后自动展示。"
        />
      </el-collapse-item>
    </el-collapse>
  </section>
</template>

<script setup lang="ts">
import { computed } from 'vue'
import EChart from '../EChart.vue'
import EmptyState from '../EmptyState.vue'
import { fmtTsShort, cssVar } from '../../utils/format'
import type { SyncHistoryRow } from '../../types/api'

const props = withDefaults(defineProps<{ history?: SyncHistoryRow[] }>(), { history: () => [] })

// 是否有可画的数值（耗时/下载数至少出现一次）：决定画折线图还是空态
const chartHasData = computed(() =>
  props.history.some((r) => r.duration_sec != null || r.downloads != null),
)

// 结果标签四态判定（与 SyncHistoryTable 的 resultText 同口径——历史拆分时
// 两处各自独立成组件，口径若有变化以 use-sync 侧注释为准）
function resultText(row: SyncHistoryRow): string {
  if (row.exit_code === 0) return row.warn ? '未生效' : '成功'
  if (row.exit_code == null) return '运行中'
  return '失败'
}

// ECharts 折线 option：x=时间（ts），左轴=耗时（秒），右轴=下载文件数（个）。
// 表格里 history 是"新→旧"（loadHistory 时 reverse 过），画图前翻回"旧→新"让时间轴自然从左往右。
// duration_sec 是 app.py append_history 写入的秒数（后端真实字段），downloads 是下载文件数。
const chartOption = computed(() => {
  const rows = [...props.history].reverse()
  if (!rows.length) return null
  const okColor = cssVar('--ok', '#30d158')
  const brandColor = cssVar('--brand', '#0071e3')
  const mutedColor = cssVar('--muted', '#86868b')
  const lineColor = cssVar('--line', '#dcdce1')
  return {
    grid: { left: 8, right: 8, top: 34, bottom: 8, containLabel: true },
    tooltip: {
      trigger: 'axis',
      // 自定义悬浮窗：完整时间 + 各轴数值 + 结果标签（复用历史表格的 resultText 四态判定）
      formatter: (params: Array<{ marker?: string; seriesName?: string; value?: string | number; dataIndex?: number }>) => {
        const i = params[0]?.dataIndex ?? 0
        const r = rows[i] ?? {}
        const lines = [`<b>${r.ts || '—'}</b>`]
        for (const p of params) lines.push(`${p.marker}${p.seriesName}：${p.value ?? '—'}`)
        lines.push(`结果：${resultText(r)}`)
        return lines.join('<br/>')
      },
    },
    legend: {
      top: 0, right: 0,
      textStyle: { color: mutedColor, fontSize: 12 },
      itemWidth: 14, itemHeight: 8,
    },
    xAxis: {
      type: 'category',
      data: rows.map((r) => fmtTsShort(r.ts)),
      boundaryGap: false, // 折线贴边，时间轴连贯
      axisLine: { lineStyle: { color: lineColor } },
      axisLabel: { color: mutedColor, fontSize: 11 },
    },
    yAxis: [
      {
        type: 'value',
        name: '耗时(秒)',
        scale: true, // 只显示有值的区间，耗时波动小也看得清
        nameTextStyle: { color: mutedColor, fontSize: 11 },
        axisLabel: { color: mutedColor, fontSize: 11 },
        splitLine: { lineStyle: { color: lineColor } },
      },
      {
        type: 'value',
        name: '下载(个)',
        scale: true,
        nameTextStyle: { color: mutedColor, fontSize: 11 },
        axisLabel: { color: mutedColor, fontSize: 11 },
        splitLine: { show: false }, // 右轴不画网格线，避免和左轴重叠
      },
    ],
    series: [
      {
        name: '耗时',
        type: 'line',
        data: rows.map((r) => (r.duration_sec != null ? Number(r.duration_sec) : null)),
        smooth: true,
        symbolSize: 5,
        lineStyle: { width: 2, color: brandColor },
        itemStyle: { color: brandColor },
        // 渐变面积：hex 颜色拼透明度后缀（00=全透明 → 33=浅色）
        areaStyle: {
          color: {
            type: 'linear', x: 0, y: 0, x2: 0, y2: 1,
            colorStops: [
              { offset: 0, color: brandColor + '33' },
              { offset: 1, color: brandColor + '00' },
            ],
          },
        },
      },
      {
        name: '下载文件数',
        type: 'line',
        yAxisIndex: 1,
        data: rows.map((r) => (r.downloads != null ? Number(r.downloads) : null)),
        smooth: true,
        symbolSize: 5,
        lineStyle: { width: 2, color: okColor },
        itemStyle: { color: okColor },
      },
    ],
  }
})
</script>

<style scoped>
/* 折叠标题（判据 5：图表默认收起，点开才画） */
.trend-title {
  font-size: 14px;
  font-weight: 600;
  color: var(--text);
}
</style>
