# StockDB Agent 协作与维护纪律 (AGENTS.md)

本文件定义任何 AI Agent 在本仓库（`stockdb-private` / `stockdb-ai`）作业时的核心红线、架构边界与标准流程。

---

## 一、 安全红线 (Standing Red Lines)

1. **未经明确授权的三不原则**：
   - 未获本轮用户明确授权，**绝不私自重启容器、不部署上线、不修改生产卷、不切换路由、不自动 git commit 或 git push**。
2. **敏感信息绝不入库**：
   - 严禁包含任何命理八字、用户个人账户、交易持仓、私钥凭据（token/password）或消费方私有修订草稿。
   - 所有原始采集数据必须执行脱敏过滤（`.raw_provenance`）。

---

## 二、 架构与数据契约边界

1. **核心契约冻结**：
   - `stockdb-market-v3`（3.0.0）与 `research-extension`（1.0.0）核心 schema 不得擅改。
   - 所有扩展字段严格限制在合法扩展载荷（`"extensions": {"type": "object"}`）内呈现。
2. **快照绝对不可变性 (Snapshot Immutability)**：
   - 历史快照（Snapshot）具有不可篡改性，重放必须 100% 还原当时冻结观测值；
   - 盘后终结对齐（Reconciliation）与审计修订仅更新当前权威缓存（Current），不得污染历史快照。
3. **角色与身份严格隔离**：
   - `885809` 作为正式基准折叠屏板块角色保持不变，其余 6 条 THS 板块维持参考角色；
   - 东财 BK 板块上游版本号及方法论版本无官方字段时如实填 `null`，严禁编造，严禁以 THS 板块冒充替代。

---

## 三、 分支合并与清理标准流程 (Branch Cleanup SOP)

当用户明确发出 **“合并到main 并清理多余分支”** 或类似指令时，Agent **必须严格执行以下标准流程**：

### 1. 执行准则
- 必须使用非快进合并（`--no-ff`）保留清晰版本里程碑；
- 必须将交付归档物（如候选 zip）纳入 `deliveries/` 并更新 `deliveries/README.md`；
- 必须清理本地已合入的特性分支（`git branch -D`）；
- 必须清理远程对应的旧分支（`git push origin --delete`）；
- 鉴于远程 `main` 分支启用了规则保护（GH013: 必须通过 PR 合并），Agent 应通过自动 PR 流程合入 `main` 并修剪远端过渡分支；
- 最终必须执行 `git pull origin main` 与 `git fetch origin --prune`，确保本地 `main` 与远程 `origin/main` 100% 同步且工作区仅剩 `main`。

### 2. 标准自动化工具
仓库已内置标准作业脚本，Agent 可直接调用：
```bash
python scripts/merge_to_main_and_cleanup.py --branch <branch_name> -t "<commit/pr title>"
```
完整 SOP 详见：[`docs/ops/branch-cleanup-and-merge-workflow.md`](./docs/ops/branch-cleanup-and-merge-workflow.md)。
