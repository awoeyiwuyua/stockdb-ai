<template>
  <!-- ============================================================
       系统健康页（/ops/health）——任务 G2。
       LuCI 一页一职责：容器 / 磁盘 / 数据健康 / 同步能力 / 日志 / 重启
       全部收在这一个页面，用户不用翻菜单就能判断"系统行不行、要不要动它"。
       0.10.18 重构为编排壳：状态机在 composables/use-health.js（三源并行
       各自降级、busy 互斥、日志懒加载、重启二次确认），容器卡拆
       components/health/HealthContainerCard.vue，本文件只做组合摆放。
       ============================================================ -->
  <div class="page">

    <!-- ============ 页头：小标题 + 右侧操作（LuCI 紧凑风格） ============ -->
    <div class="page-head">
      <h2 class="page-title">系统健康</h2>
      <div class="page-actions">
        <el-button :icon="Refresh" :loading="loading" @click="loadAll(true)">手动刷新</el-button>
      </div>
    </div>

    <!-- 三态一：加载中（骨架屏）——首拉数据没回来前显示 -->
    <el-skeleton v-if="loading && !hasData" :rows="10" animated />

    <!-- 三态二：错误态——首拉就失败且手里没有任何数据 → EmptyState + 重试 -->
    <EmptyState
      v-else-if="error && !hasData"
      icon="WarningFilled"
      title="健康数据加载失败"
      :description="error"
    >
      <el-button type="primary" @click="loadAll(true)">重试</el-button>
    </EmptyState>

    <!-- 三态三：正常态——数据在手，逐块渲染 -->
    <template v-else>
      <!-- 轮询失败但手里有旧数据：顶部一行弱提示，表格照常展示（降级不崩） -->
      <el-alert
        v-if="error"
        class="page-alert"
        type="warning"
        :title="`最近刷新失败：${error}（将自动重试）`"
        show-icon
        @close="error = ''"
      />

      <!-- ================= 1. 健康卡：getHealth() ================= -->
      <section class="card">
        <h3 class="card-title">数据健康</h3>
        <!-- StatCard 组合：latest / lag_days / mirror / status，滞后着色（栅格公共件 StatGrid） -->
        <StatGrid>
          <StatCard
            label="数据最新"
            :value="health ? fmtYMD(health.latest) : '—'"
            :tone="healthTone"
            sub="沪市日 K 最新交易日期"
          />
          <StatCard
            label="滞后天数"
            :value="health?.lag_days != null ? `${health.lag_days} 天` : '—'"
            :tone="healthTone"
            sub="按工作日口径计算"
          />
          <StatCard label="镜像日期" :value="health?.mirror || '—'" sub="镜像源标注的当日日期" />
          <StatCard label="健康状态" :value="statusLabel" :tone="statusTone" sub="正常 / 滞后 / 未知 三态" />
        </StatGrid>
        <!-- note：后端给出的一句话判断（如"可同步" / "镜像尚未发布"），放卡片底部 -->
        <div v-if="health?.note" class="note-line">{{ health.note }}</div>
      </section>

      <!-- ================= 2. 容器卡（拆 components/health/） ================= -->
      <HealthContainerCard
        :container="container"
        :log="containerLog"
        :log-open="containerLogOpen"
        :restarting="restarting"
        :sync-running="!!status?.sync_running"
        @toggle-log="toggleContainerLog"
        @restart="doRestart"
      />

      <!-- ================= 3. 磁盘卡：getStatus().disk ================= -->
      <section class="card">
        <h3 class="card-title">磁盘用量</h3>
        <!-- disk_usage 返回 {total_gb, used_gb, free_gb}（异常时全为 null），
             只有拿到 total 才画进度条，否则给"不可用"降级文案 -->
        <template v-if="diskPct != null">
          <el-progress
            :percentage="diskPct"
            :stroke-width="12"
            :color="diskColor"
            :format="() => `${diskPct}%`"
          />
          <div class="note-line">{{ diskText }}</div>
        </template>
        <div v-else class="hint">磁盘信息不可用（shutil.disk_usage 失败时后端返回 null 字段）</div>
      </section>

      <!-- ================= 4. 同步能力卡：getStatus().sync_cap ================= -->
      <section class="card">
        <h3 class="card-title">同步能力</h3>
        <div v-if="status?.sync_cap" class="cap-block">
          <div class="cap-title">
            能力总览
            <el-tag :type="status.sync_cap.ok ? 'success' : 'danger'" size="small">
              {{ status.sync_cap.ok ? '可用' : '不可用' }}
            </el-tag>
            <el-tag v-if="status.sync_cap.warn" type="warning" size="small">有待重试任务</el-tag>
          </div>
          <!-- checks{updater,source,writable,retry_pending}：逐项小圆点着色 -->
          <ul class="cap-list">
            <li v-for="(check, name) in status.sync_cap.checks || {}" :key="name">
              <span class="cap-dot" :style="{ background: dotColor(check) }" />
              <span class="cap-name">{{ CAP_LABELS[name] || name }}</span>
              <span class="hint">{{ check.detail }}</span>
            </li>
          </ul>
        </div>
        <div v-else class="hint">同步能力信息不可用</div>
      </section>

      <!-- ================= 5. 环境信息卡：getDiag().env ================= -->
      <section class="card">
        <h3 class="card-title">环境信息</h3>
        <el-descriptions v-if="diag?.env" :column="2" border size="small">
          <el-descriptions-item label="Python">{{ diag.env.python || '—' }}</el-descriptions-item>
          <el-descriptions-item label="架构">{{ diag.env.arch || '—' }}</el-descriptions-item>
          <el-descriptions-item label="WebUI 版本">{{ diag.env.webui_version || '—' }}</el-descriptions-item>
          <el-descriptions-item label="界面模式">{{ diag.env.ui_mode || '—' }}</el-descriptions-item>
          <el-descriptions-item label="镜像 tag">{{ diag.env.image_tag || '—' }}</el-descriptions-item>
          <el-descriptions-item label="启动时间">{{ diag.env.started || '—' }}</el-descriptions-item>
          <!-- uptime_seconds 是秒数 → 转成 'X天X时X分'（见 fmtUptimeSec） -->
          <el-descriptions-item label="运行时长">{{ fmtUptimeSec(diag.env.uptime_seconds) }}</el-descriptions-item>
          <el-descriptions-item label="数据目录">{{ diag.env.data_dir || '—' }}</el-descriptions-item>
          <el-descriptions-item label="数据最新">{{ fmtYMD(diag.env.data_latest) }}</el-descriptions-item>
        </el-descriptions>
        <div v-else class="hint">环境信息不可用</div>
        <!-- 底部一行：诊断生成时间 + 跳转完整诊断页（/ops/diag） -->
        <div class="env-foot">
          <span class="hint">
            诊断生成于 {{ diag?.generated_at ? String(diag.generated_at).slice(0, 19).replace('T', ' ') : '—' }}
          </span>
          <router-link class="diag-link" to="/ops/diag">完整诊断 →</router-link>
        </div>
      </section>

    </template>
  </div>
