<template>
  <!-- ═══════════════ 总览页（0.10.18 第四批按 docs/design/webui.md 重排）═══════════════
       范式：NAS 信息中心——聚合健康灯 + 域灯行（可点击跳转）+ 四张分区卡。
       数据来源：health/alerts/mcp/version 读全局 store（App 层 30s 轮询）；
       域灯与资产/同步卡补充读 /api/status（composables/use-overview.js 静默拉取）。 -->
  <div class="overview-page">
    <!-- ── 页头：标题 + 最近刷新时间 + 手动刷新按钮 ── -->
    <header class="page-head">
      <div class="head-left">
        <h2 class="page-title">总览</h2>
        <span class="head-sub">
          最近刷新 {{ store.lastRefresh ? hhmmss(store.lastRefresh) : '等待首次刷新' }}
        </span>
      </div>
      <!-- 手动刷新：总览 store + 版本 + status，一起刷 -->
      <el-button type="primary" :icon="Refresh" :loading="refreshing" size="small" @click="onRefresh">
        刷新
      </el-button>
    </header>

    <!-- 刷新失败 → 顶部红色 ElAlert（不遮内容，只提示"数据可能过期"） -->
    <el-alert
      v-if="store.error"
      type="error"
      :closable="false"
      show-icon
      :title="store.error"
      class="top-alert"
    />

    <!-- ── 加载态：首次数据还没回来（overview 为 null 且无报错）→ 骨架屏 ── -->
    <template v-if="store.overview === null && !store.error">
      <div class="light-strip">
        <div v-for="i in 5" :key="i" class="sk-card sk-chip">
          <el-skeleton animated :rows="1" />
        </div>
      </div>
      <div class="cards-grid">
        <div v-for="i in 4" :key="'c' + i" class="sk-card">
          <el-skeleton animated :rows="4" />
        </div>
      </div>
    </template>

    <!-- 错误态：首次加载就失败 → 错误空态 + 重试按钮（页面不崩） -->
    <EmptyState
      v-else-if="store.overview === null"
      icon="CircleClose"
      title="总览数据加载失败"
      description="接口暂不可用，请检查后端服务后重试"
    >
      <el-button type="primary" @click="onRefresh">重试</el-button>
    </EmptyState>

    <!-- ── 正常内容（数据到位后渲染） ── -->
    <template v-else>
      <!-- ① 健康灯行：聚合灯（四域最差色）+ 四个域灯（点击跳对应运维页）。
           灯色语义见 docs/design/webui.md §3 判据 1：绿正常 / 黄注意 / 红故障 / 灰未知 -->
      <div class="light-strip">
        <div class="agg-light">
          <span class="light-dot" :class="agg.tone" />
          <span class="agg-word">{{ agg.word }}</span>
        </div>
        <span class="strip-divider" />
        <RouterLink v-for="d in domains" :key="d.key" :to="d.to" class="domain-light">
          <span class="light-dot" :class="d.tone" />
          <span class="domain-label">{{ d.label }}</span>
          <span class="domain-value">{{ d.value }}</span>
        </RouterLink>
      </div>

      <!-- ② 分区卡 ×4：数据资产 / 同步 | 告警 / 版本 -->
      <div class="cards-grid">
        <!-- 数据资产：新鲜度 + 覆盖 + 标的规模（status.code_stats，15s 后端缓存兜底） -->
        <section class="card">
          <div class="card-head">
            <h3 class="card-title">数据资产</h3>
          </div>
          <div class="kv-row">
            <span class="kv-label">数据最新</span>
            <span class="kv-value">{{ dataLatest }}</span>
            <span v-if="lagText" class="hint">{{ lagText }}</span>
          </div>
          <div class="kv-row">
            <span class="kv-label">覆盖范围</span>
            <span class="kv-value">{{ coverageText }}</span>
          </div>
          <div class="kv-row">
            <span class="kv-label">标的数量</span>
            <span class="kv-value">{{ countsText }}</span>
          </div>
          <div class="card-foot">
            <RouterLink to="/ops/sync" class="foot-link">前往数据同步 →</RouterLink>
          </div>
        </section>

        <!-- 同步：上次结果摘要（last_sync / exit_code） -->
        <section class="card">
          <div class="card-head">
            <h3 class="card-title">同步</h3>
          </div>
          <div class="kv-row">
            <span class="kv-label">上次同步</span>
            <span class="kv-value">{{ lastSyncText }}</span>
          </div>
          <div class="kv-row">
            <span class="kv-label">上次结果</span>
            <span class="kv-value" :class="{ 'warn-text': lastSyncFailed }">{{ lastSyncResult }}</span>
          </div>
          <div class="card-foot">
            <RouterLink to="/ops/sync" class="foot-link">查看同步详情 →</RouterLink>
          </div>
        </section>

        <!-- 告警摘要：只保留 count + 最近 3 条极简（完整列表移至 /ops/alerts） -->
        <section class="card">
          <div class="card-head">
            <h3 class="card-title">告警</h3>
            <el-badge :value="store.alertCount" :hidden="store.alertCount === 0" type="danger">
              <el-tag size="small" type="info">共 {{ store.alertCount }} 条</el-tag>
            </el-badge>
          </div>

          <div v-if="recent3.length" class="alert-list">
            <div v-for="a in recent3" :key="a.ts" class="alert-row">
              <!-- 级别色点：error 红 / warning 黄 / info 品牌蓝 -->
              <span class="dot" :style="{ background: alertColor(a.level) }" />
              <span class="alert-time" :title="a.ts">{{ fmtHm(a.ts) }}</span>
              <span class="alert-src">{{ a.source }}</span>
              <span class="alert-msg" :title="a.message">{{ a.message }}</span>
            </div>
          </div>
          <EmptyState v-else icon="Bell" title="暂无告警" description="当前没有待处理的告警" />

          <div class="card-foot">
            <RouterLink to="/ops/alerts" class="foot-link">查看全部告警 →</RouterLink>
          </div>
        </section>

        <!-- 版本卡（并入原 OpsVersion 页）：独立 getVersion() 30s 轮询 -->
        <section class="card">
          <div class="card-head">
            <h3 class="card-title">版本</h3>
            <div class="badge-row">
              <!-- stale 高亮：有新版本 → 警告色；上游可达且最新 → 绿色（纯数据驱动，
                   加载中/接口异常时 ver 为 null，三个徽标自然都不渲染） -->
              <el-tag v-if="ver?.stale" size="small" type="warning" effect="dark">有新版本</el-tag>
              <el-tag v-else-if="ver?.upstream?.tag_name" size="small" type="success" effect="plain">已是最新</el-tag>
              <el-tag v-else-if="ver" size="small" type="info" effect="plain">上游不可达</el-tag>
              <!-- ui_mode 徽标：新版 SPA 壳 / 旧版 legacy 壳 -->
              <el-tag
                v-if="ver"
                size="small"
                :type="ver.ui_mode === 'legacy' ? 'warning' : 'success'"
                effect="plain"
              >{{ ver.ui_mode === 'legacy' ? '旧版 legacy' : '新版 SPA' }}</el-tag>
            </div>
          </div>

          <!-- 加载态：骨架 -->
          <el-skeleton v-if="verLoading && !ver" animated :rows="3" />
          <!-- 接口异常：一行弱化文案 -->
          <p v-else-if="verError && !ver" class="muted">版本信息不可用</p>
          <template v-else-if="ver">
            <div class="kv-row">
              <span class="kv-label">webui 版本</span>
              <span class="kv-value">v{{ ver.webui?.version ?? '—' }}</span>
            </div>
            <div class="kv-row">
              <span class="kv-label">镜像 tag</span>
              <span class="kv-value">{{ ver.image?.tag || '—' }}</span>
            </div>
            <div class="kv-row">
              <span class="kv-label">上游 release</span>
              <!-- upstream.tag_name：可点击跳 GitHub release（target=_blank） -->
              <template v-if="ver.upstream?.tag_name">
                <a
                  v-if="ver.upstream.html_url"
                  :href="ver.upstream.html_url"
                  target="_blank"
                  rel="noopener"
                  class="foot-link"
                >v{{ ver.upstream.tag_name }} ↗</a>
                <span v-else class="kv-value">v{{ ver.upstream.tag_name }}</span>
              </template>
              <span v-else class="kv-value muted">暂不可用</span>
            </div>
            <!-- stale 时的升级提示文案（后端 version_payload 的 msg） -->
            <div v-if="ver.msg" class="kv-row">
              <span class="kv-label">提示</span>
              <span class="kv-value warn-text">{{ ver.msg }}</span>
            </div>
          </template>

          <!-- 旧面板逃生通道：/legacy 直达旧页面 -->
          <div class="card-foot">
            <a href="/legacy" target="_blank" rel="noopener" class="foot-link">旧面板 /legacy ↗</a>
          </div>
        </section>
      </div>
    </template>
  </div>
