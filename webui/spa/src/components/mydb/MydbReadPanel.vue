<template>
  <!-- 表清单与读取（0.10.18 自 views/OpsMydb.vue 拆出）。
       状态机在 props.db（use-mydb 的 reactive 实例，多面板共享），动作经 emit。 -->
  <section class="card">
    <h3 class="card-title">表清单与读取</h3>
    <div class="form-row">
      <span class="label">选择表</span>
      <el-select v-model="db.selTable" filterable placeholder="选择自定义表" style="width: 220px">
        <el-option v-for="t in db.tables" :key="t" :label="t" :value="t" />
      </el-select>
    </div>
    <div class="form-row">
      <span class="label">key</span>
      <el-input
        v-model="db.readKey"
        placeholder="留空 = 列出表内全部键值"
        style="width: 220px"
        @keyup.enter="emit('read')"
      />
      <el-button type="primary" :icon="Search" :loading="db.reading" @click="emit('read')">读取</el-button>
    </div>

    <!-- 读取结果区：单 key 结果 / 全键列表 两种形态 -->
    <div class="read-result">
      <!-- 读取失败：错误文案优先展示（失败时 readResult 会清空，必须先判 readError） -->
      <el-alert v-if="db.readError" type="error" :title="db.readError" show-icon :closable="false" />
      <!-- 未读取过：引导提示 -->
      <EmptyState
        v-else-if="!db.readResult"
        icon="Reading"
        title="尚未读取"
        description="选择表后点击「读取」：key 留空会列出表内全部键值。"
      />
      <!-- 单条读取：value 可能是任意 JSON，用格式化文本展示 -->
      <template v-else-if="db.readSingle">
        <div class="sub-title">读取结果：{{ db.readSingle.table }}:{{ db.readSingle.key }}</div>
        <pre class="val-pre">{{ db.pretty(db.readSingle.value) }}</pre>
      </template>
      <!-- 全键列表：键 → 值 表格 -->
      <template v-else-if="db.readList">
        <EmptyState
          v-if="!db.readListRows.length"
          icon="Box"
          title="该表暂无数据"
          description="可到右侧「数据写入」区写入第一条记录。"
        />
        <template v-else>
          <div class="sub-title">
            共 {{ db.readListRows.length }} 个键（{{ db.readList.table }}）
          </div>
          <el-table :data="db.readListRows" size="small" border max-height="300">
            <el-table-column prop="key" label="键" width="140" show-overflow-tooltip />
            <el-table-column prop="val" label="值" show-overflow-tooltip />
          </el-table>
        </template>
      </template>
    </div>
  </section>
</template>

<script setup>
import { Search } from '@element-plus/icons-vue'
import EmptyState from '../EmptyState.vue'

defineProps({
  // use-mydb 的 reactive 状态机实例（壳创建后传下，多面板共享）
  db: { type: Object, required: true },
})

const emit = defineEmits(['read'])
</script>

<style scoped>
/* .form-row 已收全局 styles/form.css（三面板逐字重复，0.10.18 第三批提取） */
.label {
  width: 56px;
  font-size: 13px;
  color: var(--muted);
}
.sub-title {
  font-size: 13px;
  color: var(--text);
  margin: 4px 0 8px;
}
.read-result {
  margin-top: 8px;
}
.val-pre {
  margin: 0;
  padding: 12px 14px;
  max-height: 240px;
  overflow: auto;
  background: var(--panel2);
  border: 1px solid var(--line-soft);
  border-radius: 14px;
  font: 12px/1.55 var(--font-mono);
  color: var(--text);
  white-space: pre-wrap;
  word-break: break-all;
}
</style>
