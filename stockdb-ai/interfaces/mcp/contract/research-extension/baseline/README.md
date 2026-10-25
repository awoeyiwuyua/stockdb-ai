# StockDB 公开行情服务合同 v3

> 合同标识：`stockdb-market-v3`；合同修订：`3.0.0`。
> 状态：**消费方要求已固定，待服务端实现与联合验收；不是当前已部署接口。**
> 用途：交给 StockDB 服务维护 Agent 实施，并作为本项目后续客户端适配的同一依据。
> 本文是 `docs/market-data-source-contracts.md` 的下位接口合同，不改变项目架构路线图。

## 0. 使用方式与规范等级

先读本文，再读 [HANDOFF.md](HANDOFF.md)。机器合同、示例及验收清单在同目录：

| 文件 | 用途 |
|---|---|
| `contract.schema.json` | 首批请求、日K响应及错误信封的 JSON Schema，Draft 2020-12 |
| `examples.json` | 合成请求、完整/部分/失败响应；所有行情数值均为测试值 |
| `acceptance-cases.json` | 双方共用的验收编号、请求和明确断言 |
| `validate_contract.py` | 离线检查 schema、示例与关键跨字段规则，也可校验真实响应文件 |

“必须/MUST”是验收条件；“可以/MAY”是实现自由；“暂不支持”必须被机器可读地声明。
实现方式、数据库、缓存和线程模型由服务维护者决定，外部语义按本文固定。
本文、schema、验收样本出现不一致时，不能挑选宽松版本通过验收：先修订交付包，再两端按同一修订实现。
本合同不会自动修改本项目的生产代码、数据集晋升状态或来源优先级。

## 1. 目标、职责和数据边界

StockDB 提供公开、可重复获取的行情：证券标识、交易日历、价格、OHLCV、成交额，
以及按能力声明提供的指数/板块、分钟、复权和历史身份元数据。

项目发送的内容只有公开证券标识、日期/时间、周期、复权、字段和公开数据快照标识。
数量、成本、券商成交、账户、Attention、私有策略、研究假设和分析结果均不发送或存入 StockDB。
示例证券只是公开协议测试集合，不表示任何账户关系；请求和日志不附带“持仓”“账户”等标签。

两端分工：

| StockDB 服务端必须负责 | 本项目必须负责 |
|---|---|
| 按合同解析请求，准确定位证券，返回真实行情与来源 | 在本地选择查询范围，不向 NAS 发送选择原因或私有事实 |
| 说明支持范围、限额、错误、缺行、截断、时间、单位和版本 | 校验用途要求、窗口完整性、交叉核验及备用源是否合格 |
| 提供可识别、可在保留期内重读的公开数据快照 | 将公开行情通过现有 CLI 发布到 ODS/DWD/DWS/ADS |
| 对上游失败如实返回，不造零值、停牌理由或历史发布时间 | 正式消费者经 `data_core` 读取，在本地关联私有事实 |

财务、公告、新闻、研报、宏观和 NAS 策略指标不属于本合同的首批范围。
`warehouse_run_sql`、NAS 物理 Parquet 或第二套 HTTP 直连不是正式采集替代入口。
StockDB 是已验收行情数据集的优先来源，项目仍保留有来源、有原因、有质量检查的备用路径。

## 2. 已核验基线与本次必须修复的行为

核验时点：2026-10-23 21:16—21:17，原服务为 `stockdb-native 0.12.14`，
HTTP MCP 地址为 `http://100.66.1.5:8081/mcp`。地址仅为当前部署定位信息，不写死在业务语义里。

已观察到：

1. `get_kline(code=...)` 对五个公开证券分别返回所请求四日的日K。
2. 相同范围 `codes=[五个代码]` 在 `limit=0` 和 `limit=10` 下均为空，
   却同时报告 `errors=[]`、`is_partial=false`、`truncated=false`。
3. `codes=["002600"]` 返回有行情，但 `data` 是列表；项目批量客户端要求按代码组织的对象。
4. 服务工具 schema、说明和实际数据范围存在不一致，例如证券列表实际包含 ETF。
5. 行情慢调用后的历史断连原因未确认；后续纯行情探测已恢复。不能把猜测写成根因。

可复现的旧版原请求保存在 `acceptance-cases.json` 的 `legacy_reproduction`，服务端必须保留对应回归。
完整历史证据在项目治理归档中；交给外部 Agent 的本目录自包含，不要求访问项目业务数据库。
上游公开 SDK 与 NAS 包装层是否同一实现尚未确认，维护者须先查源码、部署镜像和调用链。

## 3. 分批能力和放行范围

服务只能声明实际实现并通过测试的能力。工具存在、健康正常、某个样本正确不能代表所有能力可用。

| 能力配置 | 要求 | 依赖工具 | 放行含义 |
|---|---|---|---|
| `core_daily_v1` | **首批必须** | `get_data_status`、`get_stock_list`、`get_trading_days`、`get_kline` | 沪深股票和 ETF，未复权日K、单票/批量、同批收盘、来源、缺行、重读和故障合同 |
| `fullmarket_daily_v1` | 独立验收 | 上述工具，沪深北 A 股身份和目标范围覆盖 | 冻结全集后逐证券×交易日可解释；不能只按返回行数或当前名称判断 |
| `index_board_daily_v1` | 独立验收 | `get_kline`，必要时复用 `get_board_members/bk_get` | 指数与各提供者板块独立序列；映射不等于行情支持 |
| `minute_v1` | 独立验收 | `get_kline` | 逐周期声明；1m、5m等分别验收 |
| `adjusted_history_v1` | 独立验收 | `get_adjust_factors`、`get_kline` | 因子、锚点、算法、行情版本对应且可复算 |
| `historical_identity_v1` | 独立验收 | `get_stock_list`，现有身份/状态工具可补充 | 历史上市/退市/迁码/状态有效期有证据；不要求首批补齐全历史 ST |