</template>

<script setup>
// ============================================================
// Overview.vue — 总览驾驶舱（0.10.18 第四批按 docs/design/webui.md 重排）。
// 范式：健康灯 → 分区 → 异常指引（DSM 存储管理器式，判据见设计文档 §3）。
// 1) 页面的 health/alerts/mcp/version 读全局 store（App 层 30s 轮询）；
//    版本卡与域灯用的 /api/status 由 use-overview 状态机提供，usePolling 驱动。
// 2) 所有展示字段都做防御（?. 与 || 兜底），后端某块降级为 null 时页面不崩、显示 '—'。
// ============================================================
import { ref, computed } from 'vue'
import { ElMessage } from 'element-plus'
// 图标显式 import（el-button :icon 需要组件对象；模板里 <el-icon> 才走全局注册）
import { Refresh } from '@element-plus/icons-vue'
import EmptyState from '../components/EmptyState.vue'
import { fmtYMD } from '../utils/format.js'
import { useGlobalStore } from '../stores/global.js'
import { usePolling } from '../composables/use-polling.js'
import { useOverview } from '../composables/use-overview.js'

const store = useGlobalStore()
// status：/api/status 载荷（域灯 + 数据资产卡 + 同步卡用）
const { ver, verLoading, verError, loadVersion, status, loadStatus } = useOverview()

