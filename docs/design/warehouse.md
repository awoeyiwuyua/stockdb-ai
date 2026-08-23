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
│   ├── facts/tick/code=xxxxxx/date=YYYYMMDD/part=HHMM/data.parquet   逐笔（唯一按码，见 §2.2）
│   ├── facts/minute/period=5m/year=YYYY/market=xx/date=YYYYMMDD.parquet   分钟K（时间分层）
│   ├── facts/hour/year=YYYY/market=xx/date=YYYYMMDD.parquet    小时K（60m，时间分层）
│   ├── facts/daily/year=YYYY/market=xx/date=YYYYMMDD.parquet   日K：按日一文件，内按 code 排序
│   ├── facts/week|month|year/year=YYYY/market=xx/date=YYYYMMDD.parquet   聚合K（daily 聚合物化）
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

### 2.2 facts/<dataset> 粒度阶梯（0.10.10 定稿：tick→minute→hour→daily→week→month→year）

**核心规则一（用户拍板）：数据集按「粒度」命名，七级阶梯就是读写维度**——
tick / minute / hour / daily / week / month / year，每级独立 dataset + 独立
watermark 键。5m/15m/30m/60m 归 minute 家族（period 目录段），hour=60m。

**核心规则二（用户拍板）：只有 tick 按 code 分层，其余全部时间分层**——
tick 盘中流式到达（逐码追加写同一文件），按日文件会写放大，故按码+日内 part 窗口；
时间序列（minute/hour/daily/week/month/year）是批量拉取（按周期全市场一次写齐），
二级目录统一从 `year=YYYY/market=xx` 切入——与 daily 完全同构，布局/sink/查询通用。

| dataset | 粒度 | 分区 | 行量级/日 | 更新语义 | watermark 键 | 写入通道 |
|---|---|---|---|---|---|---|
| tick | 逐笔 | `code=xxxxxx/date=YYYYMMDD/part=HHMM` | 2400 万 | 流式追加，窗口内只增 | `watermark:tick` | 待接入（延后） |
| minute | 1m/5m/15m/30m | `period=xx/year=YYYY/market=xx/date=YYYYMMDD` | 5000×~300 | 日追加，只增不改 | `watermark:minute` | 分钟K 通道（引擎 HTTP 可拉，延后） |
| hour | 60m | `year=YYYY/market=xx/date=YYYYMMDD` | 5000×4 | 日追加 | `watermark:hour` | 分钟K 通道（延后） |
| daily | 日K | `year=YYYY/market=xx/date=YYYYMMDD` | 5K×4市场 | 日追加，只增不改 | `watermark:daily` | 快照通道（现役） |
| week | 周K | `year=YYYY/market=xx/date=YYYYMMDD`（周结束日） | 5K×4/周 | 周追加 | `watermark:week` | daily 聚合物化（延后） |
| month | 月K | `year=YYYY/market=xx/date=YYYYMMDD`（月末） | 5K×4/月 | 月追加 | `watermark:month` | daily 聚合物化（延后） |
| year | 年K | `year=YYYY/market=xx/date=YYYYMMDD`（年末） | 5K×4/年 | 年追加 | `watermark:year` | daily 聚合物化（延后） |

- **minute 家族**：1m/5m/15m/30m 共享 `minute` dataset，period 为目录段（hive 解析出
  period 列）——文件内单一周期避免混装过大；hour=60m 独立成 dataset（金融惯例 H1）
- **week/month/year 由 daily 本地级联聚合物化**：沉淀 daily 后当场聚合落盘
  （一次计算多次复用，不依赖 pybao SDK 的 1w/1M 通道），同骨架同列（含物化复权列）。
  **week 已实现（0.10.11）**，聚合语义见 §2.4；month/year 同模式待加。
- **hk 并入 daily**：market=hk 分区（layout.market_of 已支持 hk 前缀/5 位代码），
  由 mydb 迁入时直接写 `market=hk` 分区，不独立成 dataset
- **事件/快照类（lhb/fundamental 等）暂不占位**：非 K 线尺度，接入时再定
- **空仓视图**：每个 dataset 独立 `v_<dataset>` 空视图（类型正确的空结果），
  沉淀后 refresh 换成 read_parquet 视图——与 v_daily 同模式（W3 已验证）

### 2.4 周K/月K 聚合物化（0.10.11 week 落地；0.10.12 month 落地 + 当前周期派生）