首批最低资产能力是 `XSHG/XSHE × equity/etf × exchange × 1d × none`。
每种能力必须声明实际历史起止和已知例外；这不承诺所有证券拥有相同长度历史。
项目按用途检查所需窗口，例如某证券只能提供短历史时，不得作为长窗口研究的已通过数据。
北交所或某项扩展未验收时返回不支持，不能使用代码前缀猜身份后冒充完整范围。

## 4. MCP 传输与兼容

### 4.1 复用入口和工具名

沿用 HTTP MCP `/mcp` 和已有工具名，不另建相似的 `*_v3` 工具族。
首批保留 MCP 协议 `2025-06-18` 的兼容能力；MCP 协议版本、服务软件版本和本合同版本是三个不同字段。

标准传输、初始化、会话及工具调用遵循官方规范，服务端采用合规 MCP 实现：
[Streamable HTTP](https://modelcontextprotocol.io/specification/2025-06-18/basic/transports)、
[Tools](https://modelcontextprotocol.io/specification/2025-06-18/server/tools)。
上述链接为协议依据，本文新增的行情字段是双方应用合同，并非 MCP 标准字段。

新客户端完成 `initialize`、初始化通知和完整 `tools/list`（处理工具列表分页），再读状态能力。
支持 JSON 与 SSE；SSE 必须按 JSON-RPC 请求 id 配对，不能取任意最后一条消息当本次结果。
通知的正常空响应不能被当作工具读取失败。会话失效后重新初始化并核验合同和能力。
这些是后续项目客户端的适配要求，本交付不宣称旧客户端已满足全部协议细节。

### 4.2 显式选择新合同

每个新请求都显式携带：

```json
{"contract_version":"stockdb-market-v3"}
```

未携带该字段的现有调用保持旧合同和旧返回形状，不默认升级。
输入 schema 必须准确区分旧版与显式 v3 分支；不能只修改说明文字。
未知版本返回 `UNSUPPORTED_CONTRACT`，不能忽略该字段而按旧版返回。
若MCP框架在函数执行前校验schema，须在其验证错误适配层完成错误分类：未知合同、歧义裸代码
分别映射为上述特定错误，其余非法参数为INVALID_ARGUMENT；不能让这些请求绕开既定错误合同。
服务状态声明支持的版本和旧版停止支持时间；首批旧版 `sunset_at=null`，不借本次改造下线已有客户端。
旧版明确的错误可以修复，但不能在没有版本参数时突然返回本合同新形状或新的代码编码。

v3 的 `tools/call` 结果同时提供 `structuredContent` 和恰好一个包含同对象 JSON 的文本块，二者解析后必须相等。
`outputSchema` 若同时支持旧版，须正确容纳两种合同，不能用仅 v3 schema 校验旧响应。
新增机器信息放在规定的 `extensions` 对象中；不能复用现有字段表达另一含义。
不兼容修改需新合同标识，两端更新同一 schema 和用例；不得只修改版本字符串。

### 4.3 三层错误

| 层次 | 表达 | 消费方动作 |
|---|---|---|
| HTTP/会话/JSON-RPC 格式或方法错误 | 对应协议错误，不能伪装为行情成功 | 记录并按会话/网络策略处理 |
| 工具级输入不合法、整个读取失败 | `isError=true`，内容为本合同 `status=failed` 信封 | 保留机器错误，不发布完整分区 |
| 部分证券失败或结果截断 | `isError=false`，信封 `status=partial`、`is_partial=true` | 解析逐证券结果，但整体不能作为完整输入放行 |

完整成功是 `isError=false/status=complete/is_partial=false/truncated=false/errors=[]`，还需通过本地用途合同。
正常闭市、上市前、已退市或证据充分的停牌可以是已解释的合法空；上游失败和不明空不是合法空。

四个首批工具遇到无法正规化的请求，统一返回schema中的`ToolFailureResponse`：
即query/request_hash/snapshot均为null、data/meta/source_catalog为空对象的failed信封，至少一项机器错误。
状态工具本身执行失败也使用该信封；列表/日历在已正规化后发生源错误，可返回各自schema的failed响应以保留scope。
因此状态、列表和日历的v3 outputSchema须接受各自正常定义与ToolFailureResponse的联合；
get_kline的KlineResponse已包含这种失败形状，不要用互相重叠的oneOf分支。
未知合同的错误由服务已支持的v3错误信封表达，details.requested_contract记录被拒绝的版本，不表示接受该版本。

## 5. 证券身份：v3 不再接受歧义裸代码

`code/codes` 在 v3 中传入**公开规范证券键**，格式固定为：

```text
<exchange>/<asset_type>/<namespace>/<native_code>
XSHE/equity/exchange/002600
XSHG/equity/exchange/603516
XSHE/etf/exchange/159915
XSHG/index/exchange/000001
XSHE/equity/exchange/000001
CN/board/eastmoney/BK1037
CN/board/ti/308384.TI
```

字段区分大小写；`exchange` 为 `XSHG/XSHE/XBSE/CN`，`asset_type` 为 `equity/etf/index/board`。
`namespace=exchange` 表示证券交易所代码；板块可用 `eastmoney/ths/sw/ti` 等公开代码命名空间，新增值须在能力中声明。
此处ti仅标识该原生代码体系，不据 `.TI` 后缀猜测指数编制方或原始事实提供者，二者须查源码和来源说明。
`CN` 只适用于跨市场提供者板块；不得用它掩盖未知的上市交易所。
对未验证命名空间或身份报错，不按名称相近就映射成其他序列。

日K `data` 和 `meta` 的键均使用请求中的规范证券键。
元数据 `instrument.code` 保存上游原生代码，例如 `002600`；`instrument.instrument_id` 保存完整键。
证券列表返回这两者及 `exchange/asset_type/namespace`，字段与规范键必须一致。
规范键消除当前股票/指数的 `000001` 冲突，也使混合沪深批量请求无需猜交易所。

v3 收到 `code="000001"` 返回 `AMBIGUOUS_INSTRUMENT`，不悄悄选择股票或指数。
语法正确但目录未验证的键返回 `UNKNOWN_INSTRUMENT`；没有该资产能力返回 `UNSUPPORTED_ASSET`。
代码迁移以新的规范键和带有效期的别名证据处理，不把旧、新代码无证据地拼为同一序列。
仅列出“六位代码符合前缀”不能作为 `identity_verified=true` 的证据。

规范键是**接口边界标识**，不要求批量迁移本项目既有数据库主键、文件名或展示代码。
项目客户端按已核实的本地身份建立映射，例如股票002600→XSHE/equity/exchange/002600，
ETF159915→XSHE/etf/exchange/159915，上证指数sh000001→XSHG/index/exchange/000001，
BK1037→CN/board/eastmoney/BK1037；服务不支持的映射仍走明确备用来源，不把它改成同名TI序列。
本地公开身份映射与查询范围选择由项目完成，不要求NAS读取项目私有配置。

## 6. `get_kline` 请求合同

完整 schema 为 `contract.schema.json#/$defs/KlineRequest`。
该对象是MCP `tools/call` 的 `params.arguments`，`params.name=get_kline`；不另建REST请求入口。

| 参数 | 类型/默认 | 精确定义 |
|---|---|---|
| `contract_version` | 必填字符串 | 固定 `stockdb-market-v3` |
| `code` / `codes` | 必须二选一 | 规范证券键；`codes` 非空、唯一、有序，最多50项；重复项报错 |
| `frequency` | 必填枚举 | `1d/1m/5m/15m/30m/60m/1w/1M`；枚举存在不代表能力已实现 |
| `start/end` | 均必填 | 日K为真实 `YYYYMMDD`，闭区间；分钟为带 `+08:00` 的 RFC3339 秒级时刻，按 bar结束时刻闭区间 |
| `fq` | 必填枚举 | `none/qfq/hfq`；首批只要求 none；不支持必须明确报错 |
| `fields` | 可选字符串数组 | 默认六字段；只能选 `open/high/low/close/volume/amount`，唯一、非空；v3 不接收旧版逗号字符串 |
| `limit` | 非负整数，默认0 | **每个证券**最多返回的行数；0表示调用方不主动截断；正数取区间内最新N行，仍按时间升序输出 |
| `snapshot_id` | 可选字符串 | 重读已捕获数据，不能用实时值替换；缺省创建新公开数据快照 |
| `adjustment_anchor` | 可选 YYYYMMDD | fq非none时必填，见第12节 |
| `factor_snapshot_id` | 可选字符串 | fq非none时必填；与因子工具快照对应 |

未知参数拒绝为 `INVALID_ARGUMENT`。`start>end`、不存在的日期、类型错误、两个代码参数同时传入均拒绝。
禁止省略 end 后暗用“今天”，禁止缺 fq 后按 SDK 默认前复权取数。
`1w/1M` 留作能力扩展；在周期起止、跨月和未完成周期细则冻结前不能声明支持。
首批的 schema 接受频率枚举后，服务能力层必须准确拒绝尚未支持的频率。
`get_market_snapshot/get_data`保持现有兼容用途，不是首批v3依赖；收盘价由同批get_kline的close派生。
如以后给它们增加v3分支，必须与同日K使用相同行情/因子版本，并另交共享schema和等价性用例。

示例：

```json
{
  "contract_version":"stockdb-market-v3",
  "codes":["XSHG/equity/exchange/603516","XSHE/equity/exchange/002600","XSHE/etf/exchange/159915"],
  "frequency":"1d","start":"20261020","end":"20261023","fq":"none","limit":0
}
```

### 6.1 字段投影和行值

行始终是对象，不返回位置数组。每行强制带 `date/bar_state/source_id`；分钟再强制带 `time`。
`date` 固定 `YYYY-MM-DD`，表示交易日；分钟 `time` 表示本 bar 结束时刻，固定 `+08:00`。
投影只影响六个数值字段，不能丢掉日期、来源或 finality。

| 行字段 | 类型 | 定义 |
|---|---|---|
| `open/high/low/close` | 十进制字符串 | 原始精度，不用二进制浮点/科学计数法；不得根据显示小数位先行粗略四舍五入 |
| `volume` | 非负整数；部分扩展可null | 股票为股，ETF为份；禁止默认“手×100”猜换算 |
| `amount` | 非负十进制字符串；部分扩展可null | CNY元；禁止猜万元、亿元或直接把 SDK 原值贴成元 |
| `bar_state` | `final/preliminary/unknown` | final必须有源终态或可核验冻结依据；盘后查询时间本身不足以证明final |
| `source_id` | 字符串 | 指向本响应 `source_catalog`，来源可以逐行不同 |

首批股票/ETF日K六字段必须有值；缺字段属于不完整，不能用null或0假造完成。
指数/板块可能没有可解释的量额；只有其能力明确声明允许null并给出原因时才能返回null，依赖量额的用途仍不得放行。
价格的正负约束由资产与复权决定：首批未复权股票/ETF OHLC必须>0；qfq等不能照搬这一约束。
所有实际返回的 OHLC满足 `low <= open/close <= high`，量额非负；零成交的真实性需来源支持。
停牌不制造平盘K线；缺行通过 coverage解释。缺失前收、涨跌幅等派生字段由项目按自身公式计算。

### 6.2 排序、limit与上游上限

每证券按日/时升序、无重复、不越界。请求证券顺序在响应 `query.codes` 中保持；JSON对象成员顺序不作语义依据。
`limit=0` 不是把0直接传入 SDK 切片；必须规避下游 `rows[:0]` 等含义差异。
正数limit取最新N行；若范围内有更多行，逐证券和顶层均报告截断，整体为partial。
`available_rows` 统计截断前该快照实际可读行数；无法确定时为null，不能假写为返回行数。

3.0.0首批不定义 cursor 参数。内部有分页时，服务自行穷尽并保持同一数据版本。
超出响应资源上限时可返回 `RESULT_TOO_LARGE`，或者返回明确partial/截断并给出相应错误，不能成功吞掉尾页。
续取办法是用**同一 snapshot_id**缩小日期范围/证券集合，随后在客户端合并并核验覆盖。
若快照范围无法完整捕获，应返回失败而非生成“完整快照”；允许的部分结果必须显式说明快照仅涵盖已捕获范围。
客户端不能用增大limit掩盖批量空数据，不能直接把多次实时查询声称为同一快照。

## 7. 日K统一返回信封

完整 schema 为 `#/$defs/KlineResponse`；单票、单元素批量和多元素批量都使用同一形状。
本节对象放在工具结果structuredContent及对应JSON文本中，不是最外层JSON-RPC响应。
除整个请求无法正规化而 `query=null` 的失败响应外，`data/meta` 恰好覆盖 `query.codes`，不得漏键或加其他证券。

| 字段 | 必填与语义 |
|---|---|
| `contract_version/contract_revision` | 固定 `stockdb-market-v3` / `3.0.0` |
| `server_version/server_build_id` | 服务软件版本和可定位构建身份，不能把合同版本当服务版本 |
| `request_id` | 服务分配的请求跟踪ID，不承载账户信息 |
| `query` | 正规化请求：永远是codes数组、显式fields/limit、snapshot_id或null；错误时可null |
| `request_hash` | 对query按第10节算法求hash；query=null时为null |
| `served_at` | 本次响应时间，RFC3339；不冒充源数据更新时间 |
| `snapshot_id/snapshot_expires_at` | 捕获快照与可重读到期时间；完全失败可null |
| `status` | `complete/partial/failed` |
| `is_partial` | 严格等于 `status != complete`，包括完全失败 |
| `truncated` | 任一证券因调用方limit、源上限或响应上限未全量返回即true |
| `data` | `{规范证券键: [行对象,...]}`；错误证券仍有空数组 |
| `meta` | `{规范证券键: 逐证券元数据}` |
| `errors` | 结构化错误数组；不能用一串说明替代 |
| `source_catalog` | `{source_id: 来源记录}`，所有行/证据引用均可解析 |
| `data_hash` | 对data求hash，投影/截断不同会得到不同hash |
| `extensions` | 可选扩展对象，不覆盖正式字段语义 |

query只含schema的NormalizedQuery字段，不重复contract_version或原始code参数；响应版本在顶层固定。

### 7.1 逐证券meta

| 字段 | 含义 |
|---|---|
| `instrument` | 规范键、原生code、交易所、资产、命名空间、name可null；`identity_verified`和`identity_source_id` |
| `status` | `ok/empty/partial/error` |
| `units` | `price=CNY/points`、`volume=shares/fund_units/index_native/unknown`、`amount=CNY/unknown`、精度 |
| `adjustment` | mode、anchor_date、factor_snapshot_id、algorithm；none时后三项全null |
| `coverage` | state、expected_rows、available_rows、returned_rows、gaps |
| `truncated/truncation_reason` | 布尔值和 `caller_limit/source_cap/response_cap`，未截断时reason=null |

units另有必填`null_reason`，正常为null；扩展资产的量额为null时必须给出原因。

`identity_source_id` 指向可说明身份的来源；仅从规范键拆出字段，不能宣称身份已验证。
`coverage.state` 为 `complete/incomplete/unknown`：

- `expected_rows`：请求范围内该交易所日历的预期bar槽位数；首批日K为交易日数，尚不扣除上市前/退市后/停牌。
- `available_rows`：本快照在完整请求范围内实际存在的行情行数，未受调用方limit影响；未穷尽上游时为null。
- `returned_rows`：本次data数组长度。
- `gaps`：每个预期槽位却无行的日期/时刻和原因，不包含普通周末等日历已排除槽位。
- 能准确计数时满足 `expected_rows = available_rows + len(gaps)`。真实未上市/退市/停牌仍列作有证据的gap。
- `expected_rows=null` 表示日历或范围未知，`available_rows=null`表示源未穷尽；均不能标complete。
- 完整且无行时 `status=empty`；有行且完整为ok；有可用行但不完整为partial；没有可用行且不能解释为合法空为error。
- 完整覆盖可包含有证据的合法gap；任何未解释gap、截断、预期槽位未知或错误均不得标complete。

### 7.2 gap原因和证据

每项gap字段为 `date/time/reason/evidence`，日K time=null；分钟按结束时刻定位。

| reason | 可成为已解释合法空？ | 必须依据 |
|---|---|---|
| `PRE_LISTING` | 是 | 有效上市日期和原始证据 |
| `POST_DELISTING` | 是 | 有效摘牌/迁码日期和原始证据 |
| `SUSPENDED` | 是 | 覆盖该日/时刻的交易状态或明确事件证据 |
| `NOT_PUBLISHED` | 否 | 尚未发布/尚未收盘，不把等待当停牌 |
| `OUT_OF_HISTORY` | 否 | 请求早于源覆盖；不是证券当时不存在 |
| `SOURCE_ERROR` | 否 | 上游超时/错误详情 |
| `UNKNOWN` | 否 | 不能判定，证据可空，保留未知 |

证据每项为 `{source_id,reference,known_at}`：reference为公开原文URL或可追溯公共记录键，known_at未知则null。
合法原因必须至少一项证据，且有效期覆盖gap。不能从“没返回行”“名字带ST”“当前不在清单”推断停牌或退市。
服务未提供足够身份/状态时可以返回unknown，由项目按现有规则另补公开证据；不能因此说NAS已经全市场完整。

### 7.3 顶层状态的唯一判定

1. 全部证券覆盖complete、全部身份已验证、无错误、无截断：`status=complete`。
2. 至少有一项可用行情或已解释合法空，但不能满足1：`status=partial`。
3. 没有任何可用行情或已解释合法空，或请求整体不合法：`status=failed`。

`errors=[]` 不足以证明成功；反向也要求complete时errors必须为空。
`preliminary/unknown`行可以在响应中出现，但其用途状态由项目判定；正式收盘冻结必须全部为final且有依据。
不能通过把未收盘的真行情伪标final，使覆盖合同通过收盘用途。

## 8. 证券列表、交易日历与状态发现

这些工具沿用现有名字，显式v3分支按下列字段返回；首批schema包含四个工具的请求和响应，
服务端须把对应定义正确接入tools/list的inputSchema/outputSchema并提交契约测试。

### 8.1 `get_stock_list`

请求必填 `contract_version/asset_types/exchanges/as_of`；asset_types非空唯一数组，
exchanges非空唯一数组，as_of为YYYYMMDD；可选snapshot_id。禁止使用一个“股票列表”混合股票、ETF却不给资产类型。

成功响应必填字段：

| 字段 | 类型与语义 |
|---|---|
| `contract_version/contract_revision/server_version/server_build_id/request_id/served_at` | 与日K相同 |
| `status/is_partial/truncated/errors` | 完整时complete/false/false/[]；失败不返回成功空全集 |
| `scope` | `{asset_types,exchanges,as_of}`，与请求一致 |
| `as_of_type` | `observed_current` 或 `historical_verified`；当前清单不伪装成历史清单 |
| `data` | instrument对象数组，结构见日Kmeta的instrument；规范键唯一 |
| `total` | 精确等于数组长度，不能是未抓完分页时猜的总量 |
| `universe_complete` | 是否穷尽该上游在所声明scope的清单；不等于交易所全市场身份已独立验证 |
| `snapshot_id/snapshot_expires_at/data_hash/source_catalog` | 不可变快照、哈希与来源；同日K通用含义 |
| `extensions` | 可选 |

首批允许只实现当前观察清单：as_of必须是本次当前清单的有效交易日期；历史日期请求报 `UNSUPPORTED_AS_OF`。
不能把“现在查到的代码”贴上过去as_of。historical_verified只在历史身份能力通过后使用。
名称、上市日或退市日若返回，未知为null；不要根据名称生成ST布尔值。
若增加list_date/delist_date/alias有效期等字段，放extensions或先修订双方schema。

### 8.2 `get_trading_days`

请求必填 `contract_version/exchange/start/end`，日区间闭区间，均为真实YYYYMMDD；可选snapshot_id。
无调用方limit；内部原400日上限不能静默截断v3。

成功响应必填通用版本、构建、请求、served_at、状态/错误/截断、快照、来源字段，另含：

- `exchange/start/end`：正规化请求值。
- `trading_days`：升序、唯一的ISO日期数组，只在请求范围内。
- `calendar_id/calendar_version`：实际日历身份与版本；version不可得时null并在extensions说明。
- `coverage_start/coverage_end`：该版本已核实可用的日历自然日边界，ISO日期。
- `data_hash`：对trading_days数组按第10节求hash。

范围越过已核实日历边界报 `OUT_OF_CALENDAR`，不能按工作日推断交易日。
若使用XSHG日历支持其他交易所，必须说明等价范围和依据；不默认所有历史永远相同。
没有交易日的周末/假期范围可complete且trading_days=[]，但来源与覆盖仍须完整。
项目本地正式交易日历继续由现有数据层负责；服务日历用于查询验证与对账，不自动覆盖本地生产日历。

### 8.3 `get_data_status`

显式v3请求只有contract_version。响应schema为 `#/$defs/StatusResponse`，包括：

- 服务名、软件版本、build_id、源码commit和镜像digest（实际不可得者为null）。
- `protocol_versions/contract_versions/contract_revision`，与实际schema和实现一致。
- `status=ready/degraded/unavailable`、`checked_at`、`data_status`。
- `issues`：运行异常数组，每项为profile_id、code、message、retryable；ready时为空，其他状态至少一项。
- `profiles`：每项id/supported/reason/capabilities；每个capability含资产、交易所、命名空间、周期、fq和历史边界。
- `limits`：max_codes、max_concurrency、request_timeout_ms、max_response_rows、snapshot_retention_seconds。
- `legacy`：default_contract和sunset_at。
- `extensions`：可选诊断；不放账户、密钥、宿主路径或请求正文中的私人标签。

历史边界为null代表不知道，不代表无限。概览latest_trade_date只说明摘要，不能证明请求范围完整。
`ready`表示本服务首批已声明能力当前就绪；可选能力不支持不使core失败。
`degraded`必须在issues逐项说明当前受影响能力，项目按数据集决定是否继续；不能一律忽略或一律替代所有数据。
健康状态从有界缓存/内部状态读取，不能每次现查全部上游或扫全仓。

## 9. 结构化错误码

错误对象固定为 `{code,message,instrument_id,retryable,details}`。
instrument_id为本次请求键或null；message供人阅读，客户端按code决策；details为无秘密的诊断对象。
同一未知gap可合并为一项证券错误并在details列gap日期，不必每行复制大段异常堆栈。

| code | retryable | 定义 |
|---|---|---|
| `INVALID_ARGUMENT` | false | 参数类型、范围、字段或互斥关系不合法 |
| `UNSUPPORTED_CONTRACT` | false | 未实现所请求合同 |
| `AMBIGUOUS_INSTRUMENT` | false | v3裸代码/歧义标识 |
| `UNKNOWN_INSTRUMENT` | false | 无法验证规范证券身份 |
| `UNSUPPORTED_ASSET` | false | 未声明资产/交易所/命名空间 |
| `UNSUPPORTED_FREQUENCY` | false | 未实现周期；不能换另一周期 |
| `UNSUPPORTED_ADJUSTMENT` | false | 不支持复权或相应因子/算法 |
| `UNSUPPORTED_AS_OF` | false | 不能提供该历史身份时点 |
| `OUT_OF_CALENDAR` | false | 超出可靠交易日历范围 |
| `OUT_OF_HISTORY` | false | 请求的必要历史不在源覆盖内 |
| `NOT_PUBLISHED` | true | 数据尚未发布；应等待新的源状态，而非紧循环重试 |
| `INCOMPLETE_COVERAGE` | false | 范围有未解释缺口；details列unknown或缺日 |
| `SOURCE_UNAVAILABLE` | true | 上游暂不可用 |
| `SOURCE_TIMEOUT` | true | 上游超过服务读取期限 |
| `SOURCE_SCHEMA_ERROR` | false | 上游形状/单位无法安全解析，需修适配 |
| `RESULT_TOO_LARGE` | false | 超出已声明资源限额；缩小范围/按快照拆分 |
| `BUSY` | true | 并发资源已占满；服务返回建议等待毫秒 |
| `SNAPSHOT_NOT_FOUND` | false | 不存在该快照 |
| `SNAPSHOT_EXPIRED` | false | 快照已过保留期；不得悄悄重抓实时值 |
| `SNAPSHOT_SCOPE_MISMATCH` | false | 新查询超出已捕获证券、时间、周期或复权范围 |
| `INTERNAL_ERROR` | false | 未分类实现异常，request_id用于追踪；不能包装为成功空数组 |

retryable仅表示同一语义请求将来可能恢复，不授予无限重试。
项目每次采集最多3次尝试，有界退避；切备用源必须重新验证身份、单位、复权和范围。
返回部分数据时也保留错误；错误证券data=[]且meta.status=error、coverage不完整，不能丢掉该证券键。

## 10. 来源、时间、哈希和快照

### 10.1 来源记录

`source_catalog[source_id]` 固定字段：

| 字段 | 定义 |
|---|---|
| `provider` | 原始事实提供者；未知为null并在unknown_fields说明，不把http/sdk/warehouse当提供者 |
| `aggregator` | 聚合/转发软件或服务，可null，例如free-stockdb |
| `access_path` | `http/sdk/warehouse/cache`，仅描述访问路径 |
| `reference` | 公开URL或公共数据集/记录标识，可null；不含令牌、签名秘密或NAS本地路径 |
| `retrieved_at` | NAS首次获取本批原始内容的时间，必填；重读缓存不更新它 |
| `source_updated_at` | 上游明确给出的更新时间，未知null |
| `published_at` | 上游明确给出的发布时间，未知null |
| `finality_evidence` | 原始终态标记、已核实发布机制/公共快照依据，可null；不能只写“现在已过收盘” |
| `unknown_fields` | provider/reference/source_updated_at/published_at/finality_evidence中未知字段的列表 |

时间均为带时区RFC3339，日K date是交易日而非发布时间。
served_at、retrieved_at和published_at三个含义不得混用；只晚查一次不能补造历史“当时可知”。
未知元数据允许如实返回，但项目可因用途要求而拒绝晋升；“schema合法”不等于“正式用途已通过”。

### 10.2 确定性哈希

为避免跨语言浮点序列化差异，数据价格/金额为十进制字符串，volume为整数。
`canonical_json_v1`：UTF-8、对象键按Unicode码点升序、无额外空白、非ASCII直接输出、
字符串按JSON标准转义，禁止NaN/Infinity；`/`不额外转义。参与hash的query/data不允许浮点数。
Python等价为 `json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",",":"), allow_nan=False)`。
字符串不做Unicode规范化；标准字段使用ASCII；量整数不超过2^53-1，超出该范围报源解析错误。

request_hash = SHA256(canonical_json_v1(query))，data_hash = SHA256(canonical_json_v1(data))，均为64位小写hex。
query把单code正规化为codes数组；fields按 `open/high/low/close/volume/amount` 的固定顺序正规化；
limit默认0、snapshot_id/adjustment_anchor/factor_snapshot_id缺省为null；其他请求值不擅自改变。
snapshot_id是不可变数据捕获身份，不等于query hash；不同子查询可以读取同一快照并产生不同data_hash。

### 10.3 快照与研究复现

新读取创建不可变快照：保存已捕获的规范行情、来源、身份、日历依据、因子引用和范围清单。
至少保留7个自然日；到期时间显式返回。项目公开数据层长期保存自己已验收的事实和采集证据。

同一snapshot_id重复读取/拆分，只能使用已捕获证券与范围、相同周期/fq/因子锚点；允许字段子集和更小日期区间。
服务捕获完整六字段，再投影响应，保证同一快照内的fields对照不依赖后续实时上游。
不能把新增证券或新日期暗中拼进旧快照；范围外返回SNAPSHOT_SCOPE_MISMATCH。
快照过期/找不到明确失败。服务重启若丢失快照也必须明确失败，不能同ID返回新值。

快照仅证明该次捕获内容可重读，**不自动证明多个请求在上游的原子一致性**，
也不提供历史任意时刻as-of查询。全市场由多个批次组成时，项目保留每批快照和真实采集时段；
若服务支持仓库全局原子快照，另声明global_snapshot能力并给出版本一致性证据。
历史回测按公开可知时间筛选；没有历史知识时间的数据，只能说明事后复算，不伪装成当时可交易样本。

## 11. 指数、板块与分钟扩展的明确限制

### 11.1 指数/板块

仍使用get_kline和本信封，分别声明namespace、身份、价格单位及量额可用性。
`get_board_members/bk_get`只说明映射/成分；映射成功不代表对应指数日K存在。
板块更名、来源切换、编制规则或历史成分变化保留自己的版本；当前成分不能当历史成分。
BK与TI/申万的同名板块不得互换native_code、拼接历史或沿用对方点位阈值。
首批core无需补齐所有板块；项目在既有来源保留缺口与降级证据，待独立验收后切换。

### 11.2 分钟

分钟能力按周期和市场分别声明；返回周期必须与请求一致。
时间戳统一为bar结束时刻、区间 `(上一bar结束, 本bar结束]`；跨午休不制造bar，
首bar区间按交易所开盘时刻开始，明确集合竞价成交是否纳入首bar。
1m标准连续竞价时段的结束标签为09:31—11:30、13:01—15:00；
只有与交易所该日期规则一致且竞价归属说明经过核验时才能启用，不能把任意源的09:30标签简单改名。
其他周期按相同交易时段分桶；源原生粒度和边界无法等价时不声明支持。
停牌/临停/无成交缺分钟逐槽位保留解释；不前向填充合成真实K线。
项目现有分钟价格点产品可能与OHLCV分钟条的合同不同，适配时逐项验收，不仅比较行数。

## 12. 复权扩展合同

首批允许 `adjusted_history_v1=false`；此时fq=qfq/hfq直接明确拒绝。
启用该能力前，必须同时实施本节、双方对应schema和同一回算样本，不能仅返回SDK字符串“qfq”。

v3 `get_adjust_factors`请求：contract_version、code/codes二选一、start/end、fq=qfq/hfq、
adjustment_anchor均必填；可选snapshot_id重读已捕获因子。不再使用语义不明的date_pattern作为v3生产请求。
响应复用第7节信封与规范证券键，每行改为：

```json
{"date":"2026-10-20","multiplier":"1.00000000","offset":"0.00000000","source_id":"s1"}
```

算法固定 `affine-v1`：`adjusted_price = raw_price * multiplier + offset`，分别用于OHLC；
multiplier>0，offset可负。锚点当日multiplier=1且offset=0。
qfq锚点取请求显式指定的已完成交易日，hfq锚点取明确的基准交易日；不随查询当天自动漂移。
因子逐交易日对应，精度至少足以在声明price_decimals内复算；量与额保持真实成交口径、不复权。
若原始SDK无法导出该算法的真实因子/偏移，不得猜 `div/give/trans/mult/cum` 的字段含义后冒充本合同。
可以保留SDK原始记录到extensions，但需先定位真实公式并证明等价，否则该能力保持false。

因子响应snapshot_id即factor_snapshot_id，meta.adjustment记录mode/anchor/algorithm。
随后get_kline的fq非none请求必须携带该factor_snapshot_id和相同锚点；返回行情snapshot保留因子快照引用。
因子修订生成新身份，不覆盖旧因子；同一原始行情+同一因子必须可重算到声明精度。
本包首批日Kschema不会把因子行误判为日K行；扩展schema文件需按新类型另行补齐并一并交付。

## 13. 服务运行合同

首批建议部署配置也作为首轮验收基准，变更时更新状态声明和联合验收记录：

| 项目 | 首批基准 |
|---|---|
| max_codes | 50，批量不能在内部静默只处理前N个 |
| 行情执行并发 | 4，有限队列；满载明确BUSY，不无限堆积 |
| 单请求期限 | 最多30秒，含排队和上游读取；更长研究作业另设已声明配置，不改变轻行情期限 |
| 单响应行数上限 | 200,000，按所有证券行数合计；超限按第6节处理 |
| 快照保留 | 至少604,800秒 |
| 轻量状态 | 正常与注入慢请求时，服务本地测得p95≤2秒 |

并发额度、期限、行数上限必须从实际配置声明，客户端使用不高于已声明限额。
首轮服务可提交不同资源配置，但须说明原因并用相同验收矩阵证明有界失败和恢复，不能私改语义。
上游同步SDK不能无限阻塞MCP事件循环。到期后释放或隔离任务和资源；超时线程不能持续占满全部行情槽位。
客户端断开不自动视为取消已执行任务；支持协议取消时按MCP处理，另有服务自身期限兜底。
业务慢请求不能阻断initialize、tools/list和缓存状态；重启/错误日志能关联request_id。
记录工具、合同版本、公开请求hash、耗时、结果状态、源调用、超时和构建身份；不输出密钥或私人业务标签。

## 14. 联合验收：服务完成与项目晋升分别判定

所有验收按 `acceptance-cases.json` 的固定编号报告：PASS/FAIL/NOT_RUN及证据。
首批不允许用NOT_RUN冒充PASS；扩展未实现时声明不支持并通过拒绝用例，可以不阻塞core。
固定样本是合成数据；真实服务测试比较同快照、同范围、同单位，不把历史实测价格长期写死为当前真值。

每份报告必须另写phase=`service/client/joint`，每个用例保留子断言结果。
C01—C22中标注service+client的项，在服务阶段先验服务输出/协议责任，客户端责任留到client阶段；
单端PASS不能标为joint PASS。C23—C24仅属于后续项目接入，服务交付时如实NOT_RUN，
不阻塞服务侧交付，但仍阻止宣称本项目已切换或已晋升。扩展功能未实现时，记录“不支持声明/拒绝测试PASS，功能NOT_RUN”。

### 14.1 服务端完成条件

1. 确认源码路径、commit、构建产物/镜像digest、配置和回滚版本；版本号不能只存在于说明文档。
2. schema和真实tools/list一致，显式v3可用，旧请求兼容；新旧形状分别有测试。
3. C01—C22的服务侧断言通过，含本次批量空数据和单元素形状回归、错误/合法空/截断、来源/单位/快照。
4. 注入源错误、超时、并发满载后，轻量服务可用，行情槽位恢复；保留运行证据。
5. 提供真实公开响应样本、通过的测试输出和能力清单；不访问项目私有数据库作实现前提。

### 14.2 项目接入及晋升条件

服务完成不等于本项目已切换。项目后续依第15节适配，在隔离环境走采集→校验→发布→data_core读取。
行情/收盘窗口、身份、来源与单位通过；该用途需要的元数据未知时保留候选或备用来源。
按项目既有合同完成至少5个连续实际交易日观察及相应审计，不补造历史“事前运行”。
完整日终还依赖本地成交、Attention、公告、复盘和指南；这些不作为StockDB服务能力要求。

### 14.3 必须交付给消费方的材料

- 服务软件版本、构建身份、源码commit、镜像digest、实际访问地址和部署配置（无秘密）。
- 本合同修订号及**完全相同**的schema/用例；如有必要变更，提交明确diff和影响，不单方面另造字段。
- 支持/不支持的能力矩阵、历史范围、单位、日历/分钟边界和原始来源说明。
- 按用例编号的测试结果、公开响应原件、故障恢复记录和仍未解决的限制。
- 报告phase和逐断言状态；项目侧未接入的部分保留NOT_RUN，不把服务测试当作联合验收。
- 旧版兼容/退回方法，以及服务端已完成、本项目未切换的明确状态。

## 15. 本项目后续适配清单：与服务逐项对应

本合同只固定这些后续工作，不在本轮修改生产取数逻辑。

| 项目现有文件/入口 | 对应服务能力 | 后续必须做的适配 | 共同验收 |
|---|---|---|---|
| `scripts/stockdb_http_client.py` | 状态、schema、v3、快照 | 按用途检查工具/能力；正确MCP会话与SSE配对；统一信封解析；保留meta/source；拒绝错误行而非静默过滤 | C01—C06、C15—C22 |
| `scripts/update_market_data.py` 的prices/kline/daily | get_kline core | 本地公开身份映射；日K与收盘来自同一快照；十进制/单位转换；按用途检查finality和窗口 | C03—C14、C23 |
| `scripts/stockdb_daily_collection.py` | list、calendar、kline | 冻结目标范围；逐证券×交易日核验；unknown/partial不发布complete；保持调试子集隔离 | C07—C13、C24 |
| `scripts/stockdb_fullmarket.py` | fullmarket独立配置 | 不把ETF混进A股全集；保留身份和状态不确定性；全市场来源/覆盖独立验收 | C24、X01 |
| `kline_main/fetch_index_history` | index_board扩展 | 规范指数/板块键，独立序列和来源；不跨BK/TI拼接 | X02 |
| `minute_main` | minute扩展 | 显式周期与边界，保留原完整时段/用途合同 | X03 |
| 正式研究采集 | 因子、快照、历史身份 | 冻结数据/因子和知识时间；不扩张legacy脚本，不绕过data_core | X04—X05 |

项目后续代码改动需运行对应StockDB测试、数据消费者边界审计，以及涉及的数据产品审计。
服务维护Agent只交付服务与公开契约证据，不在本项目修改私有事实、正式报告或来源晋升开关。

## 16. 交付包自检

在本项目使用已锁定环境运行：

```bash
uv run python docs/contracts/stockdb-market-v3/validate_contract.py
```

检查服务产生的已解包日K信封（不是最外层JSON-RPC）：

```bash
uv run python docs/contracts/stockdb-market-v3/validate_contract.py --response /path/to/kline-response.json
```

列表、日历、状态或统一失败信封分别加`--kind StockListResponse/TradingDaysResponse/StatusResponse/ToolFailureResponse`。

其他仓库可使用自己的Python环境和jsonschema 4.x运行同一脚本，不必复制本项目环境或数据库。
离线检查不连接NAS，不发布数据；它能发现形状、计数、hash和部分语义错误，不能证明行情真实性或服务负载通过。
