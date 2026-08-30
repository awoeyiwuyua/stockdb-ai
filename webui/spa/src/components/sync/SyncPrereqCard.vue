<template>
  <!-- 前提检查卡（0.10.18 第四批新增，docs/design/webui.md 判据 3）：同步管道的运行
       前提（进程/调度器/更新程序/数据源/数据卷/待重试）。全绿收成一行"全部就绪"，
       有异常自动展开；异常定位给单步处置链接（完整诊断/日志中心），不做多步向导。 -->
  <section class="card">
    <el-collapse v-model="openNames" class="prereq-collapse">
      <el-collapse-item name="prereq">
        <template #title>
          <span class="light-dot" :class="summary.tone" />
          <span class="prereq-summary">{{ summary.word }}</span>
        </template>

        <ul class="prereq-list">
          <li v-for="item in items" :key="item.name">
            <el-icon :color="levelColor(item.level)">
              <component :is="levelIcon(item.level)" />
            </el-icon>
            <span class="prereq-name">{{ item.name }}</span>
            <span class="hint">{{ item.detail }}</span>
            <span v-if="item.extra" class="hint extra">{{ item.extra }}</span>
          </li>
        </ul>

        <div v-if="!summary.ok" class="prereq-links">
          异常定位：
          <RouterLink to="/ops/diag" class="prereq-link">完整诊断</RouterLink>
          <span class="hint">·</span>
          <RouterLink to="/ops/logs" class="prereq-link">日志中心</RouterLink>
        </div>
      </el-collapse-item>
    </el-collapse>
  </section>
</template>

<script setup>
// 纯展示：status 由壳（use-sync 状态机）下发。检查项口径与旧「同步能力检查」一致：
// check.ok === false → 红；check.warn → 黄；否则绿。
import { ref, computed, watch } from 'vue'
import { fmtUptime } from '../../utils/format.js'

const props = defineProps({
  status: { type: Object, default: null },
})

const openNames = ref([])

// sync_cap.checks{updater,source,writable,retry_pending} → 三态检查项
const items = computed(() => {
  const s = props.status
  if (!s) return []
  const cap = s.sync_cap?.checks || {}
  const capItem = (name, key) => {
    const c = cap[key] || {}
    return {
      name,
      level: c.ok === false ? 'err' : c.warn ? 'warn' : 'ok',
      detail: c.detail || '—',
    }
  }
  return [
    {
      name: 'stockdb 进程',
      level: s.container?.ok ? 'ok' : 'err',
      detail: s.container?.note || (s.container?.ok ? '运行中' : '未运行'),
      extra: `运行时长 ${fmtUptime(s.container?.started)} · 镜像 ${s.container?.image || '—'}`,
    },
    {
      name: '定时调度器',
      level: s.scheduler_alive ? 'ok' : 'err',
      detail: s.scheduler_alive ? '后台线程心跳正常' : '调度线程未运行，定时同步不会触发',
    },
    capItem('更新程序', 'updater'),
    capItem('数据源', 'source'),
    capItem('数据卷', 'writable'),
    capItem('待重试任务', 'retry_pending'),
  ]
})

// 汇总灯：有红报红、有黄报黄、全绿报"全部就绪"
const summary = computed(() => {
  const err = items.value.filter((i) => i.level === 'err').length
  const warn = items.value.filter((i) => i.level === 'warn').length
  if (err) return { tone: 'err', word: `前提检查 · ${err} 项异常，需要处理`, ok: false }
  if (warn) return { tone: 'warn', word: `前提检查 · ${warn} 项需要注意`, ok: false }
  return { tone: 'ok', word: `前提检查 · 全部就绪（${items.value.length} 项通过）`, ok: true }
})

// 判据 3：正常安静（收起）、异常自动展开；用户仍可手动收起/展开
watch(
  () => summary.value.ok,
  (ok) => {
    openNames.value = ok ? [] : ['prereq']
  },
  { immediate: true },
)

const levelColor = (l) => (l === 'err' ? 'var(--err)' : l === 'warn' ? 'var(--warn)' : 'var(--ok)')
const levelIcon = (l) =>
  l === 'err' ? 'CircleCloseFilled' : l === 'warn' ? 'WarningFilled' : 'CircleCheckFilled'
</script>

<style scoped>
.prereq-summary {
  font-size: 13px;
  font-weight: 600;
  color: var(--text);
  margin-left: 8px;
}
.prereq-list {
  list-style: none;
  margin: 0;
  padding: 0;
  display: flex;
  flex-wrap: wrap;
  gap: 8px 20px;
}
.prereq-list li {
  display: flex;
  align-items: center;
  gap: 6px;
  font-size: 13px;
}
.prereq-name {
  color: var(--text);
}
.extra {
  margin-left: 4px;
}
.prereq-links {
  margin-top: 10px;
  padding-top: 10px;
  border-top: 1px dashed var(--line);
  font-size: 12px;
  color: var(--muted);
  display: flex;
  align-items: center;
  gap: 6px;
}
.prereq-link {
  color: var(--brand);
  text-decoration: none;
  font-size: 12px;
}
.prereq-link:hover {
  text-decoration: underline;
}
</style>
