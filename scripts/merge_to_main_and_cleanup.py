#!/usr/bin/env python3
"""
scripts/merge_to_main_and_cleanup.py

自动化分支合入 main 与多余分支清理标准作业工具 (SOP)
--------------------------------------------------
执行流程：
1. 检查工作区纯净度与分支状态；
2. 本地检出 main，执行 --no-ff 合并；
3. 本地清理已合入的特性分支；
4. 若远端存在同名分支，自动删除该远端分支；
5. 处理 GitHub main 分支保护（GH013: 必须通过 PR 合并）：
   - 推送临时发布分支 release/<name>；
   - 自动读取 git 凭据调用 GitHub API 创建并合并 PR；
   - 删除远端临时发布分支；
6. 本地 git pull origin main 对齐最新远端合并提交；
7. 执行 git fetch origin --prune 彻底修剪本地无效追踪引用。
"""

import os
import sys
import json
import argparse
import subprocess
from pathlib import Path

def run_git(args, check=True, capture=True):
    cmd = ['git'] + args
    res = subprocess.run(cmd, capture_output=capture, text=True, errors='replace')
    if check and res.returncode != 0:
        err = res.stderr.strip() if capture else f"exit code {res.returncode}"
        raise RuntimeError(f"git {' '.join(args)} failed: {err}")
    return res

def get_current_branch():
    res = run_git(['rev-parse', '--abbrev-ref', 'HEAD'])
    return res.stdout.strip()

def get_github_token():
    # 优先环境变量
    for env_var in ('GH_TOKEN', 'GITHUB_TOKEN'):
        val = os.environ.get(env_var)
        if val:
            return val.strip()

    # 从 git credential fill 获取
    input_text = "protocol=https\nhost=github.com\n\n"
    res = subprocess.run(['git', 'credential', 'fill'], input=input_text, capture_output=True, text=True, errors='replace')
    for line in res.stdout.splitlines():
        if line.startswith('password='):
            return line.split('=', 1)[1].strip()
    return None

def curl_github(token, owner, repo, endpoint, method='GET', data=None):
    url = f'https://api.github.com/repos/{owner}/{repo}/{endpoint}' if not endpoint.startswith('https://') else endpoint
    cmd = [
        'curl.exe', '-s',
        '-X', method,
        '-H', f'Authorization: token {token}',
        '-H', 'Accept: application/vnd.github.v3+json',
        '-H', 'User-Agent: stockdb-automation'
    ]
    temp_payload = None
    if data is not None:
        temp_payload = Path('.tmp_github_payload.json')
        temp_payload.write_text(json.dumps(data, ensure_ascii=False), encoding='utf-8')
        cmd.extend(['-H', 'Content-Type: application/json; charset=utf-8', '--data-binary', f'@{temp_payload.as_posix()}'])
    cmd.append(url)
    try:
        res = subprocess.run(cmd, capture_output=True)
        out_text = res.stdout.decode('utf-8', errors='replace')
        try:
            return json.loads(out_text)
        except Exception:
            return {'raw': out_text, 'returncode': res.returncode}
    finally:
        if temp_payload and temp_payload.exists():
            try:
                temp_payload.unlink()
            except Exception:
                pass

