// nav.js — 左侧导航唯一配置源（纯数据，可单测）。
// W1 驾驶舱重设计（docs/design/webui-cockpit-redesign.md）：菜单 = 驾驶舱单页 +
// 系统运维分组（批 1：系统健康/诊断中心并入驾驶舱抽屉的过渡期先移除独立入口，
// 其信息由四灯 + 诊断抽屉承接）；每项一个职责、一条 URL；badge 挂通知中心。
export const TOP_ITEMS = [
  { path: '/', title: '驾驶舱', icon: 'Odometer' },
]

export const NAV_GROUPS = [
  {
    title: '系统运维',
    icon: 'Setting',
    items: [
      { path: '/ops/sync', title: '数据同步', icon: 'Refresh' },
      { path: '/ops/mydb', title: '私有存储', icon: 'Coin' },
      { path: '/ops/logs', title: '日志中心', icon: 'Document' },
      { path: '/ops/alerts', title: '通知中心', icon: 'Bell', badge: 'alertCount' },
      { path: '/ops/mcp', title: 'MCP 观测', icon: 'Monitor' },
    ],
  },
]

// 展平：全部页面（含分组信息，测试保证 path 唯一）
export const NAV_ITEMS = [
  ...TOP_ITEMS,
  ...NAV_GROUPS.flatMap((g) => g.items.map((it) => ({ ...it, group: g.title }))),
]

// 旧路径 → 新地址（路由 redirect 兜底，老书签不 404）。
// W1 批 1：/overview、/ops/health、/ops/diag 并入驾驶舱（'/'）；批 3 起带
// ?drawer= 参数自动展开对应抽屉（此处先落 '/'，抽屉参数在 Cockpit 内解析）。
export const LEGACY_REDIRECTS = {
  '/overview': '/',
  '/ops/health': '/',
  '/ops/diag': '/',
  '/ops/version': '/',
  '/data/sync': '/ops/sync',
  '/data/mydb': '/ops/mydb',
  '/data': '/ops/sync',
  '/alerts': '/ops/alerts',
  '/paper': '/',
  '/paper/audit': '/',
  '/paper/signal': '/',
}
