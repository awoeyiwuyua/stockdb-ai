# 列式仓库层设计（0.10.0，架构决策 D12）

> 用户三层存储架构（2026-08-22 拍板）的项目落位：
> ① Parquet 持久层（事实沉淀）→ ② DuckDB 查询仓储（视图/宏/关联）→ ③ 计算与应用（SQL 宏 + MCP 响应）。
> 本文件是**实施与验收的存档**：布局、不变量、SQL 面、宏口径、对账三板斧、降级语义。
> 决策依据与红线关系见 `docs/architecture.md` §4 D12（显式推翻 0.9.4「Parquet/DuckDB 不做」）。

## 1. 定位

**仓库 = 引擎数据的列式派生副本 + 个人研究可写库**。真相源声明（防双源漂移）：

- 引擎 = 当日权威（实时查询仍走引擎/MCP 既有 53 工具，契约不动）
- 仓库 = 派生副本，可信度由 **watermark** 承载（known_at = 已沉淀最新交易日，可能落后引擎当日）
- 沉淀数据全部来自现有引擎通道（快照/日K），**不新增行情获取源**——「不自建行情获取管线」红线继续成立

## 2. 磁盘布局（DATA_DIR 按存储类型分目录；0.10.0 治理批定稿；0.10.8 root 锁定）

> **0.10.8 root 锁定（用户 2026-08-23 拍板）**：所有数据存放于 `<repo>/data`，
> 仓库根 = `<repo>/data/warehouse`。`WAREHOUSE_DIR` 必须显式设置或跟随 DATA_DIR；
> Windows 上未设 DATA_DIR 时默认 `/data` 解析为 `C:\data`（本机漂移事故源头）——
> config 启动即警告，dev.sh 固化 `DATA_DIR=../data` + `WAREHOUSE_DIR=$DATA_DIR/warehouse`。

```
DATA_DIR/                                                 本机开发 = 仓库根 data/，生产 = /data 卷
├── warehouse/                                            ← 列式仓库（本设计）
│   ├── facts/daily/year=YYYY/market=sh/date=YYYYMMDD.parquet   日K：按日一文件，内按 code 排序
│   ├── facts/adjust/snapshot=YYYYMMDD.parquet                  复权因子：低频全量快照（版本化追加）
│   ├── facts/minute/code=xxxxxx/date=YYYYMMDD.parquet          分钟K：每码每日一文件（见 §2.2）
│   ├── facts/lhb/date=YYYYMMDD.parquet                         龙虎榜：按日事件文件（见 §2.2）
│   ├── warehouse.duckdb                                        视图/宏 + 用户表 + meta（C4 单点）
│   └── backups/warehouse-<stamp>-<uuid>.db                     warehouse.duckdb 在线备份（0.10.8，C5）
├── research/                                             研究成果 SQLite（research.db + backups/；旧根路径粘性兼容）
├── records/                                              日检 jsonl
└── alerts.json / sync.log                                ops 自描述单文件（留根）
```

**分层原则（0.10.8 确认）**：按「生命周期与角色」分目录，而非按技术格式——
facts/ = 不可变事实（Parquet，只增不改），warehouse.duckdb = 可变状态+派生（单文件，
内部 schema 分层：main 视图/宏 + research 用户表 + meta），backups/ = 恢复副本。
duckdb 保持单文件（不拆多库：视图/宏/元数据原子性与备份简单优先，与 mydb 单文件多表同原则）。

### 2.2 facts/<dataset> 分区策略矩阵（0.10.8 全量设计）

**核心规则：分区维度由数据的「访问形态」决定，不是由技术格式决定**——
横截面优先（全市场单日查询是主场景）→ 按日分区；单码时序优先（单标的连续查询
是主场景）→ 按码分区。每个 dataset 独立声明分区策略、watermark 键、写入通道。

