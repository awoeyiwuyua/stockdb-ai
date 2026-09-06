# webui「从零四件套」设计留痕（0.10.27）

> 缘起：用户问「如果从 0 开始设计当前项目的 webui，你会怎么做」。答案是：终点形态
> （两页 + 四灯 + 时间线 + 资产卡 + 抽屉）已经由 W1 六批重构走到，从零重做的价值不在
> 换样子，而在把"长出来的"变成"设计出来的"。本文记录 2026-09-06 一次落地四件增量的
> 决策与验收，用户原话指令：「现在就开干这四件」。

## 四件是什么

| # | 增量 | 一句话 |
|---|------|--------|
| 1 | `/api/snapshot` 聚合 | 驾驶舱轮询 5 路收敛 1 路（同一瞬间一致视图） |
| 2 | TypeScript 化 | 类型守数据契约；推导层抽纯函数独打 |
| 3 | tokens 单一来源 | 变量只住在 tokens.css；组件禁止裸写色值 |
| 4 | token 门禁 | 出内网（节点小宝等隧道）最低限度兜底 |

## 决策留痕

### 1. snapshot 聚合
- `GET /api/snapshot?days=7` = `overview + status + schedule + warehouse + timeline{days,totals}`
  五块，逐块 `_safe` 降级（任一子块异常只影响该块，整体恒 200）。
- 后端把 `_status`/`_overview` 载荷体抽为模块函数 `status_payload()`/`overview_payload()`，
  snapshot 与旧端点共用同一实现（旧端点契约不变，legacy/MCP 不受影响）。
- `schedule` 块为扁平契约（直接放配置对象），不复用 /api/schedule 的 `{"schedule":...}` 包裹。
- **warehouse_totals 加 60s TTL 缓存**：facts glob 每次数万文件名，进 15s 轮询后不能每拍
  扫盘；`force=True` 供备份落盘后即时刷新。测试时钟注入 `app._monotonic`。
- 契约守卫：`test_ops.SnapshotPayloadTest` 断言五块键集 + timeline 走 `app.*` 动态引用
  （from-import 快照教训）；前端 `types/api.ts` 手写镜像，前端消费面字段全部可空化。

### 2. TypeScript 化
- 全量：19 个 .js 模块（api/composables/stores/layout/router/utils/main）+ 30 个 SFC
  全部 `lang="ts"`；`tsconfig` strict；`build` 门 = `vue-tsc --noEmit && vite build`。
- 状态推导层抽出为 `src/domain/lights.ts` 纯函数（零 Vue 依赖）：`deriveLights` /
  `worstTone` / `aggregateWord` / `hasWhError24h`，`domain/lights.test.ts` 直测 §2.2 口径。
  动机：调度器存活恒假、布尔快照两个事故都出在推导层而非渲染层。
- store（Pinia）切 snapshot 单通道：state.snapshot；getter 面保持旧名
  （health/alertCount/mcp/version/lagDays）+ 新块（status/schedule/warehouse/timelineDays/
  whTotals/serverVersion）——消费方无感切换。
- 类型哲学：只对"前端真正消费的字段"写严，其余索引签名放行（全量镜像后端字段 =
  维护税）；可空性对齐 null-safety 防线（后端瞬时失败 → 子块 null）。
- 死代码清除：`views/Overview.vue`（540 行）、`api/overview.js`、`use-overview.js`
  （路由批 3 后唯一消费方消失）。
- 生态坑位记录：**vitest 的 hoist transform 会吃掉 `vi.mock(...)` 行尾注释并连带换行**，
  导致下一行被拼上来 → rollup 解析错。`vi.mock` 行禁止行尾注释（注释写上一行）。

### 3. tokens 单一来源
- `styles/tokens.css`（新）只放变量：字体栈 / 表面 / 文字 / 语义色 / Element Plus 桥接
  色阶，浅色 `:root` + 深色 `html.dark`；`base.css` 瘦身为纯元素重置。
- 约定（文件头写死）：新增颜色/圆角/阴影先立令牌再消费；组件样式禁止裸写色值。
- 现状盘点：皮肤批已把组件层基本令牌化（扫描仅 SideNav logo 投影一处 rgba 艺术值、
  SyncTrendChart cssVar 兜底值、skin.css 桥接层三处豁免，均已在文件头留痕）。

### 4. token 门禁
- `config.WEBUI_TOKEN`（env，空 = 关闭，纯内网零负担）；规则纯函数
  `interfaces/web/auth.py::authorized(path, provided, expected)`：
  - `/api/*`（含全部 POST 写路径）必须带 `X-StockDB-Token`（hmac.compare_digest）；
  - 静态资源与 `/legacy` 放行（SPA shell 要先加载，登录卡片才有地方渲染）；
  - **`/mcp` 显式豁免**：Mac 端 MCP 客户端只配了 STOCKDB_HOST，加头会当场断链；
    MCP 面只读，独立鉴权留待客户端支持 header 后再收。
- 前端：`http.ts` 每请求附头（localStorage `webui-token` + 内存兜底）；401 → 广播
  `onUnauthorized` → App 挂 `TokenGate` 登录卡片；令牌校验永远在后端（前端不比对）。
- 已知限制：WEBUI_TOKEN 启用后 `/legacy` 旧面板无法带头发请求，不可用（逃生通道
  本身也被门住）；docker-compose 以 `${WEBUI_TOKEN:-}` 透传，宿主侧 .env 控制。
- healthcheck 不受影响（打引擎 7899，不经 webui）；entrypoint 无内部 /api 依赖。

## 验收（2026-09-06）

- Python：`python -m unittest test_ops` → **106 tests OK**（新增 9：TTL 缓存 3、
  snapshot 聚合 2、token 门禁 4）。
- 前端：`vitest run` → **10 文件 68 用例全绿**（新增 lights 推导 8 + http token 4 +
  store 单通道重写）；`vue-tsc --noEmit` → **0 错误**；`npm run build` 成功
  （构建门已含类型检查）。
- fnOS 部署与浏览器目测见台账 0.10.27 行。