/* ═══════════════ 手动刷新（总览 store + 版本 + status 一起刷） ═══════════════ */
const refreshing = ref(false)
const onRefresh = async () => {
  refreshing.value = true
  // store.refresh() 内部已把异常写进 store.error，不会 throw；Promise.allSettled 保证
  // 单个数据源失败不影响其余。最后用 ElMessage 给"点按钮没反应"一个明确反馈。
  await Promise.allSettled([store.refresh(), loadVersion(), loadStatus()])
  refreshing.value = false
  if (store.error) ElMessage.warning(store.error)
}

// 把 Date 格式化成 HH:MM:SS（最近刷新时间展示用）
const hhmmss = (d) => {
  const p = (n) => String(n).padStart(2, '0')
  return `${p(d.getHours())}:${p(d.getMinutes())}:${p(d.getSeconds())}`
}

/* ═══════════════ ① 健康灯行（判据 1：灯 + 标准状态词） ═══════════════ */
// —— 数据域：滞后天数 ≤1 ok / 2 warn / >2 err / 未知灰 ——
const dataTone = computed(() => {
  const lag = store.lagDays
  if (lag === null) return 'muted'
  if (lag <= 1) return 'ok'
  if (lag === 2) return 'warn'
  return 'err'
})
const dataValue = computed(() =>
  store.health?.latest ? fmtYMD(store.health.latest) : '—')

// —— 同步域：运行中蓝 / 上次成功绿 / 失败红 / 无记录灰 ——
const syncLight = computed(() => {
  const s = status.value
  if (!s) return { tone: 'muted', value: '—' }
  if (s.sync_running) return { tone: 'brand', value: '同步中' }
  const code = s.exit_code
  if (code == null) return { tone: 'muted', value: '空闲·无记录' }
  return code === 0 ? { tone: 'ok', value: '空闲·成功' } : { tone: 'err', value: '空闲·失败' }
})