| dataset | 访问形态 | 分区 | 文件粒度 | 行量级/日 | 更新语义 | watermark 键 | 写入通道 |
|---|---|---|---|---|---|---|---|
| daily | 横截面优先 | `year=YYYY/market=xx/date=YYYYMMDD` | 全市场 5000 行/文件 | 5K | 日追加，只增不改 | `watermark:daily` | 快照通道（现役） |
| adjust | 全量快照 | `snapshot=YYYYMMDD` | 全市场 1 文件 | 5K（低频） | 版本化追加，视图取最新 | `adjust:snapshot` | adjust_provider（延后） |
| minute | **单码时序优先** | `code=xxxxxx/date=YYYYMMDD` | 单码单日 ~50-480 行/文件 | 5000×~780 | 日追加，只增不改 | `watermark:minute` | 分钟K 通道（引擎 HTTP 可拉，延后） |
| lhb | 横截面事件 | `date=YYYYMMDD` | 单日 1 文件（内按 code 排序） | ~百行 | 日追加，只增不改 | `watermark:lhb` | 待引擎键空间（延后） |
| hk_daily | 横截面优先 | `year=YYYY/market=hk/date=YYYYMMDD` | 全市场 1 文件 | ~2.5K | 日追加 | `watermark:hk_daily` | mydb 迁移（延后） |
| fundamental | 单码快照 | `code=xxxxxx/date=YYYYMMDD` | 单码单日 1 文件 | 5K | 版本化追加 | `watermark:fundamental` | 待引擎键空间（延后） |
| tick | **单码时序优先（流式）** | `code=xxxxxx/date=YYYYMMDD/part=HHMM` | 单码单日单窗口 1 文件 | 2400 万（全市场） | 流式追加，窗口内只增 | `watermark:tick` | 待接入（延后，见 ROADMAP） |

**分钟K 粒度归一**：minute 内按 `period` 列区分 5m/15m/30m/60m（引擎原生字段
`code/date(14位)/open/close/high/low/volume/amount`，实测 8 列），不做多 dataset
拆分——同一访问形态（单码时序）共享同一分区策略，粒度只是列属性。

**tick 的 part 窗口**：tick 量级 2400 万行/日，按日一文件过大且写放大（流式追加
要反复重写文件）；`part=HHMM` 把窗口切到半小时级（沿 adjust 的「文件名段不生效、
真实列承载」教训，part 以真实列写入）。tick 是**唯一**允许日内多文件的 dataset。

**lhb 与 daily 同构**：横截面事件按日成文件（事件本身每日全市场可见），但量级
小一个数量级，不做 year/market 嵌套——单层 `date=` 分区足够，避免过度分区。

**空仓视图**：每个 dataset 独立 `v_<dataset>` 空视图（类型正确的空结果），
沉淀后 refresh 换成 read_parquet 视图——与 v_daily 同模式（W3 已验证）。

- 文件粒度「年/市场/日」而非「每标的一文件」：日K约 5000 行/日，按日成文件保持追加语义，
  又避免每年数千小文件；单标的时序查询靠文件内 code 排序 + 行组统计裁剪
- **DuckDB 只解析目录形式的 hive 分区**（文件名段的 `date=` 不生效）——adjust 的快照版本
  以真实列 `snapshot` 写入 parquet（W2 实测结论）
- 市场归类（layout.market_of）：sh（50/51/52/56/58/60/68 前缀）、sz（00/15/16/18/30）、
  bj（43/83/87/88/92）、hk 预留、other 兜底——与 app._classify_code 同域

## 3. 不变量（W2 验收通过）

| 不变量 | 机制 | 验收 |
|---|---|---|
| facts 只增不改 | 分区文件已存在即跳过；无改写历史路径 | 同日双跑：第二次 skipped、行数不变 |
| 原子可见 | 临时文件 → COPY → os.replace；失败清理 .tmp | 模拟 rename 中断：无半文件、无残留 |
| 护栏 | 数值列 None/NaN/Inf 拒写计数（沿 research_store 教训） | NaN 行 dropped_nonfinite=1 |
| watermark 只前进 | catalog.set_watermark 字典序比较 | 回退/原地重写返回 False |
| 文件独立可读 | 分区可用独立 duckdb 连接直读 | DESCRIBE + SELECT 断言（沿 0.9.6 备份断言模式） |

