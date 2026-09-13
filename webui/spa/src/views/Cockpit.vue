<template>
  <!-- ═══════════════ 驾驶舱（W1 单页重设计，docs/design/webui-cockpit-redesign.md）═══════════════
       数据来源（0.10.27 起单通道化）：全部四灯 + 时间线 + 资产 totals 来自全局 store
       （App 层轮询 /api/snapshot，一拍拿全同一瞬间一致视图）；本页 15s 快拍只是
       提前触发 store.refresh()。灯判定口径在 domain/lights.ts（纯函数，Vitest 独打）。 -->
  <div class="cockpit-page">
    <!-- ── 页头 ── -->
    <header class="page-head">
      <div class="head-left">
        <h2 class="page-title">驾驶舱</h2>
        <span class="head-sub">
          最近刷新 {{ store.lastRefresh ? hhmmss(store.lastRefresh) : '等待首次刷新' }}
        </span>
      </div>
      <el-button :icon="Refresh" :loading="refreshing" size="small" @click="onRefresh">
        刷新
      </el-button>
    </header>

    <!-- 刷新失败 → 顶部弱提示（不遮内容） -->
    <el-alert
      v-if="store.error"
      type="error"
      :closable="false"
      show-icon
      :title="store.error"
      class="top-alert"
    />

    <!-- 加载态：首次渲染前骨架（snapshot 未到） -->
    <template v-if="store.snapshot === null && !store.error">
      <div class="sk-band sk-card">
        <el-skeleton animated :rows="1" />
      </div>
      <div class="sk-card">
        <el-skeleton animated :rows="4" />
      </div>
    </template>

    <!-- 错误态：首次加载就失败 → 空态 + 重试 -->
    <EmptyState
      v-else-if="store.snapshot === null"
      icon="CircleClose"
      title="驾驶舱数据加载失败"
      description="接口暂不可用，请检查后端服务后重试"
    >
      <el-button type="primary" @click="onRefresh">重试</el-button>
    </EmptyState>

    <template v-else>
      <!-- ⓪ 告警横幅（0.10.38 置顶）：需处理项或等待态出现；补录/重试/静音三动作 -->
      <AlertBanner
        :items="banner"
        :awaiting="awaiting"
        :silenced="store.alertMuted"
        :mute-until="store.alertMuteUntil"
        @open-logs="openDrawer('logs')"
        @retried="onRefresh"
        @mute-changed="onRefresh"
      />

      <!-- ① 四灯状态带（灯点击的抽屉联动批 3 接线；同步灯暂跳数据同步页） -->
      <StatusBand :lights="lights" :worst="worst" :agg-word="aggWord" @select="onLightSelect" />

      <!-- ② 异常区：仅有非绿灯时出现（§5；自动展开定位批 4） -->
      <section v-if="abnormal.length" class="card abnormal-card">
        <div class="card-head">
          <h3 class="card-title">异常 {{ abnormal.length }} 项</h3>
          <span class="muted">展开定位将在后续批次接入对应抽屉</span>
        </div>
        <ul class="abnormal-list">
          <li v-for="l in abnormal" :key="l.key" class="abnormal-item" @click="onLightSelect(l.key)">
            <span class="light-dot" :class="l.tone" />
            <b>{{ l.label }}</b>
            <span class="muted">{{ l.detail }}</span>
          </li>
        </ul>
      </section>

      <!-- ③ 同步矩阵（W1 批 2 + 0.10.38：胶囊折叠 / 失败外推 / 无告警不占位） -->
      <TimelineCard :rows="store.timelineDays" />

      <!-- ④ 数据资产（0.10.38 三栏真身：行情底座 / 分析数仓 / 私有存储） -->
      <AssetsCard
        :status="store.status"
        :warehouse="store.warehouse"
        :totals="store.whTotals"
        :assets="store.assets"
        :latest="store.health?.latest || ''"
      />
    </template>

    <!-- ── 抽屉群（批 3）：四页降级为抽屉内容组件，destroy-on-close 关闭即停轮询 ── -->
    <el-drawer v-model="dreducers.alerts" title="通知中心" size="760px" destroy-on-close>
      <OpsAlerts />
    </el-drawer>
    <el-drawer v-model="dreducers.logs" title="日志中心" size="820px" destroy-on-close>
      <OpsLogs />
    </el-drawer>
    <el-drawer v-model="dreducers.diag" title="诊断" size="820px" destroy-on-close>
      <el-tabs v-model="diagTab">
        <el-tab-pane label="体检" name="check">
          <OpsDiag />
        </el-tab-pane>
        <el-tab-pane label="MCP 观测" name="mcp">
          <OpsMcp />
        </el-tab-pane>
      </el-tabs>
    </el-drawer>
    <el-drawer v-model="dreducers.query" title="数据查询" size="860px" destroy-on-close>
      <OpsMydb />
    </el-drawer>
  </div>
