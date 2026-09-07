# 发布纪律（Release Policy）

> 目标：一次发布 = 一批改动 + 一个版本号 + 一次验证 + 一条记录。
> 起因：2026-08-15 一天 6 版（0.6.1→0.6.6），每个 commit 都发版、无 tag、无 CHANGELOG，
> 用户反复部署且无法判断"哪个版本是稳的"。本文件把纪律制度化。
> **0.8.18 修订：开发主线 = 本机 Windows 原生引擎模式（见 `docs/development-guide.md`）；
> docker 镜像为可选发布物，仅版本成熟后才构建。**
> **0.10.0 修订：本文由 docs/webui-spa/ 上提至 docs 根（发布纪律属全项目）；
> 文中「面板版本」措辞 = 应用版本（WEBUI_VERSION，后端整体——0.9.7 起后端即主体）。**

## 1. 版本语义（面板版本 = WEBUI_VERSION）

- `0.6.x`：补丁——修 bug / 小加固；**攒批发布**，不随 commit 走
- `0.7.0`：功能里程碑——新增一个完整能力（如回测引擎、备份/恢复）
- `1.0.0`：正式版——60 天模拟盘验收通过 + 数据契约冻结评审通过
- 版本号 `stockdb-ai/config.py` 的 `WEBUI_VERSION`；tag `vX.Y.Z` 打在本仓库（原生模式
  下 tag 即发布物，无需镜像）

## 2. 发布节奏

- **修复攒批**：小修合入 main 后不发版；攒到一组有意义的修复（或出现"必须现在修"的阻断问题）才发一版
- **功能里程碑**：一个 Phase 完成 → 一版
- **主线验证**：本机引擎跑单测 + 回填/采集验证（`docs/development-guide.md` 配方）
- **镜像低频**：docker 镜像**仅成熟版本**手动触发（build-image.yml 仅 workflow_dispatch，
  日常合并只走 test.yml 轻量门禁，不构建镜像）

## 3. 发布检查单（发版前全部勾选）

- [ ] 前端 Vitest 全绿（含 10 页空载荷挂载防线）
- [ ] Python 全绿（test_ops / test_auction_* / mcp）
- [ ] 本机原生模式验证：引擎连通 + 必要回填/采集冒烟（离线单测不覆盖的路径）
- [ ] 功能清单回归表对应页勾选（docs/history/m2-regression-checklist.md）
- [ ] CHANGELOG.md 更新（本版一行说明）
- [ ] 合并 main → 打 tag `vX.Y.Z`；**若本版同时出镜像**：手动触发 build-image.yml → CI 四 job 绿
- [ ] 部署/验证记录更新 docs/deployments.md（镜像部署时）或本机验证记录（原生模式）

**分支纪律（2026-08-22 起）**：PR 合入 main 后功能分支即删（仓库已开启 GitHub
"自动删除 head 分支"；历史堆积的 53 个陈旧分支已于 0.10.0 后清理，仅保留
main 与确未合入的工作分支）。长期分支只有 main；tag 是发布历史的载体。

## 4. 发布物清单

| 发布物 | 位置 | 说明 |
|---|---|---|
| 版本号 | `stockdb-ai/config.py` 的 WEBUI_VERSION | 每版必更 |
| 变更说明 | CHANGELOG.md | 每版必更 |
| Git 标记 | tag `vX.Y.Z` | 每版必打（原生模式下即最终发布物） |
| 镜像 | `ghcr.io/awoeyiwuyua/stockdb-ai` | **可选**：仅成熟版本手动构建（+ 上游版本 tag） |
| 部署记录 | docs/deployments.md | 镜像部署时更新

## 5. 回滚约定

- 面板级（首选）：compose 环境变量 `WEBUI_UI=legacy` → 旧面板，零停机
- 镜像级：compose 改回上一个已知良好的镜像 digest/tag

## 6. 上游依赖治理 SOP（08-27 三雷实证固化，2026-08-29）

> 戒律前提（ROADMAP §4）：**不主动追上游新版本**——构建坏了才修；**修复必 pin
> SHA256**。上游是 free-stockdb（hello245m/free-stockdb），其 release 与镜像源
> 均会静默变更，本章是把"再遇到 = 半小时修复流程"制度化。

### 6.1 已知雷区（08-27 实证 + 后续补充）

| 雷 | 症状 | 首修 |
|---|---|---|
| workflow YAML 解析失败 | push 触发全 0s 失败 / dispatch 422 | 0.8.x（shell 引号内嵌真实换行）；0.10.17 再犯（job name 裸冒号）→ **改 workflow 必跑 pyyaml 严格解析** |
| release 资产被删/替换 | 下载 URL 404，CI 构建失败 | 0.3.1→0.3.2（上游删"测试版本0.3.1"仅存"测试0.3.2"） |
| 二进制打包丢可执行位 | sha256 OK 但 stockdb/数据更新 呈 -rw-rw-rw-，启动失败 | 0.3.2（tar 解包后补 chmod +x + test -x 断言） |
| 同步域名静默更换 | 镜像 302 → 新域，旧 sync_url 失效 | ah.123128.xyz → workbuddy.link（**不写适配层**，只改 /data/sync_url.txt 配置） |
| 数据源禁止旧客户端（09-07 实证） | 同步失败：manifest 403/502；旧 release 资产 404 | 0.3.2→0.3.5（协议版本门禁 X-Sync-UA: sync_client_X，**必须升级客户端二进制**，改 sync_url 无效；见 CHANGELOG 0.10.28） |
| 引擎 HTTP 协议升级（JSON→MsgPack） | webui/MCP 全链路 JSON 解析失配：health/coverage/code_stats 变 None | 0.10.29（free_stockdb.fetch 按 Content-Type 嗅探 → msgpack_lite 解包 → json 回序列化；引擎 0.3.5 起，无请求侧开关） |
| 镜像页日期标注失效 | health mirror:null | 0.10.13 起不依赖镜像页日期（本地探针自检） |

### 6.2 换域/换资产半小时流程

1. 确认上游 release 资产清单与 digest（浏览器看 release 页 assets）；
2. 改 `docker/Dockerfile`：`ARG VERSION` / `GH_TAG_ENCODED`（URL 编码 tag）/
   双平台 `SHA256`（与上游 assets digest 核对一致）；
3. 本机实测：下载包 `shasum -a 256` 吻合 + 解包看 `stockdb/` 内部布局未变；
4. CI 构建（build-image.yml 手动触发）——**五 job 全绿为准**（0.10.17 起含
   容器冒烟：起镜像断言 webui 就绪/版本一致/diag 全绿）；
5. NAS `docker compose pull && up -d`，按 6.3 复验清单过一遍；
6. 部署台账 + CHANGELOG 记录。

### 6.3 部署后实机复验清单（每次镜像部署必做）

> 0.10.14~0.10.16 教训：本地 388 测试全绿 ≠ 实机无恙（fixture/schema 同错、
> 存量数据 schema 落后都绕过测试）。部署后 5 分钟走完：

1. `/api/version` → 版本号与 config.py 一致；
2. `/api/diag` → 五项全绿；
3. `/api/warehouse/status` → watermark / last_result 正常；
4. MCP `warehouse_list_tables` + `run_sql` 任查一条 v_daily → 视图注册健康
   （schema/存量数据问题最先在这里炸）；
5. `/api/alerts` 当日新增 → 部署触发的降级告警会在这里出现。
