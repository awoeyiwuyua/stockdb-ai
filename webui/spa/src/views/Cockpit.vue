<template>
  <!-- ═══════════════ 驾驶舱（W1 单页重设计，docs/design/webui-cockpit-redesign.md）═══════════════
       批 1 范围：骨架 + 四灯状态带 + 异常区（静态列出非绿项）+ 时间线占位。
       数据来源：数据灯 = 全局 store（App 层 30s 轮询）；同步/仓库/磁盘灯 = 本页
       use-cockpit 三路拉取（15s 节拍，§5）；时间线数据批 2 接 /api/timeline。 -->
  <div class="cockpit-page">
    <!-- ── 页头 ── -->
    <header class="page-head">
      <div class="head-left">
        <h2 class="page-title">驾驶舱</h2>
        <span class="head-sub">
          最近刷新 {{ store.lastRefresh ? hhmmss(store.lastRefresh) : '等待首次刷新' }}
        </span>
      </div>
      <div class="head-actions">
        <el-button type="primary" :icon="Upload" :loading="syncing" size="small" @click="onHotSync">
          {{ syncing ? '同步中…' : '热更新' }}
        </el-button>
        <el-button :icon="Refresh" :loading="refreshing" size="small" @click="onRefresh">
          刷新
        </el-button>
      </div>
    </header>

    <!-- 同步进行中：日志尾部滚动（§5 进行中态） -->
    <el-alert v-if="syncing" type="info" :closable="false" class="top-alert">
      <pre class="sync-tail">{{ syncTail || '已触发，等待日志…' }}</pre>
    </el-alert>

    <!-- 刷新失败 → 顶部弱提示（不遮内容） -->
    <el-alert
      v-if="store.error"
      type="error"
      :closable="false"
      show-icon
      :title="store.error"
      class="top-alert"
    />

    <!-- 加载态：首次渲染前骨架（overview 与三路页面级数据任一未到） -->
    <template v-if="store.overview === null && !store.error">
      <div class="sk-band sk-card">
        <el-skeleton animated :rows="1" />
      </div>
      <div class="sk-card">
        <el-skeleton animated :rows="4" />
      </div>
    </template>

    <!-- 错误态：首次加载就失败 → 空态 + 重试 -->
    <EmptyState
      v-else-if="store.overview === null"
      icon="CircleClose"
      title="驾驶舱数据加载失败"
      description="接口暂不可用，请检查后端服务后重试"
    >
      <el-button type="primary" @click="onRefresh">重试</el-button>
    </EmptyState>

    <template v-else>
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

      <!-- ③ 时间线（W1 批 2：/api/timeline 七交易日聚合） -->
      <TimelineCard :rows="timeline" />
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

<script setup>
// 组合范式与旧 Overview 一致：全局 store（顶栏/健康/告警）+ 页面级 use-cockpit
// （同步/仓库/磁盘灯）+ usePolling 统一节拍（15s，§5：驾驶舱讲究新鲜）。
// 视图层零直连：取数走 src/api/ 封装、定时器走 usePolling。
import { ref, computed, onMounted, watch } from 'vue'
import { useRouter, useRoute } from 'vue-router'
import { Refresh, Upload } from '@element-plus/icons-vue'
import { ElMessage, ElMessageBox } from 'element-plus'
import { runSync, getLog } from '../api/status.js'
import { useGlobalStore } from '../stores/global.js'
import { useCockpit } from '../composables/use-cockpit.js'
import { usePolling } from '../composables/use-polling.js'
import StatusBand from '../components/cockpit/StatusBand.vue'
import TimelineCard from '../components/cockpit/TimelineCard.vue'
import EmptyState from '../components/EmptyState.vue'
import { getTimeline } from '../api/status.js'
import OpsAlerts from './OpsAlerts.vue'
import OpsLogs from './OpsLogs.vue'
import OpsDiag from './OpsDiag.vue'
import OpsMcp from './OpsMcp.vue'
import OpsMydb from './OpsMydb.vue'

