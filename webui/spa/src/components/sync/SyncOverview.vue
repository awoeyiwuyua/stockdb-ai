<template>
  <!-- 状态总览卡（0.10.18 自 views/OpsSync.vue 拆出）：同步管道横幅 + 进程描述 +
       指标卡栅格 + 磁盘条 + 同步能力检查。纯展示（freshTone 经 props 传入，
       其余派生值内部计算）。 -->
  <section class="card">
    <h3 class="card-title">状态总览</h3>

    <!-- 同步管道横幅：正在同步 → 进度条 + 阶段；空闲 → 最近一次结果 -->
    <div v-if="status?.sync_running" class="sync-banner">
      <el-progress
        :percentage="phasePct"
        :stroke-width="14"
        :format="() => `正在${phaseLabel} · ${phasePct}%`"
      />
      <div class="sync-meta">
        <el-tag type="primary" effect="dark">同步中</el-tag>
        <span class="hint">已运行 {{ syncElapsedText }}（每 30s 刷新一次阶段）</span>
      </div>
    </div>
    <div v-else class="sync-banner idle">
      <el-tag :type="lastExitOk ? 'success' : lastExitCode == null ? 'info' : 'danger'">
        {{ lastExitOk ? '空闲 · 上次成功' : lastExitCode == null ? '空闲' : '空闲 · 上次失败' }}
      </el-tag>
      <span v-if="lastSync" class="hint">
        上次同步 {{ lastSync.ts }} · 下载 {{ lastSync.downloads ?? '—' }} 个文件
      </span>
      <span v-else class="hint">尚无同步记录，点击下方按钮启动首次同步</span>
    </div>

    <!-- 容器 / 数据源 / 同步能力 描述块（el-descriptions 一键出表格排版） -->
    <el-descriptions :column="2" border size="small" class="desc">
      <el-descriptions-item label="stockdb 进程">
        <el-tag :type="status?.container?.ok ? 'success' : 'danger'" size="small">
          {{ status?.container?.ok ? '运行中' : '已停止' }}
        </el-tag>
        <span class="hint" style="margin-left: 6px">{{ status?.container?.note || '' }}</span>
      </el-descriptions-item>
      <el-descriptions-item label="运行时长">
        {{ fmtUptime(status?.container?.started) }}
      </el-descriptions-item>
      <el-descriptions-item label="镜像" :span="1">
        {{ status?.container?.image || '—' }}
      </el-descriptions-item>
      <el-descriptions-item label="数据源" :span="1">
        {{ status?.source || '—' }}
      </el-descriptions-item>
    </el-descriptions>

    <!-- 指标卡栅格：StatCard 组件统一样式，tone 按语义着色（栅格公共件 StatGrid） -->
    <StatGrid>
      <!-- 数据新鲜度：滞后天数从全局 store 读（health.lag_days，App 已 30s 轮询 /api/overview，
           本页不必重复请求；≤1 正常 / 2 警告 / >2 错误） -->
      <StatCard
        label="数据最新"
        :value="status?.data_latest ? fmtYMD(status?.data_latest) : '—'"
        :sub="`镜像 ${status?.mirror || '—'}`"
        :tone="freshTone"
      />
      <!-- 全市场标的数量：code_stats{stock,etf,other} 由后端 15s 缓存兜底 -->
      <StatCard label="股票数" :value="status?.code_stats?.stock ?? '—'" sub="code_stats.stock" />
      <StatCard label="ETF 数" :value="status?.code_stats?.etf ?? '—'" sub="code_stats.etf" />
      <StatCard label="其他标的" :value="status?.code_stats?.other ?? '—'" sub="code_stats.other" />
      <!-- 行情服务延迟：null 说明查询失败 → 标红（与旧页 hcSvc 同判据） -->
      <StatCard
        label="行情响应"
        :value="status?.code_stats?.latency_ms != null ? `${status?.code_stats?.latency_ms} ms` : '不可用'"
        :tone="status?.code_stats?.latency_ms != null ? 'ok' : 'err'"
        sub="code_stats.latency_ms"
      />
      <!-- 覆盖范围：coverage{earliest,latest} 是 8 位数字，只取年份展示 -->
      <StatCard label="数据覆盖" :value="coverageText" sub="coverage.earliest ~ latest" />
      <StatCard
        label="定时调度器"
        :value="status?.scheduler_alive ? '运行中' : '已停止'"
        :tone="status?.scheduler_alive ? 'ok' : 'err'"
        sub="scheduler_alive（后台线程心跳）"
      />
      <!-- 今日是否交易日：仅提示用，不影响操作 -->
      <StatCard
        label="今日交易日"
        :value="status?.trading_today ? '是' : '否'"
        :tone="status?.trading_today ? 'ok' : 'warn'"
        sub="trading_today（定时按此跳过休市）"
      />
    </StatGrid>

    <!-- 磁盘用量：el-progress 容量条，>80% 变红提醒扩容 -->
    <div class="disk-block">
      <div class="disk-label">
        <span>数据卷磁盘</span>
        <span class="hint">{{ diskText }}</span>
      </div>
      <!-- 颜色走语义 CSS 变量（--err/--warn/--ok），随主题自动切换，不写死十六进制 -->
      <el-progress
        v-if="diskPct != null"
        :percentage="diskPct"
        :stroke-width="12"
        :color="diskPct > 80 ? 'var(--err)' : diskPct > 60 ? 'var(--warn)' : 'var(--ok)'"
        :format="() => `${diskPct}%`"
      />
    </div>

    <!-- 同步能力检查：sync_cap{ok, checks{updater,source,writable,retry_pending}} -->
    <div class="cap-block">
      <div class="cap-title">
        同步能力检查
        <el-tag :type="status?.sync_cap?.ok ? 'success' : 'danger'" size="small">
          {{ status?.sync_cap?.ok ? '可用' : '不可用' }}
        </el-tag>
      </div>
      <ul class="cap-list">
        <li v-for="(check, name) in status?.sync_cap?.checks || {}" :key="name">
          <el-icon :color="check.ok === false ? 'var(--err)' : check.warn ? 'var(--warn)' : 'var(--ok)'">
            <component :is="check.ok === false ? 'CircleCloseFilled' : check.warn ? 'WarningFilled' : 'CircleCheckFilled'" />
          </el-icon>
          <span class="cap-name">{{ CAP_LABELS[name] || name }}</span>
          <span class="hint">{{ check.detail }}</span>
        </li>
      </ul>
    </div>
  </section>
