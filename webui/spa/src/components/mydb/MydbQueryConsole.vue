<template>
  <!-- 查询台（0.10.18 自 views/OpsMydb.vue 拆出）。状态机在 props.db，动作经 emit。 -->
  <section class="card">
    <h3 class="card-title">查询台</h3>
    <p class="card-hint">
      直查 stockdb 任意表（等价于 <code>/?cmd=get&t=表名</code>）。表名可带前缀通配，如
      <code>股票代码</code>、<code>日k:000001:2024*</code>、<code>hk日k:00700*</code>。返回 JSON 自动表格化。
    </p>
    <div class="form-row">
      <el-input
        v-model="db.queryInput"
        placeholder="输入表名或查询语句，如 股票代码"
        style="width: 360px"
        @keyup.enter="emit('query')"
      />
      <el-button type="primary" :icon="Search" :loading="db.querying" @click="emit('query')">查询</el-button>
    </div>

    <!-- 查询结果 -->
    <div class="query-result">
      <!-- 查询失败：错误文案（同时已 ElMessage.error 提示） -->
      <el-alert v-if="db.queryError" type="error" :title="db.queryError" show-icon :closable="false" />
      <!-- 还没查过 -->
      <EmptyState
        v-else-if="!db.queryDone"
        icon="Monitor"
        title="等待查询"
        description="输入表名后回车或点「查询」，返回的 JSON 会按字段展开成表格。"
      />
      <!-- 查过但空返回 -->
      <EmptyState
        v-else-if="!db.queryColumns.length"
        icon="Box"
        title="无返回内容"
        description="该查询没有返回任何数据（可能是空表或返回了非 JSON）。"
      />
      <!-- 正常结果：动态列表格（最多展示前 MAX_ROWS 行，防大表卡死） -->
      <template v-else>
        <div class="sub-title">
          返回 {{ db.queryRows.length }} 行
          <span v-if="db.queryTruncated" class="hint">（内容过多，仅展示前 {{ db.MAX_ROWS }} 行）</span>
        </div>
        <el-table :data="db.queryTableData" size="small" border max-height="420">
          <el-table-column
            v-for="col in db.queryColumns"
            :key="col"
            :prop="col"
            :label="col"
            min-width="120"
            show-overflow-tooltip
          />
        </el-table>
      </template>
    </div>
  </section>
</template>

<script setup lang="ts">
import { Search } from '@element-plus/icons-vue'
import EmptyState from '../EmptyState.vue'
import { useMydb } from '../../composables/use-mydb'

defineProps<{ db: ReturnType<typeof useMydb> }>()

const emit = defineEmits<{ (e: 'query'): void }>()
</script>

<style scoped>
/* .form-row 已收全局 styles/form.css（三面板逐字重复，0.10.18 第三批提取） */
.sub-title {
  font-size: 13px;
  color: var(--text);
  margin: 4px 0 8px;
}
.query-result {
  margin-top: 8px;
}
</style>