// —— 服务域（MCP 成功率）：≥90% 绿 / ≥70% 黄 / 更低红 / 无数据灰 ——
const mcpLight = computed(() => {
  const ok = store.mcp?.ok_rate
  if (store.mcp?.total === undefined || ok === null || ok === undefined) {
    return { tone: 'muted', value: '—' }
  }
  return {
    tone: ok >= 0.9 ? 'ok' : ok >= 0.7 ? 'warn' : 'err',
    value: `${(ok * 100).toFixed(1)}%`,
  }
})

// —— 系统域：磁盘 >80% 红 / >60% 黄，容器停止直接红 ——
const sysLight = computed(() => {
  const s = status.value
  if (!s) return { tone: 'muted', value: '—' }
  const d = s.disk
  const pct = d && d.total_gb ? Math.round((d.used_gb / d.total_gb) * 100) : null
  let tone = pct == null ? 'muted' : pct > 80 ? 'err' : pct > 60 ? 'warn' : 'ok'
  if (s.container && !s.container.ok) tone = 'err'
  return { tone, value: pct == null ? '—' : `磁盘 ${pct}%` }
})

const domains = computed(() => [
  { key: 'data', label: '数据', to: '/ops/health', tone: dataTone.value, value: dataValue.value },
  { key: 'sync', label: '同步', to: '/ops/sync', ...syncLight.value },
  { key: 'mcp', label: '服务', to: '/ops/mcp', ...mcpLight.value },
  { key: 'sys', label: '系统', to: '/ops/health', ...sysLight.value },
])

// 聚合灯 = 四域最差色（brand"同步中"不是问题，参与聚合时按正常档计）
const agg = computed(() => {
  const norm = domains.value.map((d) => (d.tone === 'brand' ? 'ok' : d.tone))
  const worst = ['err', 'warn', 'ok', 'muted'].find((t) => norm.includes(t)) ?? 'muted'
  return { tone: worst, word: { err: '有故障', warn: '需要注意', ok: '正常', muted: '状态未知' }[worst] }
})

/* ═══════════════ ② 数据资产卡（status.code_stats/coverage + store.health） ═══════════════ */
const dataLatest = computed(() => {
  const latest = store.health?.latest || status.value?.data_latest
  return latest ? fmtYMD(latest) : '—'
})
const lagText = computed(() => {
  const lag = store.lagDays
  if (lag === null) return ''
  return lag === 0 ? '已是最新' : `滞后 ${lag} 天`
})
// coverage{earliest,latest} 是 8 位数字 → 只取年份，如 '1990 ~ 2026'
const coverageText = computed(() => {
  const c = status.value?.coverage
  if (!c || !c.earliest) return '—'
  return `${String(c.earliest).slice(0, 4)} ~ ${String(c.latest).slice(0, 4)}`
})
const countsText = computed(() => {
  const cs = status.value?.code_stats
  if (!cs) return '—'
  return `股票 ${cs.stock ?? '—'} · ETF ${cs.etf ?? '—'} · 其他 ${cs.other ?? '—'}`
})

/* ═══════════════ ③ 同步卡（status.last_sync / exit_code） ═══════════════ */
const lastSyncText = computed(() => status.value?.last_sync?.ts ?? '尚无记录')
const lastSyncFailed = computed(() => {
  const code = status.value?.exit_code
  return code != null && code !== 0
})
const lastSyncResult = computed(() => {
  const code = status.value?.exit_code
  if (code == null) return '—'
  return code === 0 ? '成功' : '失败'
})

/* ═══════════════ ④ 告警摘要卡（count + 最近 3 条极简） ═══════════════ */
// 后端告警字段是 {ts, level, source, message}；overview.alerts.recent 最多 8 条，这里只取 3
const recent3 = computed(() => (store.overview?.alerts?.recent ?? []).slice(0, 3))
const alertColor = (level) => {
  const l = String(level || '').toLowerCase()
  if (l === 'error' || l === 'critical') return 'var(--err)'
  if (l === 'warning') return 'var(--warn)'
  if (l === 'info') return 'var(--brand)'
  return 'var(--muted)'
}
// ts 是 ISO 本地时间（如 2026-08-15T09:30:00），截取 HH:MM，完整值放 title 悬浮
const fmtHm = (ts) => String(ts || '').slice(11, 16) || '--:--'

