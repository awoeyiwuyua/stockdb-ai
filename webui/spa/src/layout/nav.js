// nav.js — 左侧导航唯一配置源（纯数据，可单测）。
// W1 驾驶舱重设计批 3（docs/design/webui-cockpit-redesign.md）：导航收敛为
// 驾驶舱 + 数据同步两页；私有存储/日志/通知/MCP 四页转为驾驶舱抽屉内容组件
//（视图文件保留、路由删除，旧路径经 LEGACY_REDIRECTS 落 '/?drawer=' 自动展开）。
export const TOP_ITEMS = [
  { path: '/', title: '驾驶舱', icon: 'Odometer' },
  { path: '/ops/sync', title: '数据同步', icon: 'Refresh' },
]

export const NAV_GROUPS = []

// 展平：全部页面（含分组信息，测试保证 path 唯一）
export const NAV_ITEMS = [
  ...TOP_ITEMS,
  ...NAV_GROUPS.flatMap((g) => g.items.map((it) => ({ ...it, group: g.title }))),
]

// 旧路径 → 新地址（路由 redirect 兜底，老书签不 404）。
// 批 3：值为字符串直接跳转；值为 {path, query} 落驾驶舱并自动展开对应抽屉。
// drawer 取值：alerts / logs / diag（含 MCP 标签）/ query。
export const LEGACY_REDIRECTS = {
  '/overview': '/',
  '/ops/health': { path: '/', query: { drawer: 'diag' } },
  '/ops/diag': { path: '/', query: { drawer: 'diag' } },
  '/ops/version': '/',
  '/ops/alerts': { path: '/', query: { drawer: 'alerts' } },
  '/ops/logs': { path: '/', query: { drawer: 'logs' } },
  '/ops/mcp': { path: '/', query: { drawer: 'diag', tab: 'mcp' } },
  '/ops/mydb': { path: '/', query: { drawer: 'query' } },
  '/data/sync': '/ops/sync',
  '/data/mydb': { path: '/', query: { drawer: 'query' } },
  '/data': '/ops/sync',
  '/alerts': { path: '/', query: { drawer: 'alerts' } },
  '/paper': '/',
  '/paper/audit': '/',
  '/paper/signal': '/',
}
