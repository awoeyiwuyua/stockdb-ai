<template>
  <!-- Apple 皮肤顶栏：毛玻璃 + 发丝底线；数据新鲜度/告警胶囊、时钟等宽数字 -->
  <header class="status-bar">
    <el-button text class="collapse-btn" @click="$emit('toggle-collapse')">
      <el-icon><component :is="collapsed ? 'Expand' : 'Fold'" /></el-icon>
    </el-button>

    <!-- 数据新鲜度（W1 v0.2：驾驶舱页隐藏——状态带为唯一出处，防双份冗余） -->
    <div v-if="!onCockpit" class="sb-item" :class="lagClass">
      <span class="sb-value">
        <span class="sb-dot" aria-hidden="true" />
        {{ store.health?.latest ? fmtYMD(store.health.latest) : '—' }}
        <span v-if="store.lagDays !== null" class="sb-sub">滞后 {{ store.lagDays }} 天</span>
      </span>
    </div>

    <!-- 告警红点（W1 v0.2：驾驶舱页隐藏，同上） -->
    <RouterLink v-if="!onCockpit" class="sb-item link" to="/ops/alerts">
      <span class="sb-value">
        <span class="sb-dot alert-dot" aria-hidden="true" />
        告警
        <el-badge :value="store.alertCount" :hidden="store.alertCount === 0" type="danger">
          <span class="sb-count">{{ store.alertCount === 0 ? '无' : store.alertCount }}</span>
        </el-badge>
      </span>
    </RouterLink>

    <div class="sb-spacer" />

    <!-- 刷新状态 + 错误 -->
    <span v-if="store.error" class="sb-error" :title="store.error">接口异常</span>
    <span class="sb-refresh" title="最近刷新时间">
      {{ store.lastRefresh ? `刷新 ${hhmm(store.lastRefresh)}` : '等待首次刷新' }}
    </span>

    <ThemeToggle />

    <!-- 时钟 -->
    <span class="sb-clock">{{ clock }}</span>
  </header>
</template>

<script setup>
// 学习点：computed 从 store 派生展示数据；setInterval 在 onUnmounted 清理（防泄漏）。
import { ref, computed, onMounted, onUnmounted } from 'vue'
import { useRoute } from 'vue-router'
import { useGlobalStore } from '../stores/global.js'
import { fmtYMD } from '../utils/format.js'
import ThemeToggle from '../components/ThemeToggle.vue'

// W1 v0.2：驾驶舱（/）上隐藏 新鲜度/告警 胶囊——状态带已是唯一出处
const route = useRoute()
const onCockpit = computed(() => route.path === '/')

defineProps({
  collapsed: { type: Boolean, default: false },
})
defineEmits(['toggle-collapse'])

const store = useGlobalStore()

const clock = ref('--:--:--')
let clockTimer = null
onMounted(() => {
  const tick = () => {
    clock.value = new Date().toLocaleTimeString('zh-CN', { hour12: false })
  }
  tick()
  clockTimer = setInterval(tick, 1000)
})
onUnmounted(() => clearInterval(clockTimer))

const hhmm = (d) =>
  `${String(d.getHours()).padStart(2, '0')}:${String(d.getMinutes()).padStart(2, '0')}`

// 数据滞后着色：1 天以内正常；2 天警告；更多/未知显示错误色
const lagClass = computed(() => {
  const lag = store.lagDays
  if (lag === null) return ''
  if (lag <= 1) return 'ok'
  if (lag <= 2) return 'warn'
  return 'err'
})
</script>

<style scoped>
.status-bar {
  position: sticky;
  top: 0;
  z-index: 20;
  display: flex;
  align-items: center;
  gap: 14px;
  padding: 10px 24px;
  border-bottom: 1px solid var(--glass-border);
  background: var(--glass-bg);
  backdrop-filter: blur(24px) saturate(180%);
  -webkit-backdrop-filter: blur(24px) saturate(180%);
  flex-wrap: wrap;
}
.collapse-btn {
  padding: 6px;
  font-size: 16px;
  color: var(--muted);
}
.sb-item {
  display: flex;
  align-items: center;
  line-height: 1.3;
}
.sb-item.link {
  color: var(--text);
  text-decoration: none;
}
.sb-value {
  display: inline-flex;
  align-items: center;
  gap: 7px;
  font-size: 13px;
  font-weight: 600;
  font-variant-numeric: tabular-nums;
}
.sb-dot {
  width: 8px;
  height: 8px;
  border-radius: 50%;
  background: var(--ok);
  box-shadow: 0 0 0 3px color-mix(in srgb, var(--ok) 20%, transparent);
}
.sb-item.ok .sb-dot { background: var(--ok); box-shadow: 0 0 0 3px color-mix(in srgb, var(--ok) 20%, transparent); }
.sb-item.warn .sb-dot { background: var(--warn); box-shadow: 0 0 0 3px color-mix(in srgb, var(--warn) 20%, transparent); }
.sb-item.err .sb-dot { background: var(--err); box-shadow: 0 0 0 3px color-mix(in srgb, var(--err) 20%, transparent); }
.sb-item.warn .sb-value { color: var(--warn); }
.sb-item.err .sb-value { color: var(--err); }
.alert-dot {
  background: var(--brand);
  box-shadow: 0 0 0 3px color-mix(in srgb, var(--brand) 20%, transparent);
}
.sb-count {
  padding: 1px 8px;
  margin-left: 4px;
  border-radius: 999px;
  background: var(--panel3);
  font-size: 12px;
  font-weight: 600;
  color: var(--text);
}
.sb-sub {
  font-size: 11.5px;
  color: var(--muted);
  font-weight: 400;
}
.sb-spacer { flex: 1; }
.sb-error {
  color: var(--err);
  font-size: 12px;
  font-weight: 600;
}
.sb-refresh {
  color: var(--muted);
  font-size: 11.5px;
}
.sb-clock {
  font-variant-numeric: tabular-nums;
  font-size: 13px;
  font-weight: 600;
  color: var(--text);
}
</style>
