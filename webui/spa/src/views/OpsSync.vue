<template>
  <!-- ============================================================
       数据同步页（0.10.18 重构为编排壳，~100 行）。
       职责边界（哲学 #3 依赖方向）：
         - 业务状态机：composables/{use-sync,use-schedule}.js
         - 展示块：components/sync/*（props 进 / 事件出，不自取数；
           港股面板域自含 useHk）
         - 轮询：usePolling 统一节拍（可见 30s / 后台 5min / 回前台补拉）
       本文件只做「组合 + 摆放」，零 fetch、零 setInterval。
       ============================================================ -->
  <div class="page">
    <!-- 页头：标题 + 手动刷新按钮（轮询之外随时补拉一次） -->
    <div class="page-head">
      <h2 class="page-title">数据同步</h2>
      <el-button :icon="Refresh" :loading="loading" @click="loadAll(true)">手动刷新</el-button>
    </div>

    <!-- 非阻塞错误条：轮询期间某次拉取失败时展示文案，页面其余部分保持可用 -->
    <el-alert v-if="error" :title="error" type="error" show-icon class="page-alert" />

    <!-- ① 加载态：整页骨架屏（el-skeleton），首次数据没回来前显示 -->
    <section v-if="loading && !status" class="card">
      <el-skeleton :rows="8" animated />
    </section>

    <!-- ② 错误态：首拉就失败且没有任何数据 → EmptyState + 重试按钮，页面不崩 -->
    <EmptyState
      v-else-if="!status"
      icon="WarningFilled"
      title="状态数据不可用"
      :description="error || '请确认 stockdb 服务运行中，稍后点击手动刷新重试。'"
    >
      <el-button type="primary" :icon="Refresh" @click="loadAll(true)">重试</el-button>
    </EmptyState>

    <!-- ③ 正常态：数据齐了，逐块渲染 -->
    <template v-else>

      <SyncTrendChart :history="history" />

      <SyncOverview :status="status" />

      <!-- ================= 操作区 ================= -->
      <section class="card">
        <h3 class="card-title">同步操作</h3>
        <p class="card-hint">
          热更新：stockdb 保持运行、增量同步 + 自动 reload，零中断（推荐）；停服严格模式：按官方要求先停服务再同步，故障兜底用。
        </p>
        <div class="actions">
          <el-button
            type="primary"
            :icon="VideoPlay"
            :loading="syncBusy"
            :disabled="!!status?.sync_running"
            @click="doSync(true)"
          >
            立即热更新
          </el-button>
          <el-button
            type="warning"
            :icon="SwitchButton"
            :loading="syncBusy"
            :disabled="!!status?.sync_running"
            @click="doSync(false)"
          >
            停服严格同步
          </el-button>
          <span v-if="status?.sync_running" class="hint">同步进行中，按钮已禁用（后端锁保证串行）</span>
        </div>
      </section>

      <SyncScheduleForm
        v-model:enabled="schEnabled"
        v-model:times="schTimes"
        v-model:trading="schTrading"
        :schedule="schedule"
        :saving="schSaving"
        :dirty="schDirty"
        :today-note="schTodayNote"
        @mark-dirty="markSchDirty"
        @save="saveSch"
      />

      <SyncHistoryTable :history="history" />

      <SyncLogCard :log="syncLog" />

      <SyncHkPanel />

    </template>
  </div>
</template>

<script setup>
// 编排壳：图标（:icon 需要真实组件对象）+ 空态组件 + sync/ 域组件 + composables。
// 注意：getContainerLogs / restartContainer 已随容器区块迁往 /ops/health。
import { Refresh, VideoPlay, SwitchButton } from '@element-plus/icons-vue'
import EmptyState from '../components/EmptyState.vue'
import SyncTrendChart from '../components/sync/SyncTrendChart.vue'
import SyncOverview from '../components/sync/SyncOverview.vue'
import SyncScheduleForm from '../components/sync/SyncScheduleForm.vue'
import SyncHistoryTable from '../components/sync/SyncHistoryTable.vue'
import SyncLogCard from '../components/sync/SyncLogCard.vue'
import SyncHkPanel from '../components/sync/SyncHkPanel.vue'
import { useSync } from '../composables/use-sync.js'
import { useSchedule } from '../composables/use-schedule.js'
import { usePolling } from '../composables/use-polling.js'

// —— 同步域状态机（status/history/syncLog + doSync）——
const {
  status, history, syncLog, loading, error, syncBusy,
  loadStatus, loadHistory, loadSyncLog, loadAll, doSync,
} = useSync()

// —— 定时计划域状态机（表单 + 防吞草稿）；失败文案汇入同一 error ——
const {
  schedule, schEnabled, schTimes, schTrading, schSaving, schDirty,
  markSchDirty, loadSchedule, saveSch, schTodayNote,
} = useSchedule({
  tradingToday: () => status.value?.trading_today,
  onError: (msg) => { error.value = msg },
})

// —— 统一轮询：可见性感知节拍（30s/5min）由 use-polling 管，这里只声明"轮询什么"——
usePolling(() => {
  loadStatus()
  loadHistory()
  loadSchedule()
  loadSyncLog()
})

// 首次进入拉全量（loadAll 不含 schedule——属 schedule 域，单独补一次）
loadAll()
loadSchedule()
</script>