元数据（C4）：watermark/快照指针唯一存于 warehouse.duckdb meta 表（key/value），无 json 旁路。

## 4. 查询与计算（engine.py，W3 验收通过）

连接策略：单连接 + threading.Lock 全程串行（沿 mydb `_rd_lock` 模式）；DuckDB 同进程按路径
缓存实例，sink 短连接与 engine 常驻连接共享实例。

### 视图

| 视图 | 定义 |
|---|---|
| v_daily | `read_parquet(facts/daily/*/*/date=*.parquet, hive_partitioning=true)`（附 year/market 列）；空仓期为类型正确的空视图 |
| v_adjust | 全部快照按 (code,date) 取 snapshot 最新（去重） |
| v_daily_fq | `v_daily ASOF LEFT JOIN v_adjust`（code 相等、date ≥ 因子日取最近）→ adj_factor + open_fq/high_fq/low_fq/close_fq；无因子行 fq 列为 NULL（原价） |
| v_codes | codes 表（沉淀任务每日全量刷新，"当前状态"非事实） |

### 指标宏（表宏，窗口按 code 分区、date 排序；窗口不满 n 为 NULL——对齐 pandas rolling 语义）

- `ta_ma(n)`：n 日简单均线
- `ta_rsi(n)`：**简单版 RSI**（滚动均值比，非 Wilder 平滑）——口径差异显式声明，
  与 pybao zhibiao 对账时以实际容差结论签字
- `ta_macd(fast, slow, sig)`：递归 CTE 双 EMA + 信号线（EMA 以首日收盘为种子 → 首日 macd=0）

### run_sql 三护栏（用户拍板「读写最大权限」）

1. **单语句**：一次一条（信封结果形态唯一）
2. **facts 只读**：语句文本含 `facts/` 即拒（COPY TO 逃逸通道封死；视图读不受影响）
3. **行数上限/超时**：超 cap 截断（信封 truncated）；超时经 watchdog 线程 `interrupt()`（尽力而为）

用户自建表建议放 `research` schema（warehouse.duckdb 持久化）。数值语义：字段按引擎原样镜像，
无单位换算。

## 5. 沉淀任务（services/warehouse_tasks.py，W4 验收通过）

- 调度：第四条线程，交易日 `WAREHOUSE_SEDIMENT_TIME`（默认 16:40，config 可覆盖）触发；
  就绪门 `data_latest(force=True) >= 今日`（与打板收口同判定），未就绪 10 分钟重试至 20:00 告警收口
- 拉取：全市场快照 `query_point_snapshot(limit=0)` 一次往返（SDK 批量快路径），TRADED 行 = 当日日K
- 写入：sink 分区 + codes 刷新 + watermark 推进；复权快照周一（或首次）全量——依赖
  adjust_provider 注入（引擎键空间无批量端点，SDK 通道接入前为 None → 跳过，延后项）
- 纪律：try/except 降级 + log + notify_alert + records.append（trace_id 贯穿）
- 手动通道：`POST /api/warehouse/run {"days":1-5}`（小范围测试拉取，幂等补缺口）；
  `GET /api/warehouse/status`（watermark/守卫/任务状态）
- 层纪律（C3）：services 不 import storage.warehouse——sink/reconcile/availability 经组合根注入

### 5.1 warehouse.duckdb 每日备份（0.10.8，C5 落地）

- 时机：沉淀任务成功且有目标日后一次（`backup_duckdb(root)` 注入点，日级守卫防重复）；
  失败静默（log 不阻塞沉淀），同 research_store 的"备份失败不影响日检"纪律
