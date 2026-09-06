// nav.test.js — 导航配置纯数据单测（Vitest）。运行：npm run test
// W1 驾驶舱重设计批 3：导航收敛为 驾驶舱 + 数据同步两页；其余旧页转为抽屉
// 内容组件（路由删除，LEGACY_REDIRECTS 以 {path,query} 落驾驶舱自动展开抽屉）。

import { describe, it, expect } from 'vitest'
import { TOP_ITEMS, NAV_GROUPS, NAV_ITEMS, LEGACY_REDIRECTS } from './nav'

describe('TOP_ITEMS：驾驶舱 + 数据同步两页', () => {
  it('恰好 2 项且 path 依次为 / 与 /ops/sync', () => {
    expect(TOP_ITEMS).toHaveLength(2)
    expect(TOP_ITEMS.map((t) => t.path)).toEqual(['/', '/ops/sync'])
  })
})

describe('NAV_GROUPS：批 3 起无分组', () => {
  it('分组为空数组（抽屉承接原运维子页）', () => {
    expect(NAV_GROUPS).toEqual([])
  })
})

describe('NAV_ITEMS：展平全量菜单', () => {
  it('共 2 项且与 TOP_ITEMS 一致', () => {
    expect(NAV_ITEMS).toHaveLength(2)
    expect(NAV_ITEMS.map((it) => it.path)).toEqual(['/', '/ops/sync'])
  })

  it('path 全唯一', () => {
    const paths = NAV_ITEMS.map((it) => it.path)
    expect(new Set(paths).size).toBe(paths.length)
  })

  it('不含 /paper、/overview、已抽屉化的旧路径', () => {
    const paths = NAV_ITEMS.map((it) => it.path)
    for (const prefix of ['/paper', '/overview', '/ops/alerts', '/ops/logs', '/ops/mcp', '/ops/mydb', '/ops/health', '/ops/diag']) {
      expect(paths.some((p) => p.startsWith(prefix))).toBe(false)
    }
  })
})

describe('LEGACY_REDIRECTS：老书签兜底（字符串或 {path,query}）', () => {
  it('字符串目标直接落页', () => {
    expect(LEGACY_REDIRECTS['/overview']).toBe('/')
    expect(LEGACY_REDIRECTS['/data/sync']).toBe('/ops/sync')
    expect(LEGACY_REDIRECTS['/data']).toBe('/ops/sync')
    expect(LEGACY_REDIRECTS['/paper']).toBe('/')
  })

  it('抽屉目标携带 drawer query（旧四页 + 健康/诊断聚合）', () => {
    expect(LEGACY_REDIRECTS['/ops/alerts']).toEqual({ path: '/', query: { drawer: 'alerts' } })
    expect(LEGACY_REDIRECTS['/ops/logs']).toEqual({ path: '/', query: { drawer: 'logs' } })
    expect(LEGACY_REDIRECTS['/ops/mydb']).toEqual({ path: '/', query: { drawer: 'query' } })
    expect(LEGACY_REDIRECTS['/ops/health']).toEqual({ path: '/', query: { drawer: 'diag' } })
    expect(LEGACY_REDIRECTS['/ops/diag']).toEqual({ path: '/', query: { drawer: 'diag' } })
  })

  it('MCP 观测落诊断抽屉并带 mcp 标签', () => {
    expect(LEGACY_REDIRECTS['/ops/mcp']).toEqual({ path: '/', query: { drawer: 'diag', tab: 'mcp' } })
  })

  it('对象目标 path 都在 NAV_ITEMS 集合内（重定向不 404）', () => {
    const valid = new Set(NAV_ITEMS.map((it) => it.path))
    Object.values(LEGACY_REDIRECTS).forEach((target) => {
      const p = typeof target === 'string' ? target : target.path
      expect(valid.has(p), `重定向目标 ${p} 不在菜单里`).toBe(true)
    })
  })
})
