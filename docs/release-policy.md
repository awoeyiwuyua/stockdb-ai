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
  下 tag 即发布物，无需镜像）——**tag 与版本号一一对应，是回滚的唯一依据**
  （见 §3.1 判据；2026-10-20 已补齐历史上 17 个漏打的版本）

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
- [ ] **打 tag `vX.Y.Z`（见下方细则）**；**若本版同时出镜像**：手动触发 build-image.yml → CI 四 job 绿
- [ ] 部署/验证记录更新 docs/deployments.md（镜像部署时）或本机验证记录（原生模式）

### 3.1 打 tag 的细则（2026-10-20 补——连续 17 版漏打的直接对策）

> **为什么补这段**：09-19~10-17 连续 **9 版**未打 tag（另加两段更早的缺口，
> 共 **17 个版本**，最远可回溯到 0.5.0）。根因不是"忘了"——是本检查单把
> 「打 tag」写成一句 checkbox，与「合并 PR」分属**两个工具**
> （浏览器 UI vs 本地终端），每次发版都要跨工具切换，跨 17 次全漏。
> 补这段是为了把判据写死，让打 tag 不再依赖记忆。

**tag 目标 = 该版本 `WEBUI_VERSION` 首次出现的 commit**（版本号单源 =
`stockdb-ai/config.py`），**不是 PR 合并 commit**——一个版本可能跨多个 PR，
也可能多个版本共用一个 commit。

| 情形 | tag 指向 |
|---|---|
| 常规（0.10.45 / 0.12.0 等） | 该版本 `WEBUI_VERSION` 首次出现的那个 commit |
| 一版跨多个 PR（0.12.3） | 版本号首次出现的 commit（非最后一个 PR 的 merge commit） |
| 多版共用一个 commit（0.11.1 / 0.11.2） | 同一个 commit，两个 tag 各带自己的版本说明 |

**操作**：

```bash
# 1) 找该版本版本号首次出现的 commit
git log --format=%H -- stockdb-ai/config.py          # 逐个看
git show <sha>:stockdb-ai/config.py | grep WEBUI_VERSION   # 确认该 commit 的版本值

# 2) 打 annotated tag（带说明：版本要点 + PR 号 + 是否事后补打）
git tag -a vX.Y.Z -F <message-file> <sha>

# 3) 推送
git push origin vX.Y.Z
```

**核对（必做）**——仓库内已有审计脚本，一条命令验证「无版本漏打」：

```bash
python .github/scripts/audit_tag_coverage.py
# 输出 "OK: N 个版本全部有对应 tag（缺 0）" = 通过
# 有缺口时列出 MISSING 版本 + 处置步骤，退出码 1
```

**发版后自检**（可直接放进合并后的收尾动作）：

```bash
python .github/scripts/audit_tag_coverage.py --quiet && echo "tag 覆盖完整"
```

> **注**：CI 侧目前**没有**自动 tag / 自动审计 job——按 ROADMAP §0 的判断，
> 「防复发」的基建排在研究闭环之后（见 ROADMAP §0-5 候选项）。
> 在那之前，**这一条 checklist 是唯一的防线**，所以它必须被执行，不能只是"写着"。

**分支纪律（2026-08-22 起）**：PR 合入 main 后功能分支即删（仓库已开启 GitHub
"自动删除 head 分支"；历史堆积的 53 个陈旧分支已于 0.10.0 后清理，仅保留
main 与确未合入的工作分支）。长期分支只有 main；tag 是发布历史的载体。

## 4. 发布物清单

| 发布物 | 位置 | 说明 |
|---|---|---|
| 版本号 | `stockdb-ai/config.py` 的 WEBUI_VERSION | 每版必更 |
| 变更说明 | CHANGELOG.md | 每版必更 |
| Git 标记 | tag `vX.Y.Z` | 每版必打（原生模式下即最终发布物）；**判据与操作见 §3.1** |
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

> **完整条目（症状/判据/处置/铁律）见 [`docs/ops/runbook.md`](ops/runbook.md) §2。**
> 本表只留**雷区索引**——速查用；排查时按 runbook 的症状索引跳过去。

