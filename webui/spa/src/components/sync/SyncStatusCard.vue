<template>
  <!-- 同步状态卡（0.10.18 第四批按 docs/design/webui.md 重排）：状态灯 + 标准状态词 +
       操作按钮同位（判据 2）+ 上次结果摘要；同步中显示阶段进度横幅。
       原「状态总览」的横幅逻辑与原「同步操作」卡在此合并；纯展示，doSync 由壳注入。 -->
  <section class="card">
    <div class="status-row">
      <span class="light-dot lg" :class="light.tone" />
      <div class="status-main">
        <div class="status-word">{{ light.word }}</div>
        <div class="hint">{{ metaText }}</div>
      </div>
      <div class="actions">
        <el-button
          type="primary"
          :icon="VideoPlay"
          :loading="syncBusy"
          :disabled="!!status?.sync_running"
          @click="emit('sync', true)"
        >
          立即热更新
        </el-button>
        <el-button
          type="warning"
          :icon="SwitchButton"
          :loading="syncBusy"
          :disabled="!!status?.sync_running"
          @click="emit('sync', false)"
        >
          停服严格同步
        </el-button>
        <span v-if="status?.sync_running" class="hint">同步进行中，按钮已禁用（后端锁保证串行）</span>
      </div>
    </div>

    <!-- 同步中：进度横幅（阶段百分比 + 已运行时长，字段口径同原状态总览） -->
    <div v-if="status?.sync_running" class="sync-banner">
      <el-progress
        :percentage="phasePct"
        :stroke-width="14"
        :format="() => `正在${phaseLabel} · ${phasePct}%`"
      />
      <div class="sync-meta">
        <el-tag type="primary" effect="dark">同步中</el-tag>
        <span class="hint">已运行 {{ elapsedText }}（每 30s 刷新一次阶段）</span>
      </div>
    </div>

    <p class="card-hint">
      热更新：stockdb 保持运行、增量同步 + 自动 reload，零中断（推荐）；停服严格模式：按官方要求先停服务再同步，故障兜底用。
    </p>
  </section>
</template>

<script setup>
// 状态灯语义（判据 1）：同步中蓝 / 上次成功绿 / 失败红 / 无记录灰
import { computed } from 'vue'
import { VideoPlay, SwitchButton } from '@element-plus/icons-vue'

const props = defineProps({
  status: { type: Object, default: null },
  syncBusy: { type: Boolean, default: false },
})
const emit = defineEmits(['sync'])

// 同步阶段 → 中文标签与进度百分比（对应后端 _sync_state.phase 取值）
const PHASE_LABEL = {
  idle: '空闲', stopping: '停止服务', syncing: '同步数据中',
  restarting: '重启服务', verifying: '数据校验', done: '已完成',
}
const PHASE_PCT = {
  idle: 0, stopping: 10, syncing: 45, restarting: 70, verifying: 85, done: 100,
}

const phasePct = computed(() => PHASE_PCT[props.status?.sync_phase] ?? 0)
const phaseLabel = computed(() => PHASE_LABEL[props.status?.sync_phase] ?? '处理中')
// 已运行时长文本：sync_started 是 epoch 秒，和当前时间相减
const elapsedText = computed(() => {
  const s = props.status?.sync_started
  if (!s) return ''
  const sec = Math.max(0, Math.floor(Date.now() / 1000 - s))
  const h = Math.floor(sec / 3600)
  const m = String(Math.floor((sec % 3600) / 60)).padStart(2, '0')
  const ss = String(sec % 60).padStart(2, '0')
  return h ? `${h}:${m}:${ss}` : `${m}:${ss}`
})

const lastSync = computed(() => props.status?.last_sync ?? null)
const exitCode = computed(() => props.status?.exit_code ?? null)

const light = computed(() => {
  if (props.status?.sync_running) return { tone: 'brand', word: `同步中 · ${phaseLabel.value}` }
  if (exitCode.value == null) return { tone: 'muted', word: '空闲 · 尚无同步记录' }
  return exitCode.value === 0
    ? { tone: 'ok', word: '空闲 · 上次成功' }
    : { tone: 'err', word: '空闲 · 上次失败' }
})
const metaText = computed(() => {
  if (!lastSync.value) return '尚未执行过同步，点右侧按钮启动首次同步'
  return `上次同步 ${lastSync.value.ts} · 下载 ${lastSync.value.downloads ?? '—'} 个文件`
})
</script>

<style scoped>
.status-row {
  display: flex;
  align-items: center;
  gap: 12px;
  flex-wrap: wrap;
}
.status-main {
  display: flex;
  flex-direction: column;
  gap: 2px;
  margin-right: auto; /* 摘要靠左、按钮靠右，窄屏自动换行 */
}
.status-word {
  font-size: 15px;
  font-weight: 700;
  color: var(--text);
}
/* 同步横幅：正在同步时显示阶段进度 */
.sync-banner {
  display: flex;
  flex-direction: column;
  gap: 8px;
  padding: 12px;
  border-radius: 8px;
  background: var(--panel2);
}
.sync-meta {
  display: flex;
  align-items: center;
  gap: 12px;
}
</style>
