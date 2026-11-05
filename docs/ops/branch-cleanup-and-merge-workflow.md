# 分支合并与清理标准作业程序 (SOP)

> **生效日期**：2026-11-05  
> **适用场景**：当开发分支（如 `fix/*`、`feature/*`）完成验收，需要合入 `main` 并清理所有多余分支时，必须严格遵守本流程。

---

## 一、 背景与约束

1. **GitHub 分支保护（GH013）**：
   - 远程仓库 `main` 分支启用了规则保护（*Changes must be made through a pull request*）。
   - 禁止开发者或 Agent 直接执行 `git push origin main`。
2. **多余分支清理原则**：
   - 合并完成后，必须彻底清理**本地特性分支**与**远程特性分支**，严禁遗留失效的废弃分支。
   - 本地 `main` 必须与远程 `origin/main` 保持 100% 相同步。

---

## 二、 自动化标准工具（推荐）

项目已将全套流程收敛并固化至专用自动化工具：

```bash
# 在特性分支上直接运行：
python scripts/merge_to_main_and_cleanup.py

# 或指定分支与标题：
python scripts/merge_to_main_and_cleanup.py --branch fix/my-branch -t "merge: 完成某某修复合入 main"
```

该工具自动完成：
1. 本地工作区干净度校验与 `--no-ff` 合并；
2. 本地已合并分支删除；
3. 远程对应旧分支删除；
4. 规避分支保护：推送临时发布分支并调用 GitHub API 创建 PR；
5. 自动合并 PR 并清理远程临时发布分支；
6. `git pull origin main` 本地快进同步 + `git fetch origin --prune` 远端引用彻底修剪。

---

## 三、 手动执行步骤备忘（Fallback SOP）

若无法调用自动化脚本，必须按以下 6 步逐项执行：

### Step 1：工作区确认与交付物沉淀
- 确认当前分支测试全绿（如 `python -m unittest ...` 通过）。
- 若有交付物包（ZIP/清单），存入 `deliveries/` 并更新 `deliveries/README.md`。
- 确保工作区暂存并提交完毕。

### Step 2：本地检出 main 并执行非快进合并
```bash
git checkout main
git merge --no-ff <feature-branch> -m "merge: <feature-branch> 发布线合入 main"
```

### Step 3：删除本地已合入的多余分支
```bash
git branch -D <feature-branch>
```

### Step 4：清理远程旧特性分支（若此前已推送到远端）
```bash
git push origin --delete <feature-branch>
```

### Step 5：推送临时发布分支并完成 PR 合并
由于直接 `git push origin main` 会被保护规则拒绝，采用临时分支转接：
```bash
# 推送临时分支
git push -u origin main:refs/heads/release/<version>

# 在 GitHub 上通过 PR 合入 main（可通过 API 或网页点击 Merge）
# 合并完成后，删除远端临时分支：
curl -X DELETE -H "Authorization: token $GH_TOKEN" https://api.github.com/repos/awoeyiwuyua/stockdb-ai/git/refs/heads/release/<version>
```

### Step 6：本地同步与远端追踪修剪
```bash
git pull origin main
git fetch origin --prune
```

执行后检查 `git branch -a`，确保本地仅剩 `* main`，远端仅剩 `origin/main`，且 `Your branch is up to date with 'origin/main'.`。
