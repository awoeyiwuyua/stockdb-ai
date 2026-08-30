<template>
  <!-- 数据写入（0.10.18 自 views/OpsMydb.vue 拆出）。状态机在 props.db，动作经 emit。 -->
  <section class="card">
    <h3 class="card-title">数据写入</h3>
    <p class="card-hint">
      写入私有表（表名不存在会自动创建，但不得覆盖上游保留表如 日k: / 股票代码）。写入前需二次确认。
    </p>
    <div class="form-row">
      <span class="label">表名</span>
      <!-- allow-create：既可从现有表选，也可直接输入新表名 -->
      <el-select
        v-model="db.writeTable"
        filterable
        allow-create
        default-first-option
        placeholder="选择或输入表名"
        style="width: 220px"
      >
        <el-option v-for="t in db.tables" :key="t" :label="t" :value="t" />
      </el-select>
    </div>
    <div class="form-row">
      <el-radio-group v-model="db.writeMode">
        <el-radio-button value="single">单条 key/value</el-radio-button>
        <el-radio-button value="batch">批量 items</el-radio-button>
      </el-radio-group>
    </div>

    <!-- 单条模式 -->
    <template v-if="db.writeMode === 'single'">
      <div class="form-row">
        <span class="label">key</span>
        <el-input v-model="db.writeKey" placeholder="键名（如 20260814）" style="width: 220px" />
      </div>
      <div class="form-row">
        <span class="label">value</span>
        <!-- 注意：placeholder 里含双引号，属性定界符改用单引号（HTML 属性里不能裸写 "） -->
        <el-input
          v-model="db.writeValue"
          type="textarea"
          :rows="4"
          placeholder='写入的值：任意 JSON（数字/字符串/对象），例如 {"date": 20260814, "close": 5.2}'
        />
      </div>
    </template>

    <!-- 批量模式：直接贴 JSON -->
    <template v-else>
      <div class="form-row">
        <el-input
          v-model="db.batchPayload"
          type="textarea"
          :rows="5"
          placeholder='批量格式：[["k1", 值1], ["k2", 值2]]  或  {"items": [["k1", 值1], ...]}'
        />
      </div>
    </template>

    <div class="form-row">
      <el-button type="primary" :icon="EditPen" :loading="db.writing" @click="emit('write')">写入数据</el-button>
    </div>
  </section>
</template>

<script setup>
import { EditPen } from '@element-plus/icons-vue'

defineProps({
  db: { type: Object, required: true },
})

const emit = defineEmits(['write'])
</script>

<style scoped>
.form-row {
  display: flex;
  align-items: center;
  flex-wrap: wrap;
  gap: 8px;
  margin-bottom: 10px;
}
.label {
  width: 56px;
  font-size: 13px;
  color: var(--muted);
}
</style>
