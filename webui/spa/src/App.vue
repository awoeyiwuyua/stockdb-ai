<template>
  <!-- M1 应用外壳：左侧侧边栏 + 右侧（上=状态栏，下=路由内容区）上下结构 -->
  <div class="app-shell">
    <!-- 侧边栏：collapsed 由 App 统一管理，作为 prop 传给 SideNav -->
    <SideNav :collapsed="collapsed" />

    <!-- 右侧主区域 -->
    <div class="app-main">
      <!-- 状态栏自己会读 store，我们只需传折叠状态、监听它的折叠事件 -->
      <StatusBar :collapsed="collapsed" @toggle-collapse="collapsed = !collapsed" />

      <!-- 内容区：RouterView 渲染当前路由对应的页面组件 -->
      <main class="app-content">
        <RouterView />
      </main>
    </div>

    <!-- token 门禁登录卡片（0.10.27）：任一 /api 请求被 401 拒绝时全屏弹出 -->
    <TokenGate v-if="unauthorized" @unlock="reload" />
  </div>
</template>

<script setup lang="ts">
// 学习点：
// 1) ref 定义响应式状态，模板里直接用；事件 @xx 绑定处理函数。
// 2) onMounted 做首次数据拉取 + 开启轮询；onUnmounted 清理定时器和监听，防止泄漏。
// 3) 轮询节拍（可见 30s / 后台 5min / 回前台补拉）已提取为 composables/use-polling.ts
//    ——App 是它的第一个使用者，页面级轮询（如 OpsSync）复用同一套策略（0.10.18）。
// 4) 0.10.27：全局取数收敛为 /api/snapshot 单通道（store.refresh 一拍拿全）。
import { ref } from 'vue'
import SideNav from './layout/SideNav.vue'
import StatusBar from './layout/StatusBar.vue'
import TokenGate from './components/TokenGate.vue'
import { useGlobalStore } from './stores/global'
import { usePolling } from './composables/use-polling'
import { onUnauthorized } from './api/http'

// 全局数据仓库：refresh() 拉一次 /api/snapshot，各组件通过 getter 读取
const store = useGlobalStore()

// 侧边栏折叠状态：SideNav 用 prop 读它，StatusBar 用事件改它（单向数据流）
const collapsed = ref(false)

// token 门禁：在 setup 同步注册（早于首个请求响应到达），401 → 弹登录卡片
const unauthorized = ref(false)
onUnauthorized(() => (unauthorized.value = true))
// 令牌已存 localStorage，整页 reload 让全部请求带新令牌重来（校验在后端）
const reload = () => window.location.reload()

// 全局轮询：首拉一次 + 可见性感知节拍（策略在 use-polling，此处只声明"轮询什么"）
usePolling(() => store.refresh(), { immediate: true, slow: 5 * 60_000 })
</script>

<style scoped>
/* 外层横向 flex：左=侧边栏，右=主区域 */
.app-shell {
  display: flex;
  min-height: 100vh;
}

/* 右侧主区域纵向 flex：上=状态栏，下=内容区 */
.app-main {
  flex: 1; /* 占满剩余宽度 */
  min-width: 0; /* 防止内容过宽把 flex 布局撑破 */
  display: flex;
  flex-direction: column;
}

/* 内容区：Apple 式内容列——留白呼吸 + 居中限宽（1200px），不让卡片贴边 */
.app-content {
  flex: 1;
  padding: 32px 36px 48px;
  width: 100%;
  max-width: 1200px;
  margin: 0 auto;
  box-sizing: border-box;
  overflow: auto; /* 内容超高时在区域内滚动，不撑破整体 */
}
@media (max-width: 768px) {
  .app-content {
    padding: 20px 16px 32px;
  }
}
</style>
