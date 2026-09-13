<template>
  <!-- ═══════════ 告警横幅（0.10.38）═══════════
       只在"有需处理项或等待态"时出现（不再有"全部正常"占位）：把同步矩阵里散落的
       失败提到顶部，带三个动作：
         · 立即补录（下拉三选项，语义不同，各自带后果确认）
         · 一键重试（重跑同步）
         · 静音（1h / 4h / 今天 —— **只静提醒，不改事实**：横幅本身照常显示，
           timeline 与计数不变，到期自动解除）
       判定口径在 domain/timeline.ts:pendingAlerts（只认后端 needs_action）。 -->
  <section v-if="items.length || awaiting" class="banner" :class="{ silenced }">
    <div class="bn-head">
      <el-icon class="bn-icon"><WarningFilled /></el-icon>
      <template v-if="items.length">
        <b>需处理 {{ total }} 项</b>
        <span class="bn-sub">最近 {{ fmtDay(items[0].date) }}</span>
      </template>
      <template v-else>
        <b>等待今日同步</b>
        <span class="bn-sub">{{ awaiting?.action_hint || '尚未到定时同步点' }}</span>
      </template>
      <span v-if="silenced" class="bn-silenced">
        已静音<template v-if="muteUntilText">至 {{ muteUntilText }}</template>
      </span>
      <div class="bn-actions">
        <el-dropdown trigger="click" @command="onBackfill">
          <el-button size="small" :loading="busy" :disabled="silenced">
            立即补录<el-icon class="el-icon--right"><ArrowDown /></el-icon>
          </el-button>
          <template #dropdown>
            <el-dropdown-menu>
              <el-dropdown-item command="sync">重跑行情同步（空转无害）</el-dropdown-item>
              <el-dropdown-item command="warehouse">仓库补沉淀（按水印缺口补日K）</el-dropdown-item>
              <el-dropdown-item command="auction" divided>
                打板指标回填 60 天（会用 K 线口径覆盖已采集值）
              </el-dropdown-item>
            </el-dropdown-menu>
          </template>
        </el-dropdown>
        <el-button size="small" :loading="retrying" :disabled="silenced" @click="onRetry">
          一键重试
        </el-button>
        <el-dropdown v-if="!silenced" trigger="click" @command="onMute">
          <el-button size="small" text>静音</el-button>
          <template #dropdown>
            <el-dropdown-menu>
              <el-dropdown-item command="1h">静音 1 小时</el-dropdown-item>
              <el-dropdown-item command="4h">静音 4 小时</el-dropdown-item>
              <el-dropdown-item command="today">静音到今天结束</el-dropdown-item>
            </el-dropdown-menu>
          </template>
        </el-dropdown>
        <el-button v-else size="small" text @click="onUnmute">解除静音</el-button>
        <el-button size="small" text @click="emit('open-logs')">查看日志</el-button>
      </div>
    </div>
    <ul v-if="items.length" class="bn-list">
      <li v-for="a in items" :key="a.date" class="bn-item">
        <span class="bn-date">{{ fmtDay(a.date) }}</span>
        <span class="bn-text">{{ a.hint || a.detail || '存在未决同步异常' }}</span>
        <span v-if="a.count > 1" class="bn-count">×{{ a.count }}</span>
      </li>
    </ul>
    <div v-if="silenced" class="bn-hint">
      静音只影响提醒强度：告警事实、矩阵轨迹与计数照常更新，到期自动解除。
    </div>
  </section>
</template>

<script setup lang="ts">
import { computed, ref } from 'vue'
import { WarningFilled, ArrowDown } from '@element-plus/icons-vue'
import { ElMessage, ElMessageBox } from 'element-plus'
import { runSync, runWarehouse, runAuctionBackfill } from '../../api/status'
import { setAlertMute, clearAlertMute } from '../../api/ops'
import type { PendingAlert } from '../../domain/timeline'
import type { TimelineDay } from '../../types/api'

const props = withDefaults(defineProps<{
  items?: PendingAlert[]
  awaiting?: TimelineDay | null
  silenced?: boolean
  muteUntil?: number | null
}>(), { items: () => [], awaiting: null, silenced: false, muteUntil: null })

const emit = defineEmits<{
  (e: 'open-logs'): void
  (e: 'retried'): void
  (e: 'mute-changed'): void
}>()

const retrying = ref(false)
const busy = ref(false)
const total = computed(() => props.items.reduce((n, a) => n + (a.count || 1), 0))