- **触发**：沉淀任务完成后自动——对每个覆盖到的自然周/月，**周期完整才聚合**
  （该周期内每个交易日均已有 daily 分区文件或 empty 标记，0.10.12 修复：旧实现
  只比较 watermark ≥ 周期最后交易日，数据缺口时 watermark 仍推进会误判完整——
  残缺周期被聚合后幂等无法重写）；节假日周期以实际最后交易日判定
- **聚合源**：该周期 daily 分区 read_parquet glob（不重复拉引擎），market 经 hive 解析
- **聚合语义（周期K 口径，week/month 同一 SQL 骨架 `_kline_aggregate_sql`）**：

  | 列 | 规则 |
  |---|---|
  | open / close | 周期内首/末日（arg_min/arg_max by date） |
  | high / low | 周期内 max / min |
  | volume / amount / turnover | 周期内求和 |
  | pct_chg | (周期末 close − 周期首日 pre_close) / 首日 pre_close（周期涨跌幅，重算） |
  | amplitude | (high − low) / 首日 pre_close（周期振幅，重算） |
  | vol_ratio | NULL（周期级无定义） |
  | is_st / name / pb / pe_ttm / 市值类 | 周期末日（arg_max by date） |
  | adj_factor / *_fq | 物化列同规则聚合（open_fq=首日、close_fq=末日、high/low_fq=max/min） |

- **幂等**：周期分区已存在跳过（facts 只增不改）；watermark:week / watermark:month 只前进
- **other 市场孤码不沉淀**；周期分区与 daily 同构 26 列
- **当前未走完周期 = 派生视图（股票软件"进行中的周/月K"语义）**：
  `v_week_current` / `v_month_current` 查询时从 v_daily 实时聚合
  （周期边界 = 本周一/本月1日 → current_date），历史周期固定落盘、当前周期滚动可见

### 2.3 复权：沉淀时物化，查询零计算（0.10.10 重构，取代查询时 ASOF）

**旧设计（已废弃）**：`adjust` 独立 dataset + `v_daily_fq` 查询时 `ASOF LEFT JOIN`
现算因子×价格——每次查询重复计算，因子事件变化后历史价格漂移。

**新设计（用户拍板：聚合层面一次计算、多次复用）**：

```
引擎复权事件（按码：div/give/trans/mult/cum 事件序列，追加只增）
    ↓ 沉淀任务（周度/首刷）展开
factor 序列：factor(code, date) = 截至 date 的最新 cum（每日累计因子）
    ↓ sink.write_daily 同步物化（一次计算）
daily 分区新增伴随列：adj_factor + open_fq/high_fq/low_fq/close_fq
    ↓ 查询层
v_daily_fq = v_daily 直接读物化列——零 JOIN、零计算、多查询复用
```

- **物化在 daily 分区内**：因子是日K的伴随属性，同文件同生命周期，原子写/幂等/
  只增不改全部继承；不再是独立 dataset
- **只增不改不破坏**：因子事件只追加（新分红追加新事件，历史 cum 不变）→ 物化列
  历史值永不变；reconcile 增加"复权列回读"校验
- **事件源是内存输入，不占 facts**：引擎事件经 adjust_provider 注入 →
  `_build_factor_map` 展开为 {code: cum} 缓存（周度/首刷刷新），物化后即弃；
  审计留档延后（如需可写 warehouse.duckdb 内表，低频量小）
- **依赖顺序**：daily 物化依赖 factor_map 就绪（周一/首刷先行）；未就绪时
  物化列 NULL（原价），事件到位后下次沉淀补齐该日——补写窗口内旧分区重写
  （见 §3 不变量修订）
- **week/month/year 聚合物化时同带复权列**：聚合产物 = 一次计算、带全信息

- 文件粒度「年/市场/日」而非「每标的一文件」：日K约 5000 行/日，按日成文件保持追加语义，
  又避免每年数千小文件；单标的时序查询靠文件内 code 排序 + 行组统计裁剪
- 市场归类（layout.market_of）：sh（50/51/52/56/58/60/68 前缀）、sz（00/15/16/18/30）、
  bj（43/83/87/88/92）、hk 并作 daily 的 market 分区、other 兜底——与 app._classify_code 同域

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
| v_daily_fq | `= v_daily`（复权列 adj_factor/open_fq/high_fq/low_fq/close_fq 沉淀时物化，查询零计算；事件未就绪为 NULL 原价） |
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
