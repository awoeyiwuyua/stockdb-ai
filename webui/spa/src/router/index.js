// router/index.js — 路由表从 nav.js 单一配置源生成，页面组件懒加载。
// W1 驾驶舱重设计批 3：路由仅剩 驾驶舱 + 数据同步；其余旧页转为驾驶舱抽屉
// 内容组件，旧路径经 LEGACY_REDIRECTS（字符串或 {path,query}）重定向兜底。
import { createRouter, createWebHistory } from 'vue-router'
import { NAV_ITEMS, LEGACY_REDIRECTS } from '../layout/nav.js'

// 路径 → 页面组件（懒加载函数）
const VIEWS = {
  '/': () => import('../views/Cockpit.vue'),
  '/ops/sync': () => import('../views/OpsSync.vue'),
}

const routes = [
  ...NAV_ITEMS.map((it) => ({
    path: it.path,
    component: VIEWS[it.path],
    meta: { title: it.title, group: it.group, icon: it.icon },
  })),
  // 旧路径兜底：老书签/旧顶栏链接跳转到新地址
  ...Object.entries(LEGACY_REDIRECTS).map(([from, to]) => ({
    path: from,
    redirect: to,
  })),
]

const router = createRouter({
  history: createWebHistory(),
  routes,
})

router.afterEach((to) => {
  document.title = `${to.meta.title || ''} · stockdb 控制台`
})

// 路由级错误兜底：懒加载失败/重定向异常时记录，不让导航静默失败
router.onError((err) => {
  console.error('[webui] 路由异常', err)
})

export default router
