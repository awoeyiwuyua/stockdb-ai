# StockDB 指数/板块日K、复权与长历史交接包

补充合同：`stockdb-market-v3-research-extension`，修订`1.0.0`，2026-10-25。

本包是消费方提出的开发与验收规格，不是服务现有能力声明。维护Agent先核实已部署实现：
已具备的能力直接提交证据和补齐回归，缺失的能力按本包实施。

## 阅读顺序

1. [HANDOFF.md](HANDOFF.md)：给另一个工作区Agent的任务与交付边界。
2. 基线`stockdb-market-v3/3.0.0`的README、schema及验收用例。ZIP内位于`baseline/`；
   本仓库原件位于相邻`../stockdb-market-v3/`，两份必须逐文件字节一致。
3. [DEVELOPMENT.md](DEVELOPMENT.md)：范围、接口、字段语义、算法、工程步骤及双端对应关系。
4. [ACCEPTANCE.md](ACCEPTANCE.md)、[acceptance-cases.json](acceptance-cases.json)：验收矩阵、证据和放行规则。
5. [contract.schema.json](contract.schema.json)、[examples.json](examples.json)、[validate_contract.py](validate_contract.py)：
   补充机器合同、明确标为合成的样例和离线检查器。
6. [support-matrix.template.json](support-matrix.template.json)、[report.template.json](report.template.json)：回传格式。

## 两项工作

| 批次 | 结果 | 服务能力配置 |
|---|---|---|
| A | 4个指定指数、7个指定东财板块的可核验未复权日K和长窗口；同花顺参考序列另报支持状态 | `index_board_daily_v1` |
| B | 沪深股票/ETF的长历史、qfq/hfq因子及可复算行情；指数/板块保持自身点位口径 | `adjusted_history_v1`，长历史按各既有profile声明 |

两批可分别交付、分别验收。A缺少某个东财板块时报告该序列BLOCKED及证据，交付已通过序列；
不得用同主题同花顺指数补齐东财代码，也不得宣称A全部完成。

## 兼容与当前状态

- 股票/ETF未复权日K、同批收盘已在本项目启用NAS主源，原来源保留校验及整段备用。
- 本包不改基线六文件，不修改既有`$defs`，不改变四个核心工具的已冻结请求/响应。
- 继续使用`contract_version=stockdb-market-v3`及线协议`contract_revision=3.0.0`。
  新增内容是原合同已经预留的profile、`get_adjust_factors`的v3分支及`extensions`约束。
  补充合同身份放入`extensions.research_v1`，不能混用服务软件版本、线协议修订与补充合同修订。
- 旧版无contract_version请求继续按旧版兼容；已投产的四工具schema保持原有定义精确相等。
- 财务、公告、新闻、策略、账户、持仓、成本、成交和研究判断均不属于服务开发范围。
- 本次不要求5个交易日观察期；以逐项测试、真实部署证据及消费方联合验收为准。
  服务通过不代表项目扩展能力已切换，本项目后续独立接入和发布。

## 离线检查

在本仓库使用：

```bash
uv run python docs/contracts/stockdb-market-v3-research-extension-v1/validate_contract.py --self-test
# 检查单份已解包响应（kind按文件类型选择）
uv run python docs/contracts/stockdb-market-v3-research-extension-v1/validate_contract.py --response response.json --kind FactorResponse
# 校验回传报告、逐断言及包内证据hash；必验项未通过退出2
uv run python docs/contracts/stockdb-market-v3-research-extension-v1/validate_contract.py --report acceptance-report.json --evidence-root return-package
```

在其他工作区按其Python环境规范运行同一脚本，需要`jsonschema>=4.25,<5`。
该检查器不访问NAS、不写业务库；只检查schema、样例和部分跨字段/回算不变量。
离线PASS不证明真实行情、来源证据、覆盖范围、部署或服务可靠性。
`build_artifacts.py`可重建schema、合成样例、56项清单及报告模板；合同变化必须同时更新生成器与文档。

## 交接包完整性

ZIP带`MANIFEST.sha256.json`，覆盖全部文件（不含manifest自身），并附原基线六文件。
回传同样生成文件清单和SHA256；不得仅回传“测试通过”的文字摘要。
