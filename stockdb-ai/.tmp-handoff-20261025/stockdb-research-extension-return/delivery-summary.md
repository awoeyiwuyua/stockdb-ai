# delivery-summary — stockdb-market-v3-research-extension 1.0.0

服务端交付：**0.14.0**，build `src+0.14.0+6fe4803`（commit 6fe4803，镜像
stockdb-ai:0.14.0，端点 http://192.168.50.146:8081/mcp）。按 HANDOFF.md 分
「实施 / 部署 / 验收」与逐序列/逐模式如实报告。

## 总体判定

- **B 批（因子/复权/R/F/A、股票 ETF 长历史）：服务侧必验项全部 PASS。**
  qfq/hfq 双模、真实现金事件（000001@20260924，div 0.249，mult 1.022 由
  pre_close/(pre_close−div) 独立验证）与真实送转事件（002594@20250729，
  give 0.8 + trans 1.2）回算、R/F/A 复算/重放/互传负例、**A15 重启持久化为
  docker restart 部署级实测**（F/R/A 同 id 值不变）、Decimal 精度矩阵。
- **A 批指数（4 序列）：PASS（price-only）**。量额单位不可独立证明 → 按合同
  返回 null + units.null_reason（I06 路径）；价格链路（身份/日历/覆盖/对照）全绿。
- **A 批板块（7 BK 序列）：BLOCKED**。eastmoney push2his 验收窗口全程不可达
  （多主机/协议/cookie/IPv4/IPv6 pin 均探测，见 logs/upstream-probes.md）。
  逐序列 BLOCKED + 如实 SOURCE_UNAVAILABLE，**不以 THS 序列冒充（I04 PASS）**，
  状态工具 degraded + known_limitations 申报。**A 批为部分完成：指数交付、
  板块 BLOCKED，不宣称 A 全部完成。**
- **长历史**：4 指数/股票/ETF 500 交易日矩阵 PASS（600000 有 6 会话引擎数据洞，
  如实 incomplete）；1000 日探测按合同明确 OUT_OF_CALENDAR（日历核实边界
  2024-01-01，H10 声明式支持，无静默截断）。

## 验收计数（acceptance-report.json，56 case；离线校验器 1094 项证据哈希全过）

{"BLOCKED": 1, "NOT_RUN": 10, "PASS": 45}

- 逐 case：45 PASS / 1 BLOCKED（I03 板块源不可达）/ 10 NOT_RUN（J01—J08 消费方
  8 项 + I07 无更名事件窗口 + H04 无停牌样本窗口；均附 reason）
- live 模式 36 PASS/0 FAIL；nas 模式（部署端点，最终构建）37 PASS/0 FAIL；
  A14 注入用例以 live 证据 + 单测覆盖（部署端无注入接口，如实 NOT_RUN 于 nas）
- 基线回归 acceptance_v3.py 三模式 0 FAIL（断言按 0.14.0 语义更新：指数身份
  解析/板块源错误/裸 qfq 拒绝/X02、X04 移交扩展执行器；基线六文件与原 $defs
  零变更——S01 字节级校验）
- 离线校验器 `--report` 判定 INCOMPLETE：mandatory_not_passed=[I03, I07, H04]
  ——与本交付的真实状态一致（板块 BLOCKED + 两项无事件窗口），非打包缺陷。

## 上游事实（如实声明）

1. **腾讯 ifzq**：可达性随时间/网络位置漂移；验收中段三主机同遭
   waf.tencent.com 501page 封禁（开发机+NAS 同时，疑高频探测触发）。为此**新增
   新浪 getKLineData 第四提供者轮转**（commit 5a3757e），最终构建 nas 复跑由
   新浪续供，指数组全 PASS。**I08 独立性**：跨提供者对照证据 = live 轮（腾讯
   服务 vs 新浪参考，逐点差 ≤0.01@2dp，证据含 per-point diffs）；最终 nas 轮
   为同源（新浪-新浪）一致性检查，性质已在证据内标注，不冒充独立对照。
2. **东财 push2his**：本窗口全程不可达。板块实现完整（四主机轮转 + 正常/失败
   路径单测），源恢复后重验即可交付（复验命令 deployment/reproduction-commands.md）。
3. **引擎**：股票/ETF/复权数据正常（最新 20261023）。`复权` 表 cum 链乘语义 +
   SDK 消费公式（qfq=raw×f_cur/f_latest、hfq=raw×f_cur）以容器内
   /opt/stockdb/pybao/stock_sdk.py 源码 + 实测事件行双证据固定（A04/A06/A07）。
4. **pybao SDK 远程**（8.138.149.215:12328）：连接超时——引擎本地数据不受影响，
   但 SDK get_bars/get_price 类上游已死（本次不依赖）。

## 实现要点（source/diff-full.patch、CHANGELOG 0.14.0）

- `market_v3_research.py`（新增）：序列注册表（4 指数 + 7 BK 白名单 + 公开身份
  证据 URL）、多提供者轮转抓取（腾讯三主机→新浪；东财四主机）、
  FactorRequest/Response（请求级资产资格：指数/板块拒绝 UNSUPPORTED_ADJUSTMENT）、
  cum→affine-v1（multiplier=cum(≤date)/cum(≤anchor) 12 位量化、offset=0）、
  R/F/A 快照链（互传负例全拒）、持久化快照仓 /data/market_v3_snapshots
  （原子写；重启同 id 值不变）。
- `market_v3.py`：指数/板块身份路由（I02 同码不同资产隔离）、fq=qfq/hfq 分支、
  状态声明（index_board/adjusted_history 启用 + research_v1 元数据 + 板块源
  故障 degraded）。**amount 精度修正**：500 日窗口实测引擎存在小数成交额 →
  原「整数元」断言过紧致整段失败，放宽 ≤2 位小数并声明 amount_decimals=2。
- `get_adjust_factors` tools/list：双分支 schema（扩展合同 $defs 闭包原文嵌入；
  旧版分支仅匹配无 contract_version 的请求）。

## 部署与回退

- 镜像 stockdb-ai:0.14.0（本机构建，GIT_COMMIT=6fe4803），容器 healthy；
  §6.3 复验全绿（webui=0.14.0、health latest=20261023 lag=0）。
- 回退：src-backup/src-before-0.14.0-*.tar.gz + compose.yaml.bak-0.14.0 +
  本地镜像 0.13.2（详见 source/build-and-rollback.md）。
- ⚠️ 部署坑：Windows `git archive` 默认按 autocrlf 产出 CRLF → entrypoint
  shebang 带 \r → 容器 Restarting(255)；打包必须
  `git -c core.autocrlf=false archive`。

## 边界与未完成（不隐藏）

- 7 BK 板块 BLOCKED（上源不可达；单测覆盖正常路径，源恢复即可重验）。
- 指数量额 null（price-only）；依赖量额的用途属消费方按 J04 另判。
- I07/H04 的 NOT_RUN：窗口内无板块更名/真实停牌样本（断言要求事件驱动验证）。
- 消费方 J01—J08 NOT_RUN（consumer_workspace_required）；服务侧 PASS 不汇总为
  joint PASS。
