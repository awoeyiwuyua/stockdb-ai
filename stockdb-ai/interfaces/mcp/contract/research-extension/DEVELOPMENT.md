# 开发规格

规范等级：MUST/必须为验收条件；可选项不能冒充已支持。补充合同修订1.0.0。
本文件细化基线3.0.0第10—13节，不另建项目架构路线。

## 1. 范围与两端责任

NAS提供公开行情、身份和来源、交易日历、因子与快照；本项目选择用途、校验、备用与发布。
只允许发送公开证券键、日期、周期、复权、字段和快照引用；不附账户/持仓标签。
公开查询范围由本地决定，NAS不存研究假设、策略结果或其他私有数据。

| 事项 | NAS责任 | 本项目后续责任 |
|---|---|---|
| 能力协商 | 按profile、资产、市场、namespace、频率、复权、历史范围准确声明 | 把只检查core的逻辑改为按用途匹配profile；不让可选扩展故障阻塞core |
| A指数/板块 | `get_kline`按规范键返回独立序列；`get_data_status`声明真实覆盖 | 改指数/板块采集入口、本地标识映射和发布；保持东财与同花顺独立 |
| B因子 | 现有`get_adjust_factors`增加显式v3分支、FactorRequest/Response及失败联合 | 客户端增加因子工具定义/解析器，传递锚点与因子快照参数 |
| B复权行情 | `get_kline`返回qfq/hfq及对应原始行情快照；原始数据不改写 | 独立存储原始/复权事实，支持负数或零的合法复权价格，防止旧正价格闸门误用 |
| 长历史 | 取完上游分页，声明边界、缺口和截断；日历超过400自然日仍完整 | 按证券×交易日核验，不以返回条数判全；处理有证据的合法gap |
| 来源与发布 | 服务保留原始provider、单位、修订和不可变快照 | 发布公开ODS/派生产品，消费者只经data_core读；本地备用规则继续有效 |

服务维护Agent不在本次修改本项目生产代码。上述消费方工作是联合验收前置任务，不能算NAS缺陷。

## 2. 兼容规则与发现

### 2.1 保持已投产协议

四个核心工具的冻结定义、旧版行为、传输、错误码、hash和来源时间规则沿用基线。
所有本包请求仍传`contract_version=stockdb-market-v3`，不新增`contract_revision`请求参数。
既有响应仍返回`contract_revision=3.0.0`；补充身份位于`extensions.research_v1`。
本包schema完整复制原`$defs`并只追加新定义；不得为了新增能力改写KlineRequest/KlineResponse等原定义。

`tools/list`每工具inputSchema/outputSchema根类型为object，保存基线原定义及其传递依赖。
`get_adjust_factors`输入包括已有legacy分支及FactorRequest，输出包括legacy、FactorResponse、ToolFailureResponse。
通过contract_version区分分支，合法成功/部分/失败都须匹配实际outputSchema。
不能用无约束object兜底v3。旧客户端无需因本扩展重写核心日K解析。

### 2.2 能力声明

仍使用六个既有profile ID。A启用`index_board_daily_v1`；B启用`adjusted_history_v1`。
`supported=true`只代表capabilities列出的组合，逐证券限制必须在支持矩阵和研究元数据中可核验。
不要求一次支持所有指数、板块、市场；不支持的序列明确报UNKNOWN_INSTRUMENT或UNSUPPORTED_ASSET，
错误选择依基线语义，不得伪装为空的完整行情。qfq/hfq不支持时为UNSUPPORTED_ADJUSTMENT。

`get_data_status.extensions.research_v1`必须包含：

- 补充合同身份；
- 对长历史目标的声明：指数500、东财板块250、沪深成熟股票/ETF500个交易日；
- 历史边界与例外，最终以每个响应`series_catalog`中的实际边界为准。

完整状态样本仍通过基线StatusResponse。单个可选profile不可用时如实degraded；core客户端仅检查其需要的profile。

## 3. A：指数与板块日K

### 3.1 必验指数

| 名称 | 规范键 | 本地代码 |
|---|---|---|
| 上证指数 | `XSHG/index/exchange/000001` | 000001 |
| 深证成指 | `XSHE/index/exchange/399001` | 399001 |
| 创业板指 | `XSHE/index/exchange/399006` | 399006 |
| 中证1000 | `XSHG/index/csi/000852` | 000852 |