const fmtDay = (d8: string) => (d8?.length === 8 ? `${d8.slice(4, 6)}-${d8.slice(6, 8)}` : d8)
const muteUntilText = computed(() => {
  if (!props.muteUntil) return ''
  const d = new Date(props.muteUntil * 1000)
  const p = (n: number) => String(n).padStart(2, '0')
  return `${p(d.getMonth() + 1)}-${p(d.getDate())} ${p(d.getHours())}:${p(d.getMinutes())}`
})

/** 一键重试 = 重跑热更新同步（POST /api/sync）。 */
async function onRetry() {
  retrying.value = true
  try {
    await runSync()
    ElMessage.success('已触发同步，请观察矩阵轨迹')
    emit('retried')
  } catch (e) {
    ElMessage.error(`触发失败：${(e as Error).message}`)
  } finally {
    retrying.value = false
  }
}

/** 立即补录：三个选项语义不同 → 必须显式说清做什么、代价是什么。 */
async function onBackfill(cmd: string | number | object) {
  const kind = String(cmd)
  const spec: Record<string, { title: string; body: string; run: () => Promise<unknown> }> = {
    sync: {
      title: '重跑行情同步',
      body: '拉取上游最新行情（增量）。数据已是最新时基本空转，无副作用。',
      run: () => runSync(),
    },
    warehouse: {
      title: '仓库补沉淀',
      body: '按水印缺口把缺失交易日的日K补进数仓（分钟级）。只补缺口，已存在分区不重写。',
      run: () => runWarehouse(3),
    },
    auction: {
      title: '打板指标回填 60 天',
      body: '用历史日K重建近 60 个交易日的打板指标与序列（约 1~2 分钟）。'
        + '注意：会用 K 线口径覆盖同期已采集的指标值（value_source 变为 kline，'
        + '09-11 那类真采集值数字相同但来源标注会变）。',
      run: () => runAuctionBackfill(60),
    },
  }
  const item = spec[kind]
  if (!item) return
  try {
    await ElMessageBox.confirm(item.body, item.title, {
      type: kind === 'auction' ? 'warning' : 'info',
      confirmButtonText: '执行',
      cancelButtonText: '取消',
    })
  } catch {
    return // 用户取消
  }
  busy.value = true
  try {
    await item.run()
    ElMessage.success(`${item.title}：已触发`)
    emit('retried')
  } catch (e) {
    ElMessage.error(`${item.title}失败：${(e as Error).message}`)
  } finally {
    busy.value = false
  }
}

/** 静音（只静提醒，不改事实）。 */
async function onMute(preset: string | number | object) {
  try {
    const r = await setAlertMute(String(preset))
    ElMessage.success(r?.msg || '已静音')
    emit('mute-changed')
  } catch (e) {
    ElMessage.error(`静音失败：${(e as Error).message}`)
  }
}

async function onUnmute() {
  try {
    const r = await clearAlertMute()
    ElMessage.success(r?.msg || '已解除静音')
    emit('mute-changed')
  } catch (e) {
    ElMessage.error(`解除失败：${(e as Error).message}`)
  }
}
</script>

<style scoped>
.banner {
  border: 1px solid var(--warn);
  border-radius: var(--radius-md, 8px);
  background: color-mix(in srgb, var(--warn) 8%, transparent);
  padding: 10px 14px;
  display: flex;
  flex-direction: column;
  gap: 8px;
}
/* 静音态：降饱和但仍可见（事实不隐藏） */
.banner.silenced {
  border-color: var(--line-soft);
  background: var(--panel2);
  opacity: 0.85;
}
.bn-head {
  display: flex;
  align-items: center;
  gap: 10px;
  font-size: 13px;
  flex-wrap: wrap;
}
.bn-icon {
  color: var(--warn);
}
.banner.silenced .bn-icon {
  color: var(--muted);
}
.bn-sub {
  color: var(--muted);
  font-variant-numeric: tabular-nums;
}
.bn-silenced {
  font-size: 12px;
  color: var(--muted);
  border: 1px solid var(--line-soft);
  border-radius: var(--radius-full);
  padding: 1px 8px;
}
.bn-actions {
  margin-left: auto;
  display: flex;
  align-items: center;
  gap: 6px;
  flex-wrap: wrap;
}
.bn-list {
  list-style: none;
  margin: 0;
  padding: 0;
  display: flex;
  flex-direction: column;
  gap: 4px;
}
.bn-item {
  display: flex;
  align-items: baseline;
  gap: 10px;
  font-size: 13px;
  color: var(--text);
}
.bn-date {
  font-variant-numeric: tabular-nums;
  color: var(--err);
  font-weight: 600;
}
.bn-text {
  word-break: break-all;
}
.bn-count {
  color: var(--muted);
  font-size: 12px;
}
.bn-hint {
  font-size: 12px;
  color: var(--muted);
}
</style>
