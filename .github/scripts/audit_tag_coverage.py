"""audit_tag_coverage — 发布 tag 覆盖审计（0.12.4 起，release-policy §3 检查单用）。

判据（与 release-policy §4「每版必打」对齐）：
  每个 CHANGELOG 里的版本条目，都应有一个同名 git tag `vX.Y.Z`。

为什么需要它（2026-10-20 实证）：
  09-19~10-17 连续 9 版未打 tag（另有两段更早的缺口，共 17 个版本），
  根因不是"忘了"——是 release-policy §3 检查单把「打 tag」写成一句
  checkbox，与「合并 PR」分属两个工具（浏览器 UI vs 本地终端），
  每次发版都要跨工具切换，跨 17 次全漏。
  本脚本把「有没有漏」从人工记忆变成一条命令。

用法：
  python .github/scripts/audit_tag_coverage.py          # 退出码 0 = 覆盖完整
  python .github/scripts/audit_tag_coverage.py --quiet  # 只在有缺口时输出

退出码：
  0 = 覆盖完整
  1 = 有版本缺 tag（真实缺口，CI 可据此判红）
  2 = 环境问题（浅克隆未 fetch tag / CHANGELOG 不可读）——**不是缺口**，
      修好环境再重跑，别把它当成失败误报。

CI 用法注意：actions/checkout 默认 fetch-depth: 1 **不拉 tag**，
必须用 fetch-depth: 0（或 fetch-tags: true），否则会拿到退出码 2。
"""

from __future__ import annotations

import re
import subprocess
import sys
from pathlib import Path

REPO = Path(__file__).resolve().parent.parent.parent

# CHANGELOG 版本条目：## [0.12.3] — 2026-10-20（...）
_VERSION_HEADING = re.compile(r"^## \[(\d+\.\d+\.\d+)\]", re.MULTILINE)
# tag 命名：v0.12.3（排除归档/特殊 tag，如 archive/xxx、测试版本0.3.1）
_TAG_NAME = re.compile(r"^v(\d+\.\d+\.\d+)$")


def _git(*args: str) -> str:
    out = subprocess.run(
        ["git", "-C", str(REPO), *args],
        capture_output=True,
        text=True,
        encoding="utf-8",
    )
    return out.stdout


def changelog_versions() -> list[str]:
    """CHANGELOG.md 里的全部版本号（按文件内出现顺序）。"""
    path = REPO / "CHANGELOG.md"
    if not path.exists():
        print("ERROR: CHANGELOG.md 不存在", file=sys.stderr)
        return []
    return _VERSION_HEADING.findall(path.read_text(encoding="utf-8"))


def git_tags() -> set[str]:
    """git tag 中形如 vX.Y.Z 的版本号集合（剥离 v 前缀）。"""
    out = _git("tag", "-l")
    return {m.group(1) for m in (_TAG_NAME.match(t.strip()) for t in out.splitlines()) if m}


def _repo_has_history() -> bool:
    """能否读到 CHANGELOG.md 的历史内容（浅克隆可能读不到）。"""
    return (REPO / "CHANGELOG.md").exists()


def _is_git_repo() -> bool:
    """当前目录是否是 git 仓库。"""
    return bool(_git("rev-parse", "--git-dir").strip())


def _tags_available() -> bool:
    """tag 是否已拉到本地。

    CI 的 actions/checkout 默认 fetch-depth: 1 —— **不拉 tag**，
    此时 `git tag -l` 为空，审计会把全部版本误报成缺口。
    故须先判断 tag 是否可用：不可用时明确报错（退出码 2 = 环境问题，
    与"真有缺口"（退出码 1）区分开）。
    """
    out = _git("tag", "-l")
    if out.strip():
        return True
    # 无 tag：可能是浅克隆未拉 tag，也可能真的一个 tag 都没有。
    # 用远端引用判断——有远端但本地无 tag = 未 fetch。
    remotes = _git("remote", "-v")
    if remotes.strip() and not out.strip():
        return False
    return True