</template>

<script setup lang="ts">
// 组合范式：全局 store（snapshot 单通道）+ use-cockpit（灯推导接线）+ usePolling
// 统一节拍（15s 快拍，§5：驾驶舱讲究新鲜）。视图层零直连：取数走 src/api/ 封装。
import { ref, computed, onMounted, watch } from 'vue'
import { useRouter, useRoute } from 'vue-router'
import { Refresh } from '@element-plus/icons-vue'
import { useGlobalStore } from '../stores/global'
import { useCockpit } from '../composables/use-cockpit'
import { usePolling } from '../composables/use-polling'
import StatusBand from '../components/cockpit/StatusBand.vue'
import TimelineCard from '../components/cockpit/TimelineCard.vue'
import AlertBanner from '../components/cockpit/AlertBanner.vue'
import EmptyState from '../components/EmptyState.vue'
import OpsAlerts from './OpsAlerts.vue'
import OpsLogs from './OpsLogs.vue'
import OpsDiag from './OpsDiag.vue'
import OpsMcp from './OpsMcp.vue'
import OpsMydb from './OpsMydb.vue'
import AssetsCard from '../components/cockpit/AssetsCard.vue'
import { awaitingToday, pendingAlerts } from '../domain/timeline'
import type { DrawerName } from '../types/ui'

const store = useGlobalStore()
const router = useRouter()
const route = useRoute()
const { lights, worst, aggWord } = useCockpit()
const refreshing = ref(false)

// —— 抽屉群（批 3）：alerts / logs / diag / query；diag 内 tab（check|mcp）——
const dreducers = ref<Record<DrawerName, boolean>>({ alerts: false, logs: false, diag: false, query: false })
const diagTab = ref('check')

function openDrawer(name: DrawerName, tab?: string) {
  dreducers.value[name] = true
  if (name === 'diag' && tab) diagTab.value = tab
}

// 旧路径重定向落 /?drawer=xxx → 自动展开对应抽屉（含 tab）
function applyQueryDrawer() {
  const d = route.query.drawer
  if (typeof d === 'string' && d in dreducers.value) {
    openDrawer(d as DrawerName, typeof route.query.tab === 'string' ? route.query.tab : undefined)
  }
}
watch(() => route.query.drawer, applyQueryDrawer)
onMounted(applyQueryDrawer)

// 把 Date 格式化成 HH:MM:SS（最近刷新时间展示用；与旧 Overview 同款）
const hhmmss = (d: Date) => {
  const p = (n: number) => String(n).padStart(2, '0')
  return `${p(d.getHours())}:${p(d.getMinutes())}:${p(d.getSeconds())}`
}

async function onRefresh() {
  refreshing.value = true
  try {
    await store.refresh() // snapshot 一拍拿全：灯 + 时间线 + 资产 totals
  } finally {
    refreshing.value = false
  }
}
onMounted(onRefresh)
usePolling(onRefresh, { fast: 15_000 })

// 非绿灯列表（异常区）
const abnormal = computed(() => lights.value.filter((l) => l.tone !== 'ok' && l.tone !== 'off'))

// 告警横幅：只收「需处理」的交易日（domain/timeline.ts 纯函数；空数组 → 横幅不渲染）
const banner = computed(() => pendingAlerts(store.timelineDays))
const awaiting = computed(() => awaitingToday(store.timelineDays))

// 灯点击 / 异常区条目 → 对应抽屉；同步域仍是独立页（干预与表单密度高，W1 留痕 v0.1）
function onLightSelect(key: string) {
  if (key === 'sync') router.push('/ops/sync')
  else openDrawer('diag')
}
</script>

<style scoped>
/* 页面骨架与旧 Overview 同约定（card.css 供给 .card/.page-title 全局样式） */
.cockpit-page {
  display: flex;
  flex-direction: column;
  gap: 20px;
}
.head-left {
  display: flex;
  align-items: baseline;
  gap: 14px;
}
.head-sub {
  font-size: 13px;
  color: var(--muted);
  font-variant-numeric: tabular-nums;
}
.top-alert {
  width: 100%;
}
.card-head {
  display: flex;
  align-items: center;
  justify-content: space-between;
  gap: 8px;
  flex-wrap: wrap;
}
.muted {
  color: var(--muted);
  font-size: 13px;
}
.sk-band {
  padding: 16px 22px;
}
.abnormal-list {
  list-style: none;
  margin: 0;
  padding: 0;
  display: flex;
  flex-direction: column;
  gap: 8px;
}
.abnormal-list li {
  display: flex;
  align-items: center;
  gap: 8px;
  font-size: 13px;
}
</style>
