<template>
  <!-- 港股日K 同步面板（0.10.18 自 views/OpsSync.vue 拆出）。
       域自含：状态机在本组件内经 useHk() 组合（不涉其它域，无需上提）。 -->
  <section class="card">
    <h3 class="card-title">港股日K 同步</h3>
    <p class="card-hint">
      拉取港股日K 写入私有表 <code>hk日k:</code>（东财优先、腾讯降级），按代码隔离存储。代码逗号/空格分隔，如
      <code>00700, 00941</code>。
    </p>
    <div class="actions">
      <el-input
        v-model="hkCodes"
        placeholder="港股代码，逗号分隔，如 00700,00941"
        style="width: 320px"
        @keyup.enter="doHkSync"
      />
      <el-input-number v-model="hkYears" :min="1" :max="10" :step="1" />
      <span class="hint">年数（保留最近 N 年日K）</span>
      <el-button type="primary" :loading="hkBusy" :icon="Download" @click="doHkSync">开始同步</el-button>
    </div>
    <!-- 结果卡：每只代码一行，成功显示写入根数，失败显示后端 error 文案 -->
    <el-table v-if="hkResult.length" :data="hkResult" size="small" border class="hk-result">
      <el-table-column prop="code" label="代码" width="110" />
      <el-table-column label="状态" width="90">
        <template #default="{ row }">
          <el-tag :type="row.ok ? 'success' : 'danger'" size="small">{{ row.ok ? '成功' : '失败' }}</el-tag>
        </template>
      </el-table-column>
      <el-table-column label="结果">
        <template #default="{ row }">{{ row.detail }}</template>
      </el-table-column>
    </el-table>
  </section>
</template>

<script setup lang="ts">
import { Download } from '@element-plus/icons-vue'
import { useHk } from '../../composables/use-hk'

const { hkCodes, hkYears, hkBusy, hkResult, doHkSync } = useHk()
</script>

<style scoped>
.hk-result {
  margin-top: 12px;
}
</style>