def _stale_clone_check(version_count: int, tag_count: int) -> str | None:
    """检测「当前目录不是本仓库的最新克隆」——返回警告文案（无则 None）。

    背景（2026-10-20 实机）：NAS 上的 src/ 是一个 **2026-08-23 的陈旧克隆**
    （HEAD 停在 0.10.35 时期，tag 只有 7 个）——历次部署都是直出工作区，
    从未 fetch 过。此时在本目录跑审计会报「92 个版本缺 tag」，
    **那反映的是克隆陈旧，不是发布纪律缺口**。
    判据：版本条目数与 tag 数差得离谱（tag 覆盖不足一半）→ 判为陈旧克隆。
    """
    if version_count == 0 or tag_count == 0:
        return None
    if tag_count * 2 < version_count:
        return (
            f"当前目录疑似陈旧克隆：CHANGELOG 有 {version_count} 个版本，"
            f"但本地只有 {tag_count} 个 tag。\n"
            "  这多半不是发布纪律缺口，而是这个克隆没 fetch 过：\n"
            "    git fetch --tags origin && git fetch origin main\n"
            "  确认后再跑本脚本。"
        )
    return None


def version_sort_key(v: str) -> tuple[int, int, int]:
    return tuple(int(x) for x in v.split("."))  # type: ignore[return-value]


def main() -> int:
    quiet = "--quiet" in sys.argv

    versions = changelog_versions()
    if not versions:
        print("ERROR: 未从 CHANGELOG.md 解析出任何版本条目", file=sys.stderr)
        return 2

    if not _is_git_repo():
        msg = (
            "ERROR: 当前目录不是 git 仓库——无法审计 tag 覆盖。\n"
            "  本脚本只对本仓库的克隆有效（NAS 的 src/ 是构建用源树，不是仓库）。\n"
            "  要审计请在本仓库根目录运行。"
        )
        if not quiet:
            print(msg, file=sys.stderr)
        else:
            print("ERROR: not a git repository", file=sys.stderr)
        return 2

    if not _tags_available():
        msg = (
            "ERROR: 本地没有任何 git tag——多半是浅克隆未 fetch tag。\n"
            "  CI 中请用 actions/checkout@v4 并带 fetch-depth: 0（或 fetch-tags: true）。\n"
            "  本地可用 `git fetch --tags` 补齐后重跑。"
        )
        if not quiet:
            print(msg, file=sys.stderr)
        else:
            print("ERROR: no tags fetched (shallow clone?)", file=sys.stderr)
        return 2

    tags = git_tags()

    # CHANGELOG 可能有同版本多条（如 0.10.36~0.10.39 合并条目），去重保序
    ordered: list[str] = []
    for v in versions:
        if v not in ordered:
            ordered.append(v)

    stale_msg = _stale_clone_check(len(ordered), len(tags))
    if stale_msg:
        if not quiet:
            print(f"ERROR: {stale_msg}", file=sys.stderr)
        else:
            print("ERROR: stale clone (tags not fetched)", file=sys.stderr)
        return 2

    missing = sorted((v for v in ordered if v not in tags), key=version_sort_key)
    # 有 tag 但 CHANGELOG 无条目 —— 通常是历史遗留/归档，不算缺口，仅提示
    orphan = sorted((t for t in tags if t not in set(ordered)), key=version_sort_key)

    if missing:
        if not quiet:
            print(f"FAIL: {len(missing)} 个版本缺 tag（CHANGELOG 有条目但 git 无 tag）")
            for v in missing:
                print(f"  MISSING v{v}")
            print()
            print("处置（release-policy §3）：")
            print("  1. 找该版本 WEBUI_VERSION 首次出现的 commit（版本号单源 = config.py）：")
            print("     git log --format=%H -- stockdb-ai/config.py")
            print("     git show <sha>:stockdb-ai/config.py | grep WEBUI_VERSION")
            print("  2. 打 annotated tag 并推送：")
            print("     git tag -a vX.Y.Z -m '<版本要点>' <sha>")
            print("     git push origin vX.Y.Z")
            print("  注意：一个版本可能跨多个 PR（0.11.1/0.11.2 共用一个 commit），")
            print("        tag 目标 = 版本号首次出现的 commit，不是 PR 合并 commit。")
        else:
            for v in missing:
                print(f"MISSING v{v}")
        return 1

    if not quiet:
        print(f"OK: {len(ordered)} 个版本全部有对应 tag（缺 0）")
        if orphan:
            print(f"提示: {len(orphan)} 个 tag 无 CHANGELOG 条目（非发布物，通常是归档）")
            for t in orphan:
                print(f"  orphan v{t}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
