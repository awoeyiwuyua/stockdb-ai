<template>
  <!-- ============================================================
       私有存储页（/ops/mydb）——旧面板「私有存储」子页搬迁。
       0.10.18 重构为编排壳：状态机在 composables/use-mydb.js（reactive
       实例经 props.db 下发三面板共享），本文件只做「组合 + 摆放」。
       本页是"手动操作型"页面——表清单 30s 轮询保新鲜（usePolling），
       读取/写入/查询都是用户点按触发；写入属数据改动，必须二次确认。
       ============================================================ -->
  <div class="page">

    <!-- 页头：标题 + 手动刷新（只刷新表清单） -->
    <div class="page-head">
      <h2 class="page-title">私有存储</h2>
      <el-button :icon="Refresh" :loading="db.tablesLoading" @click="db.loadTables(true)">刷新表清单</el-button>
    </div>

    <!-- 非阻塞错误条：轮询失败时不打断使用 -->
    <el-alert
      v-if="db.tablesError && db.tables.length"
      class="page-alert"
      type="error"
      :title="db.tablesError"
      show-icon
      :closable="true"
      @close="db.tablesError = null"
    />

    <!-- ① 加载态：骨架屏 -->
    <el-skeleton v-if="db.tablesLoading && !db.tables.length" :rows="5" animated />

    <!-- ② 错误态：表清单都拿不到 → EmptyState + 重试 -->
    <EmptyState
      v-else-if="db.tablesError && !db.tables.length"
      icon="WarningFilled"
      title="私有存储不可用"
      :description="db.tablesError"
    >
      <el-button type="primary" @click="db.loadTables(true)">重试</el-button>
    </EmptyState>

    <!-- ③ 正常态 -->
    <template v-else>
      <!-- 顶部指标：自定义表数量（getTables() 已过滤上游保留表） -->
      <StatGrid>
        <StatCard label="自定义表" :value="db.tables.length" tone="brand" sub="已过滤上游保留表" />
      </StatGrid>

      <div class="two-col">
        <MydbReadPanel :db="db" @read="db.doRead" />
        <MydbWritePanel :db="db" @write="db.doWrite" />
      </div>

      <MydbQueryConsole :db="db" @query="db.doQuery" />

    </template>
  </div>
</template>

<script setup lang="ts">
// 编排壳：图标 + 空态/指标卡 + mydb/ 域组件 + use-mydb 状态机 + 统一轮询。
import { Refresh } from '@element-plus/icons-vue'
import StatCard from '../components/StatCard.vue'
import StatGrid from '../components/common/StatGrid.vue'
import EmptyState from '../components/EmptyState.vue'
import MydbReadPanel from '../components/mydb/MydbReadPanel.vue'
import MydbWritePanel from '../components/mydb/MydbWritePanel.vue'
import MydbQueryConsole from '../components/mydb/MydbQueryConsole.vue'
import { useMydb } from '../composables/use-mydb'
import { usePolling } from '../composables/use-polling'

// 状态机实例（reactive）：下发三面板共享，壳里只处理轮询
const db = useMydb()

// 轮询：usePolling 统一节拍（可见 30s / 后台降频；只静默刷新清单，读取结果不动）
usePolling(() => db.loadTables(), { immediate: true })
</script>

<style scoped>
/* 指标卡栅格骨架在公共件 components/common/StatGrid.vue，本页无局部差异 */

/* 两栏布局：宽屏 读取 | 写入 并排，窄屏自动堆叠 */
.two-col {
  display: grid;
  grid-template-columns: repeat(auto-fit, minmax(340px, 1fr));
  gap: 20px;
  align-items: start;
}
</style>