- 机制：独立连接 `COPY FROM DATABASE`（DuckDB 1.5 语法；ATTACH 源/目标后全库镜像，
  含 meta/codes/research 用户表/视图宏定义）——不占 engine 业务锁（沿 0.9.12 教训）
- 保留：最近 `BACKUP_KEEP=14` 份（backups/warehouse-<stamp>-<uuid>.db，秒级+uuid 防同名）
- 边界：facts/ 是 Parquet 事实区（可从引擎重拉），**不纳入**本备份；恢复时先还原
  duckdb，再按需回填 facts。备份文件独立可打开（沿"备份独立可读"断言模式）

## 6. 对账三板斧（storage/warehouse/reconcile.py）

| 板斧 | 口径 | 容限 |
|---|---|---|
| 行数 | 分区合计 + dropped = 快照 TRADED 数 | 精确相等 |
| 字段级（同源回读） | 抽样点 vs Parquet 逐字段（7 数值列） | 相对 1e-6 |
| 异源 | 抽样点开盘/昨收 vs 腾讯/东财（quote_sources 通道） | 相对 0.5% |

issues 非空 → 告警 + 日检记录（records.jsonl）。**发版前异源签字**：抽样 ≥20 只 ×
MA20/MACD/RSI14 与 pybao get_indicators 逐值对账，结论记 `docs/acceptance/`（待实数据执行）。

## 7. MCP 工具面（warehouse 组 3 个，C2 收敛：不加单指标工具）

| 工具 | 契约 | 说明 |
|---|---|---|
| warehouse_run_sql | warehouse-sql-v1 | 单条 SQL 读写全开（三护栏）；known_at = watermark |
| warehouse_list_tables | warehouse-meta-v1 | 表/视图/宏清单 |
| warehouse_status | warehouse-status-v1 | watermark/天数/快照/duckdb 版本 |

错误映射：GuardrailError/参数类 duckdb 异常 → INVALID_ARGUMENT；WarehouseUnavailable →
DEPENDENCY_UNAVAILABLE（hint：uv sync / musllinux 无 wheel 属预期）；超时 → INTERNAL_ERROR。
工具数 53 → 56（6 组 → 7 组）。

## 8. 依赖与降级

- **duckdb = 首个第三方依赖**（uv 锁定 1.5.5；不用 pyarrow——DuckDB 原生 Parquet 读写）
- CI：setup-uv + `uv sync --frozen`（版本不漂移）；Docker：amd64 `pip install duckdb==1.5.5`
  （与 uv.lock 手动对齐，发布纪律），arm64 alpine 无 musllinux wheel → 不装 → 降级
- 降级语义：availability() 探针（duckdb 缺失 / WAREHOUSE_ENABLED=0）→ 仓库工具
  DEPENDENCY_UNAVAILABLE，53 既有工具与 webui 完全不受影响（回滚演练 = `WAREHOUSE_ENABLED=0`）

## 9. 延后项（ROADMAP 收敛清单登记）

历史回填（用户 2026-08-22 延后；分区按年，将来补历史只是加目录）；分钟K/基本面/龙虎榜数据集
（dataset 维度已预留）；hk日k 迁仓库（mydb 届时只剩自定义表）；adjust_provider SDK 通道。

## 10. 发版门（0.10.0 打版前全部通过）

1. 试运行：连续 ≥3 交易日自动沉淀 + 对账三板斧全绿（记录 docs/acceptance/）
2. 性能下限：单标的多日查询 < 200ms / 全市场单日横截面 < 500ms / 十万行 run_sql < 2s
3. 回滚演练：WAREHOUSE_ENABLED=0 全链路无感知、53 工具回归绿；warehouse 目录损坏 → 仅仓库工具降级
4. 测试总量 ≥ 300（实际 308）+ 层边界测试含 C3 规则
5. 文档四件套（本文件 / ROADMAP / CHANGELOG / development-guide）