/* ═══════════════ 轮询：usePolling 统一节拍（可见 30s / 后台降频）═══════════════ */
usePolling(() => {
  loadVersion()
  loadStatus()
}, { immediate: true })
</script>

<style scoped>
/* 页面骨架：纵向卡片流，间距 20px（Apple 皮肤：白卡 + 大圆角，见 card.css） */
.overview-page {
  display: flex;
  flex-direction: column;
  gap: 20px;
}

/* —— 页头（英雄标题 + 右侧操作；.page-title 全局样式在 card.css） —— */
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
.badge-row {
  display: flex;
  gap: 6px;
  flex-wrap: wrap;
}
.muted {
  color: var(--muted);
}

/* —— 健康灯行：聚合灯 + 四域灯（判据 1 状态灯先行；形态=白卡胶囊条） —— */
.light-strip {
  display: flex;
  align-items: center;
  gap: 22px;
  flex-wrap: wrap;
  background: var(--panel);
  border: 1px solid var(--line-soft);
  border-radius: var(--radius-md);
  box-shadow: var(--shadow-card);
  padding: 16px 22px;
}
.agg-light {
  display: flex;
  align-items: center;
  gap: 10px;
}
.agg-word {
  font-size: 15px;
  font-weight: 700;
  letter-spacing: -0.01em;
  color: var(--text);
}
.strip-divider {
  width: 1px;
  height: 20px;
  background: var(--line);
}
.domain-light {
  display: flex;
  align-items: center;
  gap: 7px;
  text-decoration: none;
}
.domain-label {
  font-size: 13px;
  color: var(--muted);
}
.domain-value {
  font-size: 13px;
  font-weight: 600;
  color: var(--text);
  font-variant-numeric: tabular-nums;
}
.domain-light:hover .domain-label {
  color: var(--brand);
}

/* —— 骨架片：与真实卡同外形 —— */
.sk-card {
  background: var(--panel);
  border: 1px solid var(--line-soft);
  border-radius: var(--radius-lg);
  padding: 22px 24px;
  box-shadow: var(--shadow-card);
}
/* 灯行骨架片：与真实灯等宽占位 */
.sk-chip {
  flex: 1;
}
.cards-grid {
  display: grid;
  grid-template-columns: repeat(auto-fit, minmax(280px, 1fr));
  gap: 20px;
  align-items: start; /* 卡片高度各自内容自适应，不强制拉伸 */
}

/* —— 键值行（资产/同步/版本卡共用） —— */
.kv-row {
  display: flex;
  align-items: center;
  gap: 10px;
  font-size: 14px;
  flex-wrap: wrap;
  padding: 3px 0;
}
.kv-label {
  color: var(--muted);
  font-size: 13px;
  flex-shrink: 0;
}
.kv-value {
  color: var(--text);
  font-weight: 500;
  font-variant-numeric: tabular-nums;
}
.warn-text {
  color: var(--warn);
}

/* —— 告警摘要卡 —— */
.alert-list {
  display: flex;
  flex-direction: column;
}
.alert-row {
  display: flex;
  align-items: center;
  gap: 9px;
  padding: 7px 0;
  border-bottom: 1px solid var(--line);
  font-size: 13px;
}
.alert-row:last-child {
  border-bottom: none; /* 最后一行去掉分隔线，视觉不突兀 */
}
.dot {
  width: 8px;
  height: 8px;
  border-radius: 50%;
  flex-shrink: 0;
}
.alert-time {
  color: var(--muted);
  font-variant-numeric: tabular-nums;
  font-size: 12px;
}
.alert-src {
  color: var(--brand);
  font-size: 12px;
  font-weight: 500;
  flex-shrink: 0;
}
.alert-msg {
  color: var(--text);
  overflow: hidden;
  text-overflow: ellipsis;
  white-space: nowrap; /* 超长文案截断，完整内容悬浮显示 */
}

/* —— 卡片底部链接（Apple 链接：品牌蓝 + 箭头） —— */
.card-foot {
  margin-top: auto;
  padding-top: 10px;
}
.foot-link {
  color: var(--brand);
  font-size: 13px;
  font-weight: 500;
  text-decoration: none;
}
.foot-link:hover {
  color: var(--brand-strong);
  text-decoration: none;
}
</style>