上表为本次约定的公开身份键；维护者须以公开编制机构/发布市场资料证明映射，不能从数字前缀推定。
其中XSHG在中证1000键中指本合同选择的行情发布市场定位，csi指编制者命名空间；
不表示该指数仅包含沪市成分。源使用其他代码时保留原码映射证据。
无法证明该键含义或与现有服务冲突时，先交映射提案及证据，暂停该序列放行，不能自行改键或串到股票。

`XSHE/equity/exchange/000001`与上证指数必须有不同identity、缓存键和源请求。
本包只要求`frequency=1d,fq=none`的指数，不给指数套股票除权因子。

### 3.2 必验东财板块与独立参考

| 东财规范键（CN/board/eastmoney/前缀） | 名称 | 可选同花顺参考键（CN/board/ths/前缀） |
|---|---|---|
| BK1408 | 机器人 | 886069 人形机器人 |
| BK1134 | 算力 | 885957 东数西算(算力) |
| BK1138 | 液冷 | 886044 液冷服务器 |
| BK1101 | 先进封装 | 886009 先进封装 |
| BK1037 | 消费电子 | 881124 消费电子 |
| BK0728 | 环保 | 881181 环境治理 |
| BK1160 | 折叠屏 | 885809 柔性屏(折叠屏) |

这些参考关系来自本项目既有映射，仅供相对涨跌背景；不是相同指数的身份断言。
例如完整键为`CN/board/eastmoney/BK1138`与`CN/board/ths/886044`。
BK数据取不到时，BK请求必须如实失败或partial；THS数据只在请求对应THS键时返回。
板块更名、编制规则变化、历史回溯改算均记录provider/series/methodology版本；不得悄悄拼接不同序列。
成员列表工具只提供成分/映射，不证明板块日K存在。当前成员不能冒充历史成员。

### 3.3 返回与单位

复用基线KlineRequest/Response；日区间闭合、排序唯一、默认六字段、按证券覆盖。
指数/板块OHLC为points，必须真实有效且>0。amount若存在为CNY；volume明确为index_native或可验证的股数口径。
不能证明量额单位时可返回null并给units.null_reason，不能编零或猜100倍换算。
价格链路可按price-only单独验收；依赖成交量的本项目现有复盘指标必须另标BLOCKED，不能给全面PASS。
本项目当前指标校验要求正价格和有效volume，消费方后续需按用途拆分资格，NAS不承担造数适配责任。

`extensions.research_v1.series_catalog`逐请求证券列出SeriesDescriptor：

- 稳定的series_id、instrument_id、原始provider、编制方法id/version（未知null）；
- 实际可证明history_start/history_end，不把概览latest_trade_date当逐证券边界；
- calendar_exchange/calendar_id/calendar_version，CN板块必须明确所依据交易所日历；
- native_volume_unit与volume_multiplier_to_wire，未知null并说明，禁止推猜；
- origin_reference：公开URL/可追溯记录键，不泄露NAS物理路径或秘密。

有行情的成功证券以上身份、来源、范围、日历信息必须非空；方法版本不可取得允许null并记限制。
错误证券的descriptor可为null。目录恰好覆盖query.codes，不得混入无关证券。

## 4. 长历史：A/B共用要求

| 范围 | 必验窗口 | 目标与合法例外 |
|---|---|---|
| 4指数 | 最近500个已完成交易日 | 其中250日及500日边界对照；源覆盖不足明确BLOCKED |
| 7东财板块 | 最近250个已完成交易日 | 有证据的新编制日期可解释之前gap；已有历史不足不能猜PRE_LISTING |
| 沪深股票/ETF | 最近500个已完成交易日 | 至少4只成熟股票（沪深各2）及2只成熟ETF（沪深各1）；公开抽样 |
| 扩展压力样本 | 1000日、上市至今 | 能力探测/压力项；不作为所有证券的统一必达条件，不支持或超限须明确 |

窗口由真实交易日历产生，250/500是交易日而非自然日。约束指预期槽位，不保证每证券都有同数量行。
上市前、停牌、摘牌按基线gap证据处理；UNKNOWN/OUT_OF_HISTORY/NOT_PUBLISHED不能算完整研究输入。
若存在合法gap，可达到“覆盖解释完整”，但依赖连续价格的本地指标仍单独判断，不能自动填充。