</template>

<script setup>
// 编排壳：图标 + StatCard/EmptyState + health/ 域组件 + use-health 状态机 + 统一轮询
import { Refresh } from '@element-plus/icons-vue'
import StatCard from '../components/StatCard.vue'
import StatGrid from '../components/common/StatGrid.vue'
import EmptyState from '../components/EmptyState.vue'
import HealthContainerCard from '../components/health/HealthContainerCard.vue'
import { useHealth, CAP_LABELS, fmtUptimeSec } from '../composables/use-health.js'
import { fmtYMD } from '../utils/format.js'
import { usePolling } from '../composables/use-polling.js'

const {
  health, status, diag, containerLog, containerLogOpen, loading, error, restarting,
  hasData, container, healthTone, statusLabel, statusTone,
  diskPct, diskColor, diskText,
  loadAll, toggleContainerLog, doRestart, fmtUptime, dotColor,
} = useHealth()

// 轮询：usePolling 统一节拍（可见 30s / 后台降频；只静默刷新，失败写 error 不弹窗）
usePolling(() => loadAll(), { immediate: true })
</script>

<style scoped>
/* 指标卡栅格骨架在公共件 components/common/StatGrid.vue，本页无局部差异 */
.note-line {
  margin-top: 8px;
  font-size: 12px;
  color: var(--muted);
}
/* 能力检查列表 */
.cap-block {
  border-top: 1px dashed var(--line);
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
.cap-dot {
  width: 8px;
  height: 8px;
  border-radius: 50%;
  display: inline-block;
}
.cap-name {
  color: var(--text);
}
.env-foot {
  display: flex;
  justify-content: space-between;
  margin-top: 10px;
  align-items: center;
}
.diag-link {
  font-size: 12px;
  color: var(--brand);
  text-decoration: none;
}
.diag-link:hover {
  text-decoration: underline;
}
</style>