| 雷 | 症状 | 首修 | runbook |
|---|---|---|---|
| workflow YAML 解析失败 | push 触发全 0s 失败 / dispatch 422 | 0.8.x（shell 引号内嵌真实换行）；0.10.17 再犯（job name 裸冒号）→ **改 workflow 必跑 pyyaml 严格解析** | — |
| release 资产被删/替换 | 下载 URL 404，CI 构建失败 | 0.3.1→0.3.2（上游删"测试版本0.3.1"仅存"测试0.3.2"）；**0.3.5 同 tag 重传**（09-08，URL 不 404 但 tar 哈希变 → 原 pin 失效，`sha256sum -c` 构建失败；见 CHANGELOG 0.10.31） | §2.5 |
| 二进制打包丢可执行位 | sha256 OK 但 stockdb/数据更新 呈 -rw-rw-rw-，启动失败 | 0.3.2（tar 解包后补 chmod +x + test -x 断言） | §3.6 |
| 同步域名静默更换 | 镜像 302 → 新域，旧 sync_url 失效 | ah.123128.xyz → workbuddy.link（**不写适配层**，只改 /data/sync_url.txt 配置） | §2.7 |
| 数据源禁止旧客户端（09-07 实证） | 同步失败：manifest 403/502；旧 release 资产 404 | 0.3.2→0.3.5（协议版本门禁 X-Sync-UA: sync_client_X，**必须升级客户端二进制**，改 sync_url 无效；见 CHANGELOG 0.10.28） | §2.7 |
| 引擎 HTTP 协议升级（JSON→MsgPack） | webui/MCP 全链路 JSON 解析失配：health/coverage/code_stats 变 None | 0.10.29（free_stockdb.fetch 按 Content-Type 嗅探 → msgpack_lite 解包 → json 回序列化；引擎 0.3.5 起，无请求侧开关） | §2.1 |
| 引擎 SDK 区间语义变更（09-08 重传二进制） | 全市场快照退化成 ~50~100 只 → 仓库沉淀零星行、打板清单漏检（**单只 `get_kline` 正常，掩盖链路**） | 0.10.33（批量 SDK 改用同日区间 `start==end`，旧语义顺延一日作回落；换引擎二进制后回归全市场快照链路，非单只调用） | §2.2 |
| 镜像页日期标注失效 | health mirror:null | 0.10.13 起不依赖镜像页日期（本地探针自检） | — |
| **镜像更新入口被禁用（10-08 上游公告）** | `a.123128.xyz` **302 → `dns.novapi.cc`**；直接用它更新**会把源站拉满** → 上游改回 `http://ad.123128.xyz` | 0.12.14（`MIRROR_PAGE_URL` 默认改 `http://ad.123128.xyz`，**不再用 a.**；同步源 source1 改 `http://ad.123128.xyz always`） | §2.8 |
| 同名 tag 重传资产无法发现 | `tag_name`/`published_at` 不变，旧探针只看 tag → 只能等同步失败被动发现（09-07/09-08 两次） | 0.10.40（`/releases` 列表 + 资产指纹 `asset_fingerprint`；首见只建基线，变化即告警 `asset_changed`） | §2.5（遗留） |
| 上游发新版但**无人被告知** | 探测判定把「面板版本」当引擎版本比（`(0,3,6)>(0,10,36)=False`）→ `stale` 恒 false；探针失败静默留空 | 0.10.37（A/B/D：`IMAGE_TAG` 注入 + 引擎版本同类比较 + 看门狗告警与显式降级）。**仍待做（C）**：`/releases/latest` 看不到 prerelease；同名 tag 重传的资产指纹未比对——0.3.5 两次同 tag 重传只能靠同步失败被动发现 | §2.5（遗留） |
| **pybao 惰性 `QueryResult` 被当普通容器用** | `.do()` 才取原生数据；`.keys()/.all()/.len()` 返回错误文案 `'Missing required parameters'`，`iter()` 逐字符吐它 → 键列表变 `[' ', 'M', 'a'…]`、全表读全空、`for v in rd.vals(...)` 恒 0 行（**数据写进去了却读不出来**） | 0.10.41（三处收口：`mydb_store._rd_to_py` 补 `.do()` 分支、`pybao_tools.rd_keys` 经 `_to_py` 归一、`hk_klines` 走 `_rd_to_py`） | §2.3 |
| mydb 键形态（段式 vs 拼接） | `get(table, "00700:20240828")` 与带表名整串在 0.3.5 上**恒返回 `[]`**；命中的是**三段** `get(table, code, date)`；单段键（`打板指标`/`自定义`）才用两段 | 0.10.41（候选序列 `_rd_get_any`/`mydb_key_candidates` 逐个试，首个非空即返回；剥表名前缀只在首段确属已登记前缀时做） | §2.4 |

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
6. **`/api/health` → `latest` + `lag_days`**（**引擎行情的真实日期**）。
   ⚠️ **不要用第 3 项的 `watermark_daily` 代替它**——那是**仓库层**水位线，
   两者口径不同。**2026-10-20 教训**：0.12.x 的台账把 `watermark_daily=20261019`
   当成「行情数据最新日期」上报，**掩盖了引擎行情已陈旧 2 个月**的事实。
   详见 `docs/ops/engine-data-stale.md`。
