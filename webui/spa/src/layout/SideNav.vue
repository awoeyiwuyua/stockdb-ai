<template>
  <!-- Apple 皮肤侧栏：毛玻璃白底 + 圆角胶囊高亮菜单项（形态在 skin.css .el-menu 系列）。
       折叠状态、路由高亮、徽标逻辑与旧版完全一致，仅换皮肤。 -->
  <div class="side-nav" :class="{ collapsed }">
    <div class="side-logo">
      <span class="logo-mark" aria-hidden="true">
        <svg viewBox="0 0 28 28" width="28" height="28">
          <defs>
            <linearGradient id="spark" x1="0" y1="0" x2="1" y2="1">
              <stop offset="0" stop-color="#0a84ff" />
              <stop offset="1" stop-color="#0055d4" />
            </linearGradient>
          </defs>
          <rect x="0" y="0" width="28" height="28" rx="8" fill="url(#spark)" />
          <path
            d="M6 17.5 L10.5 13 L14 16 L22 8"
            fill="none"
            stroke="#fff"
            stroke-width="2.4"
            stroke-linecap="round"
            stroke-linejoin="round"
          />
          <circle cx="22" cy="8" r="2" fill="#fff" />
        </svg>
      </span>
      <span v-show="!collapsed" class="logo-text">stockdb 控制台</span>
    </div>
    <el-menu
      :default-active="route.path"
      :collapse="collapsed"
      :collapse-transition="false"
      router
      class="side-menu"
    >
      <!-- 总览：单页项（LuCI 的 Status→Overview 模式） -->
      <el-menu-item v-for="it in TOP_ITEMS" :key="it.path" :index="it.path">
        <el-icon><component :is="it.icon" /></el-icon>
        <template #title>{{ it.title }}</template>
      </el-menu-item>

      <!-- 分组：系统运维 / 模拟盘（el-sub-menu 可折叠，子项一页一职责） -->
      <el-sub-menu v-for="g in NAV_GROUPS" :key="g.title" :index="g.title">
        <template #title>
          <el-icon><component :is="g.icon" /></el-icon>
          <span>{{ g.title }}</span>
        </template>
        <el-menu-item v-for="it in g.items" :key="it.path" :index="it.path">
          <el-icon><component :is="it.icon" /></el-icon>
          <template #title>
            <el-badge
              v-if="it.badge"
              :value="store[it.badge]"
              :hidden="!store[it.badge]"
              type="danger"
              class="nav-badge"
            >
              <span>{{ it.title }}</span>
            </el-badge>
            <span v-else>{{ it.title }}</span>
          </template>
        </el-menu-item>
      </el-sub-menu>
    </el-menu>

    <div class="side-foot" v-show="!collapsed">
      <span class="side-foot-text">数据基座 · 运维台</span>
    </div>
  </div>
</template>

<script setup>
// 学习点：el-sub-menu 分组菜单树（LuCI 风格）；props 折叠开关；
// el-menu router 模式 = 点菜单即跳路由，default-active 用当前路由路径高亮。
import { useRoute } from 'vue-router'
import { TOP_ITEMS, NAV_GROUPS } from './nav.js'
import { useGlobalStore } from '../stores/global.js'

defineProps({
  collapsed: { type: Boolean, default: false },
})

const route = useRoute()
const store = useGlobalStore() // 通知中心红点徽标数据源
</script>

<style scoped>
.side-nav {
  height: 100vh;
  position: sticky;
  top: 0;
  display: flex;
  flex-direction: column;
  border-right: 1px solid var(--glass-border);
  background: var(--glass-bg);
  backdrop-filter: blur(24px) saturate(180%);
  -webkit-backdrop-filter: blur(24px) saturate(180%);
  width: 64px;
  flex-shrink: 0;
  z-index: 10;
  transition: width 0.2s ease;
}
.side-nav:not(.collapsed) {
  width: 248px;
}
.side-logo {
  display: flex;
  align-items: center;
  gap: 10px;
  padding: 16px 20px 14px;
  font-weight: 700;
  font-size: 15px;
  letter-spacing: -0.01em;
  min-height: 64px;
  border-bottom: 1px solid var(--glass-border);
}
.logo-mark {
  display: flex;
  align-items: center;
  filter: drop-shadow(0 2px 6px rgba(0, 113, 227, 0.35));
}
.logo-text {
  white-space: nowrap;
}
.side-menu {
  flex: 1;
  padding: 12px 0;
  overflow-y: auto;
  overflow-x: hidden;
}
.nav-badge {
  width: 100%;
}
.nav-badge :deep(.el-badge__content) {
  transform: none;
  position: static;
  margin-left: 6px;
  border-radius: 999px;
}
.side-foot {
  padding: 12px 24px 16px;
  border-top: 1px solid var(--glass-border);
}
.side-foot-text {
  font-size: 11px;
  color: var(--muted);
  letter-spacing: 0.02em;
}
</style>