const store = useGlobalStore()
const router = useRouter()
const route = useRoute()
const { lights, worst, aggWord, loadAll } = useCockpit()
const refreshing = ref(false)
const timeline = ref([])

// —— 抽屉群（批 3）：alerts / logs / diag / query；diag 内 tab（check|mcp）——
const dreducers = ref({ alerts: false, logs: false, diag: false, query: false })
const diagTab = ref('check')

function openDrawer(name, tab) {
  if (!(name in dreducers.value)) return
  if (name === 'diag' && tab) diagTab.value = tab
  dreducers.value[name] = true
}

// 旧路径重定向落 /?drawer=xxx → 自动展开对应抽屉（含 tab）
function applyQueryDrawer() {
  const d = route.query.drawer
  if (typeof d === 'string') openDrawer(d, typeof route.query.tab === 'string' ? route.query.tab : undefined)
}
watch(() => route.query.drawer, applyQueryDrawer)
onMounted(applyQueryDrawer)

async function loadTimeline() {
  try {
    const d = await getTimeline(7)
    timeline.value = d?.days ?? []
  } catch { /* 保留旧值，时间线空态 */ }
}

// 把 Date 格式化成 HH:MM:SS（最近刷新时间展示用；与旧 Overview 同款）
const hhmmss = (d) => {
  const p = (n) => String(n).padStart(2, '0')
  return `${p(d.getHours())}:${p(d.getMinutes())}:${p(d.getSeconds())}`
}

async function onRefresh() {
  refreshing.value = true
  try {
    await Promise.all([store.refresh(), Promise.resolve(loadAll()), loadTimeline()])
  } finally {
    refreshing.value = false
  }
}
onMounted(onRefresh)
usePolling(onRefresh, { fast: 15_000 })

// —— 热更新确认流（§2.4/§5）：三行确认（水位/最新/上次同步）→ 触发 →
//    轮询日志尾部直至「同步结束」（上限 20 分钟），完成后整体刷新 ——
const NL = String.fromCharCode(10) // 换行（避免模板/字符串转义纠缠）
const syncing = ref(false)
const syncTail = ref('')

async function onHotSync() {
  const wm = warehouse.value?.watermark_daily || '—'
  const latest = store.health?.latest || '—'
  const lt = schedule.value?.last_trigger
  const last = lt ? `${lt.ts || ''}（exit ${lt.exit ?? '—'}）` : '尚无记录'
  try {
    await ElMessageBox.confirm(
      `仓库水位 ${wm} · 数据最新 ${latest} · 上次同步 ${last}`,
      '立即热更新',
      { confirmButtonText: '开始同步', cancelButtonText: '取消', type: 'info' },
    )
  } catch {
    return // 用户取消
  }
  try {
    await runSync(true)
  } catch (e) {
    ElMessage.error(e?.message || '同步启动失败')
    return
  }
  syncing.value = true
  syncTail.value = ''
  const deadline = Date.now() + 20 * 60 * 1000
  const poll = async () => {
    try {
      const d = await getLog(12)
      const lines = (d.log || '').split(NL).filter((l) => l.trim())
      syncTail.value = lines.slice(-4).join(NL)
      if (lines.some((l) => l.includes('=== 同步结束'))) {
        syncing.value = false
        ElMessage.success('同步完成')
        onRefresh()
        return
      }
    } catch { /* 单轮日志失败忽略，继续轮询 */ }
    if (Date.now() < deadline) setTimeout(poll, 5000)
    else syncing.value = false
  }
  setTimeout(poll, 4000)
}

// 非绿灯列表（异常区）
const abnormal = computed(() => lights.value.filter((l) => l.tone !== 'ok' && l.tone !== 'off'))

// 灯点击 / 异常区条目 → 对应抽屉；同步域仍是独立页（干预与表单密度高，W1 留痕 v0.1）
function onLightSelect(key) {
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
.head-actions {
  display: flex;
  gap: 8px;
}
.sync-tail {
  margin: 0;
  font-family: var(--font-mono, ui-monospace, monospace);
  font-size: 12px;
  line-height: 1.6;
  white-space: pre-wrap;
  word-break: break-all;
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