`get_trading_days`必须完整覆盖500/1000日探测所需自然日范围，不得保留400自然日静默上限。
上游分页必须穷尽并去重，边界恰好相接；跨年、春节长假、闰日、源页边界都测。
内部穷尽后再形成快照；同快照分段读可与整段拼回逐字段一致。

`limit=0`不主动截断；正数每证券取最新N行，截断必须partial。
单请求仍遵守基线30秒总期限、有限并发及实际行数上限，不能为长历史无限延长MCP请求。
允许后台预热公开缓存或内部预抓取，但请求未就绪必须返回可解释失败；不新增未经合同定义的异步RPC。
超过上限可RESULT_TOO_LARGE；必须交代合规的同快照分段可用范围，不能建议重复实时分页冒充同一捕获。
如果不能完整捕获大窗口，明确失败及限制，不谎称可用的全范围快照。

## 5. B：因子请求与返回

### 5.1 固定请求

复用`get_adjust_factors`，v3请求字段以schema的FactorRequest为准：

| 字段 | 要求 |
|---|---|
| contract_version | 固定stockdb-market-v3 |
| code/codes | 二选一，规范股票/ETF键，唯一，非空且不超过声明max_codes/50 |
| start/end | YYYYMMDD真实日期、闭区间；start<=end |
| fq | qfq或hfq，不接受none |
| adjustment_anchor | 显式已完成交易日；不得默认今天 |
| snapshot_id | 可选，重读因子快照；不是行情快照 |

不接受frequency、fields、limit、date_pattern或未知参数。日期、范围、身份及hash错误按基线处理。
首批必须支持qfq锚定请求末交易日、hfq锚定请求首交易日；其他锚点若不支持，明确拒绝并声明限制。
快照子区间重读保留原锚点，即使锚点不在子区间内也不得重置锚点。
指数/板块因子请求拒绝UNSUPPORTED_ADJUSTMENT；不能套用成分股因子。

### 5.2 FactorResponse

继承基线ResponseHeader，query改为FactorQuery，meta改为FactorMeta，data改为FactorRow数组。
所有字段与错误语义见schema；status/coverage/source_catalog/request_hash/data_hash沿用基线原则。

每行：`date,multiplier,offset,source_id`。系数为规范十进制字符串（最多12位小数），multiplier>0。
每证券每个实际存在原始行情的交易日有一组有效系数，不能只返回分红事件日让客户端自行猜延续规则。
原始行情合法缺行可有相同有证据gap；有原始行情但缺因子必须partial/failed，不用1/0补空。
工具未正规化的失败复用ToolFailureResponse；可正规化的单证券失败保留请求键和错误meta。

FactorQuery正规化为且仅为：codes,start,end,fq,adjustment_anchor,snapshot_id；缺省snapshot_id=null。
request_hash按该query求SHA256，data_hash按data求；不混用Kline的NormalizedQuery。
schema根对象与ToolFailureResponse共同覆盖该工具所有v3输出。

FactorMeta包含instrument、status、coverage、adjustment、truncated/truncation_reason和derivation。
adjustment沿用基线：algorithm=affine-v1，mode等于fq，anchor_date等于请求，factor_snapshot_id等于本响应snapshot_id。
derivation是原始公式的证据：methodology_id/version、原始source_id和可追溯formula_reference；失败证券允许null。
没有可用因子且coverage不完整的错误证券，其adjustment也可为null；不得编一个不存在的快照ID。
必须解释SDK原始系数如何转换为multiplier/offset，给出源码/公开资料及现金/送转样本，不按字段名猜。

## 6. 复权公式、原始行情和快照

唯一验收公式：

```text
adjusted_O/H/L/C = raw_O/H/L/C × multiplier(date) + offset(date)
```

- 锚点日multiplier=1、offset=0；qfq和hfq均按固定显式锚点定义。
- multiplier>0保证OHLC次序；合法复权值可以<=0，不能套原始股票OHLC>0的检查或删除这类历史。
- volume和amount保持真实未复权成交值，与对应原始快照逐日精确相等。
- 用Decimal精确计算；按price_decimals、ROUND_HALF_UP输出，不用float先算再转字符串。
  必须提供足够系数精度，使实际输出等于合同舍入结果；不支持的算法不冒充affine-v1。