7. **MCP 行情工具抽查** → `get_kline` 取最近 3 个交易日，确认引擎真能返回
   （`/api/health.latest` 也可能与引擎实际不符——它走前缀通配探针）。

### 6.5 上游源整体不可达（2026-10-20 补，§6.1 未覆盖的分支）

> §6.1 的 12 条雷区覆盖了「换域/删资产/禁旧客户端/协议升级」，但**没覆盖
> 「源整体消失」**。**2026-10-20 当日实测：15:50 同步报 `auth failed / 状态:网络不通`
> → 16:37 同一源 `状态:有效` → 18:16 同步 100% 完成、引擎追平到 20261020。**
> 所以「源整体不可达」是**间歇性**的，**判定必须跨时段复验**（见第 5 条）。

**这是常规操作不是意外**——上游换域/停服没有通知。处置：

1. **先确认是源的问题还是同步器的问题**——看 `/data/sync.log`：
   同步器报「N 个文件对不齐 → 本轮放弃」说明它**行为正确**（不强行覆盖已有数据），
   源恢复后会自动补齐。**不要去改同步器**。
2. **从容器内逐个探测候选源**（宿主机网络与容器不同，探测结果可能不一致）：
   ```bash
   for u in https://ah.123128.xyz https://ad.123128.xyz; do
     curl -sL -o /tmp/m.txt -w "$u -> http=%{http_code} size=%{size_download}\n" \
          --max-time 20 "$u/manifest.txt"
     echo "   manifest lines=$(wc -l < /tmp/m.txt)"
   done
   ```
   ⚠️ **根路径 404 不代表源不可用**（镜像是按需路径取文件，根路径本就 404），
   **http/https 表现也可能不一致**（上游在抽风）。**单次探测不作数。**
3. **找到新源后**：只改 `/data/sync_url.txt`（**不写适配层**，同 §6.2 纪律），
   然后 `POST /api/sync` 触发一次同步。
4. **源一时找不到时的兜底**：ROADMAP §0-2 的「A 股日K 收盘兜底源」
   （腾讯 `qt.gtimg.cn`，**独立于上游引擎**）正是为此场景规划；
   仓库层已能独立提供 SQL 面（本次实测 `v_daily` 116 万行、到 2026-10-19）。
5. **⚠️ 判定「源整体消失」必须跨时段复验**（2026-10-20 教训）：
   当日**只测了一次**就写下「所有源不可达、无任何可用源」——**结论过强**，
   几小时后同一源就恢复了。**判据应换成「同步有没有成功」**
   （`grep -aE 'Success: 同步已|数据源失败' /data/sync.log`），
   至少隔 30 分钟~1 小时重试若干次再定性。
6. **上游 10-08 公告的硬规则**：**禁止直接用 `dns.novapi.cc` 更新**（会把源站拉满）；
   官方更新入口是 **`http://ad.123128.xyz`**。而 `a.123128.xyz` 现在 **302 → novapi**，
   **配置里不要再用 a.**（详见 runbook §2.8）。
7. **注意数据层会分叉**：引擎陈旧而仓库层新鲜时，**MCP 行情工具与仓库 SQL 面的
   数据截止日不同**——对外说明时要讲清楚，别混为一谈。

### 6.4 fork 上游同步（消除「N commits behind」，零内容变更）

> 本仓库是 `hello245m/free-stockdb` 的 GitHub fork（但为 docker/webui 封装与
> 私有研究，**不追上游代码**）。GitHub 分支页会拿我们的 `main` 比上游 `main`，
> 上游有新提交即显示「N commits behind」。消除方式（2026-09-10 首次执行）：

```bash
git remote add upstream https://github.com/hello245m/free-stockdb.git  # 一次性
git remote set-url --push upstream no_push_allowed        # 结构上禁止误推上游
git fetch upstream main
git merge -s ours upstream/main --no-ff -m "chore(fork): 合并上游 main（-s ours，零内容变更）"
git push origin main   # 本仓库有规则集要求走 PR → 推分支 + PR 合并
```

- **`-s ours`**：记录上游为祖先（behind 归零），整棵树保持本仓库版本，**零内容
  变更**（`git diff HEAD^1 HEAD` 应为空）。上游 5 提交仅改 README，本仓库 README
  已重写为自己项目说明，常规合并必冲突且**不应采纳上游内容**——故用 ours
- **只动本仓库**，上游不受影响；`upstream` 的 pushurl 置为 `no_push_allowed`
  双保险
- 本仓库 main 有规则集「Changes must be made through a pull request」——`-s ours`
  的合并需推分支走 PR（GitHub branch-protection API 对规则集不显示，以推送报错
  为准）
- **复发即重跑**：上游后续再有提交，behind 会重新出现，重复本节操作
