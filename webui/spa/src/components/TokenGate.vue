<template>
  <!-- token 门禁登录卡片（0.10.27 四件套之四的前端半边）。
       触发：任一 /api 请求被后端 401 拒绝（http.ts onUnauthorized 广播）。
       提交：setToken 存 localStorage → emit unlock（App 层 reload 重新拉全量）。 -->
  <div class="gate-mask">
    <div class="gate-card">
      <el-icon class="gate-icon"><Lock /></el-icon>
      <h2 class="gate-title">访问验证</h2>
      <p class="gate-desc">
        本面板已启用访问令牌保护。<br />
        输入部署时设定的 WEBUI_TOKEN，本浏览器只需验证一次。
      </p>
      <el-input
        v-model="input"
        class="gate-input"
        placeholder="访问令牌"
        clearable
        show-password
        :class="{ 'is-error': shake }"
        @keyup.enter="submit"
      />
      <p v-if="hint" class="gate-hint">{{ hint }}</p>
      <el-button type="primary" class="gate-btn" :loading="busy" @click="submit">
        保存并进入
      </el-button>
    </div>
  </div>
</template>

<script setup lang="ts">
// 学习点：401 广播（http.ts）→ 这里收集令牌；真正校验在下次请求时发生——
// 前端不做本地比对（无意义），错误令牌会让 reload 后的 snapshot 再次 401 回到本卡。
import { ref } from 'vue'
import { setToken } from '../api/http'

const emit = defineEmits<{ (e: 'unlock'): void }>()

const input = ref('')
const busy = ref(false)
const hint = ref('')
const shake = ref(false)

async function submit() {
  const t = input.value.trim()
  if (!t) {
    hint.value = '请输入访问令牌'
    shake.value = true
    setTimeout(() => (shake.value = false), 400)
    return
  }
  busy.value = true
  setToken(t)
  emit('unlock') // App 层 reload：全量请求带新令牌重来
}
</script>

<style scoped>
/* 全屏遮罩 + 居中白卡（Apple 式：雾面背景 + 大圆角卡，样式全部走 tokens） */
.gate-mask {
  position: fixed;
  inset: 0;
  z-index: 3000;
  display: flex;
  align-items: center;
  justify-content: center;
  background: var(--bg);
}
.gate-card {
  width: min(380px, 90vw);
  background: var(--panel);
  border: 1px solid var(--line-soft);
  border-radius: var(--radius-lg);
  box-shadow: var(--shadow-card);
  padding: 36px 32px 32px;
  display: flex;
  flex-direction: column;
  align-items: center;
  text-align: center;
  gap: 12px;
}
.gate-icon {
  font-size: 30px;
  color: var(--brand);
}
.gate-title {
  margin: 0;
  font-size: 20px;
  font-weight: 700;
  color: var(--text);
  letter-spacing: -0.01em;
}
.gate-desc {
  margin: 0 0 6px;
  font-size: 13px;
  line-height: 1.7;
  color: var(--muted);
}
.gate-input {
  width: 100%;
}
.gate-input.is-error :deep(.el-input__wrapper) {
  box-shadow: 0 0 0 3px color-mix(in srgb, var(--err) 20%, transparent) !important;
}
.gate-hint {
  margin: 0;
  font-size: 12.5px;
  color: var(--err);
}
.gate-btn {
  width: 100%;
  margin-top: 6px;
}
</style>