- 不跨原始提供者或方法版本拼因子，不用当前因子静默替换旧因子。

调用顺序：

1. `get_adjust_factors`创建因子快照F。
2. `get_kline(fq=qfq/hfq, adjustment_anchor=..., factor_snapshot_id=F)`：
   服务捕获同一次用于计算的原始六字段行情快照R，按F计算，创建复权行情快照A。
3. A返回`extensions.research_v1.raw_snapshot_id=R`；该引用必须可用`get_kline(fq=none,snapshot_id=R)`重读。
4. 消费者读R和F复算A；A的meta.adjustment保存F、锚点和算法。
5. 重读A使用A作为snapshot_id，同时仍携带F和相同锚点/fq；服务只读原捕获，不能访问实时源换值。

R覆盖本次全部证券、日期及六字段；R和F各自源时间保留，不能声称两个独立来源在上游原子同步。
R/F/A需固定身份与日历依据。三者的捕获关系写入补充元数据并保持可追溯。
因子范围、证券、fq或锚点不匹配为SNAPSHOT_SCOPE_MISMATCH；过期/找不到按基线相应错误。
不允许把原始R通过更换fq当作A重放，也不允许把F传入行情snapshot_id参数成功。

### 6.1 修订、重启与知识时间

R/F/A至少各保留7个自然日。普通进程/容器重启后仍可重读本扩展承诺的未到期快照；必须有持久化及恢复测试。
若磁盘丢失等不可恢复，明确快照失败并报告违约，不能用同ID返回新内容。
上游修订产生新快照、新data_hash并保留旧值；series_id不因普通数据修订随意变化，方法变更必须有版本。
retrieved_at、served_at、published_at和source_updated_at分别记录；未知时间为null并说明。
历史新查询能证明事后复算，不能证明过去当时可知；本包不要求凭空补齐任意历史as-of数据库。
更长快照保留（如90日）可选；本地项目长期保存已验收公开事实，NAS不承担私有研究档案。

## 7. 补充元数据的机器结构

`extensions.research_v1`在本包A/B及长历史响应中必填，普通core未复权短窗口可不带。
字段：extension_contract、extension_revision、raw_snapshot_id、series_catalog。
SeriesDescriptor与ResearchMetadata见schema；get_data_status使用独立的ResearchStatusMetadata。
不改变原字段语义，不引入本地文件路径或账户信息。
source_id仅在其响应的source_catalog内有效；跨R/F/A比较来源须展开各自目录，不能按同名s1直接关联。
既有get_kline/get_data_status定义不改，补充校验器另校验extensions中的对象。
get_data_status扩展中的history_targets表示本包验收目标，known_limitations列出未满足项，不能当成已实测覆盖。
成功行情的series_catalog是该次价格序列描述；因子响应对应因子系列及方法来源，不能把它当行情提供者证明。

## 8. 实施步骤和完成边界

### A实施

1. 建立规范identity到真实源请求的确定映射和证据，缓存键含资产/namespace/series/frequency/fq。
2. 统一指数/板块源解析、量额单位和序列版本；保留独立提供者。
3. 修复长窗口、日历、分页、覆盖和snapshot子读取；完成4指数/7板块矩阵。
4. 重跑核心兼容及A验收，再部署实际端点验证，记录未解决序列。

### B实施

1. 先查清源因子原始公式和边界，再实现FactorRequest/Response及工具schema。
2. 建立逐日系数、因子快照及derivation证据；实现固定锚点和原始快照关联。
3. 实现Decimal仿射与舍入、量额不变和修订重放；禁止悄悄改写原始OHLC。
4. 完成500日成熟股票/ETF矩阵、真实除权事件回算及故障测试，再部署验收。

两批都要检查源失败、缓存污染、超时后槽位恢复、状态服务不被阻塞、schema合法失败和重启恢复。
维护者按实际架构定位文件，不按消费方猜测创建模块或绕过原有服务层。
若必须修改基线协议，先提交明确的版本迁移提案与影响，不能直接改变在用3.0.0定义。