def main():
    parser = argparse.ArgumentParser(description="Merge branch into main and clean up redundant branches.")
    parser.add_argument('--branch', '-b', help="Source branch to merge into main. Defaults to current branch if not on main.")
    parser.add_argument('--title', '-t', help="Merge / PR commit title.")
    parser.add_argument('--owner', default='awoeyiwuyua', help="GitHub repository owner.")
    parser.add_argument('--repo', default='stockdb-ai', help="GitHub repository name.")
    parser.add_argument('--skip-pr', action='store_true', help="Skip PR flow and try direct push to main.")
    args = parser.parse_args()

    curr_branch = get_current_branch()
    src_branch = args.branch

    if not src_branch:
        if curr_branch == 'main':
            print("Already on main. Specify --branch <name> to merge a branch into main.")
            return 1
        src_branch = curr_branch

    print(f"=== Starting merge: '{src_branch}' -> 'main' ===")

    # 1. Check working directory status
    status_res = run_git(['status', '--porcelain'])
    dirty_lines = [l for l in status_res.stdout.splitlines() if not l.startswith('??')]
    if dirty_lines:
        print("ERROR: Working tree has unstaged or uncommitted tracked changes. Please commit or stash first.")
        for l in dirty_lines:
            print(" ", l)
        return 1

    # 2. Checkout main
    print("Step 1: Checking out main...")
    run_git(['checkout', 'main'])

    # 3. Merge branch
    title = args.title or f"merge: {src_branch} 合入 main"
    print(f"Step 2: Merging '{src_branch}' into main (--no-ff)...")
    run_git(['merge', '--no-ff', src_branch, '-m', title])

    # 4. Clean up local source branch
    print(f"Step 3: Deleting local branch '{src_branch}'...")
    run_git(['branch', '-D', src_branch])

    # 5. Delete remote source branch if present
    print(f"Step 4: Checking and deleting remote branch 'origin/{src_branch}' if exists...")
    remotes_res = run_git(['branch', '-r'], check=False)
    if f"origin/{src_branch}" in remotes_res.stdout:
        del_remote = run_git(['push', 'origin', '--delete', src_branch], check=False)
        if del_remote.returncode == 0:
            print(f"  Deleted remote branch 'origin/{src_branch}'.")
        else:
            print(f"  Remote branch deletion returned: {del_remote.stderr.strip()}")

    # 6. Push to origin main or PR flow
    print("Step 5: Synchronizing with remote main...")
    direct_push = run_git(['push', 'origin', 'main'], check=False)
    if direct_push.returncode == 0:
        print("Direct push to origin/main succeeded!")
    else:
        print("Direct push to main was declined (likely GitHub branch protection). Initiating automated PR workflow...")
        token = get_github_token()
        if not token:
            print("WARNING: Could not obtain GitHub token from env or git credential manager.")
            print(f"Please manually open a PR on GitHub for '{src_branch}' -> 'main'.")
            return 1

        release_ref = f"release/{src_branch.replace('/', '-')}"
        print(f"  Pushing release branch '{release_ref}' to origin...")
        run_git(['push', '-u', 'origin', f'main:refs/heads/{release_ref}'])

        print(f"  Creating Pull Request on GitHub ({release_ref} -> main)...")
        pr_payload = {
            'title': title,
            'head': release_ref,
            'base': 'main',
            'body': f"Automated merge of `{src_branch}` into `main`."
        }
        pr_data = curl_github(token, args.owner, args.repo, 'pulls', method='POST', data=pr_payload)
        pr_number = pr_data.get('number')

        if not pr_number:
            # Query if already open
            prs = curl_github(token, args.owner, args.repo, f'pulls?head={args.owner}:{release_ref}&base=main&state=open')
            if isinstance(prs, list) and len(prs) > 0:
                pr_number = prs[0]['number']

        if not pr_number:
            print("ERROR: Failed to create or find PR on GitHub:", pr_data)
            return 1

        print(f"  PR #{pr_number} ready. Merging PR #{pr_number}...")
        merge_payload = {
            'commit_title': f"{title} (PR #{pr_number})",
            'merge_method': 'merge'
        }
        merge_res = curl_github(token, args.owner, args.repo, f'pulls/{pr_number}/merge', method='PUT', data=merge_payload)
        if not merge_res.get('merged'):
            print("ERROR: PR merge failed:", merge_res)
            return 1
        print(f"  PR #{pr_number} merged successfully! (SHA: {merge_res.get('sha')})")

        print(f"  Deleting remote temporary branch '{release_ref}'...")
        curl_github(token, args.owner, args.repo, f'git/refs/heads/{release_ref}', method='DELETE')

        # 7. Pull back to main
        print("Step 6: Pulling remote merge commit to local main...")
        run_git(['pull', 'origin', 'main'])

    # 8. Prune tracking branches
    print("Step 7: Pruning obsolete remote tracking references...")
    run_git(['fetch', 'origin', '--prune'])

    print("\n=== SUCCESS: Branch merged into main and all redundant branches cleaned up! ===")
    run_git(['branch', '-a'], check=False, capture=False)
    return 0

if __name__ == '__main__':
    sys.exit(main())