</template>

<script setup>
import { computed } from 'vue'
import StatCard from '../StatCard.vue'
import StatGrid from '../common/StatGrid.vue'
import { fmtYMD, fmtUptime } from '../../utils/format.js'
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

// 同步阶段 → 中文标签 与 进度百分比（对应后端 _sync_state.phase 取值）
const PHASE_LABEL = {
  idle: '空闲', stopping: '停止服务', syncing: '同步数据中',
  restarting: '重启服务', verifying: '数据校验', done: '已完成',
}
const PHASE_PCT = {
  idle: 0, stopping: 10, syncing: 45, restarting: 70, verifying: 85, done: 100,
}
// sync_cap.checks 的键名 → 中文（供能力检查列表展示）
const CAP_LABELS = { updater: '更新程序', source: '数据源', writable: '数据卷', retry_pending: '待重试' }

// 当前同步阶段百分比（同步中才有意义，空闲给 0）
const phasePct = computed(() => PHASE_PCT[props.status?.sync_phase] ?? 0)
const phaseLabel = computed(() => PHASE_LABEL[props.status?.sync_phase] ?? '处理中')
// 同步已运行时长文本：sync_started 是 epoch 秒，和当前时间相减
const syncElapsedText = computed(() => {
  const s = props.status?.sync_started
  if (!s) return ''
  const sec = Math.max(0, Math.floor(Date.now() / 1000 - s))
  const h = Math.floor(sec / 3600)
  const m = String(Math.floor((sec % 3600) / 60)).padStart(2, '0')
  const ss = String(sec % 60).padStart(2, '0')
  return h ? `${h}:${m}:${ss}` : `${m}:${ss}`
})

const lastSync = computed(() => props.status?.last_sync ?? null)
const lastExitCode = computed(() => props.status?.exit_code ?? null)
const lastExitOk = computed(() => lastExitCode.value === 0)

// 覆盖范围文本：coverage{earliest,latest} 是 8 位数字 → 只取年份，如 '1990 ~ 2026'
const coverageText = computed(() => {
  const c = props.status?.coverage
  if (!c || !c.earliest) return '—'
  return `${String(c.earliest).slice(0, 4)} ~ ${String(c.latest).slice(0, 4)}`
})

// 磁盘：百分比 + 文字（el-progress 需要 0~100 整数）
const diskPct = computed(() => {
  const d = props.status?.disk
  if (!d || d.total_gb == null || !d.total_gb) return null
  return Math.round((d.used_gb / d.total_gb) * 100)
})
const diskText = computed(() => {
  const d = props.status?.disk
  if (!d || d.total_gb == null) return '—'
  return `${d.used_gb} GB / ${d.total_gb} GB · ${d.free_gb ?? '?'} GB 可用`
})
</script>

<style scoped>
/* 同步横幅：正在同步/空闲两态 */
.sync-banner {
  display: flex;
  flex-direction: column;
  gap: 8px;
  padding: 12px;
  border-radius: 8px;
  background: var(--panel2);
  margin-bottom: 12px;
}
.sync-banner.idle {
  flex-direction: row;
  align-items: center;
  gap: 12px;
}

/* 栅格骨架在公共件 StatGrid.vue；这里只留本卡局部差异：与上方描述块/下方磁盘条隔开 */
.stat-grid {
  margin: 12px 0;
}

/* 磁盘条 */
.disk-block {
  margin: 12px 0;
}
.disk-label {
  display: flex;
  justify-content: space-between;
  align-items: baseline;
  margin-bottom: 6px;
  font-size: 13px;
  color: var(--text);
}

/* 同步能力检查 */
.cap-block {
  border-top: 1px dashed var(--line);
  margin-top: 12px;
  padding-top: 10px;
}
.cap-title {
  display: flex;
  align-items: center;
  gap: 8px;
  font-size: 13px;
  color: var(--text);
  margin-bottom: 6px;
}
.cap-list {
  list-style: none;
  margin: 0;
  padding: 0;
  display: flex;
  flex-wrap: wrap;
  gap: 8px 20px;
}
.cap-list li {
  display: flex;
  align-items: center;
  gap: 6px;
  font-size: 13px;
}
.cap-name {
  color: var(--text);
}
</style>
