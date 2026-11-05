#!/usr/bin/env python3
"""astock.upstream_discovery — a-stock-data 上游双通道发现（M0，U01—U05）。

四类版本不可混（UPGRADE-RELEASE §1）：上游 tag/commit、生成 adapter 版本、
StockDB 服务版本、wire 版本。本模块只负责第 1 类的只读发现与观察记录：

- `git ls-remote` 同时取 refs/tags/* 与 refs/heads/main（一次网络往返）；
- annotated tag 用 `^{}` peel 行取 commit（同一 tag 出现两行：tag 对象 + peeled）；
- semver 排序取最新**正式** tag（忽略 v3.2 这类缺 patch 位的历史 tag 不等于忽略 main）；
- main 与上次观察 commit 不同 → unreleased_commit_changed（即使 declared_version 相同）；
- 同名 tag 对象/peeled 变化 → tag_moved（隔离，不当同版本更新）；
- 无 tag 的 main → unreleased 候选；同 commit 后来补 tag → 提升引用标签（tag_for_main），
  不重建候选；
- 网络失败保留 last_successful_observation，state=query_failed，绝不伪报 up_to_date；
- 观察记录幂等：相同（tag, main, tag_moved）组合的重复观察不产生新候选事件。

本模块不执行 git pull、不修改工作树、不触碰生产路由。
"""

from __future__ import annotations

import json
import re
import subprocess
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

DEFAULT_REMOTE = "https://github.com/simonlin1212/a-stock-data.git"
# 正式 semver tag：v主.次.补（可选前导 v）；v3.2（缺 patch）不参与"最新"排序
_SEMVER_RE = re.compile(r"^v?(\d+)\.(\d+)\.(\d+)$")
_LSREMOTE_TAG_RE = re.compile(r"^refs/tags/(.+?)(\^\{\})?$")


class DiscoveryError(Exception):
    """发现失败（网络/远端结构异常），调用方保留 last_successful_observation。"""


@dataclass
class TagRef:
    name: str
    object_sha: str  # tag 对象（annotated）或直接 commit（lightweight）
    peeled_commit: str | None  # annotated tag 的 ^{} 行；lightweight 为 None

    @property
    def commit(self) -> str:
        return self.peeled_commit or self.object_sha

    @property
    def is_annotated(self) -> bool:
        return self.peeled_commit is not None

    @property
    def is_official_semver(self) -> bool:
        return bool(_SEMVER_RE.match(self.name))


def _semver_key(name: str) -> tuple[int, int, int] | None:
    m = _SEMVER_RE.match(name)
    if not m:
        return None
    return (int(m.group(1)), int(m.group(2)), int(m.group(3)))


def ls_remote(remote: str = DEFAULT_REMOTE, timeout: float = 60.0) -> dict[str, str]:
    """git ls-remote（只读）；返回 {ref: sha}。失败抛 DiscoveryError。"""
    try:
        proc = subprocess.run(
            ["git", "ls-remote", remote],
            capture_output=True, text=True, timeout=timeout)
    except (subprocess.TimeoutExpired, FileNotFoundError, OSError) as exc:
        raise DiscoveryError(f"ls-remote failed: {type(exc).__name__}: {exc}") from exc
    if proc.returncode != 0:
        raise DiscoveryError(f"ls-remote exit {proc.returncode}: "
                             f"{proc.stderr.strip()[:200]}")
    refs: dict[str, str] = {}
    for line in proc.stdout.splitlines():
        sha, _, ref = line.partition("\t")
        if sha and ref:
            refs[ref.strip()] = sha.strip()
    if "HEAD" not in refs and not any(r.startswith("refs/") for r in refs):
        raise DiscoveryError("ls-remote returned no refs")
    return refs


def parse_tags(refs: dict[str, str]) -> list[TagRef]:
    """refs/tags/* → TagRef 列表（peel 行归并到同名 tag）。"""
    tags: dict[str, TagRef] = {}
    for ref, sha in refs.items():
        m = _LSREMOTE_TAG_RE.match(ref)
        if not m:
            continue
        name, peeled = m.group(1), m.group(2)
        if peeled:  # ^{} 行：annotated tag 的 peeled commit
            entry = tags.setdefault(name, TagRef(name, sha, None))
            if entry.peeled_commit not in (None, sha):
                raise DiscoveryError(
                    f"tag {name} has multiple peeled commits "
                    f"({entry.peeled_commit} vs {sha})")
            entry.peeled_commit = sha
        else:
            entry = tags.setdefault(name, TagRef(name, sha, None))
            entry.object_sha = sha
    return sorted(tags.values(), key=lambda t: t.name)


def latest_official_tag(tags: list[TagRef]) -> TagRef | None:
    """semver 最新的正式 tag；非正式 tag 不参与但不丢弃（仍在 tags 列表）。"""
    scored = [( _semver_key(t.name), t) for t in tags]
    scored = [(k, t) for k, t in scored if k is not None]
    if not scored:
        return None
    scored.sort(key=lambda kt: kt[0])
    return scored[-1][1]


@dataclass
class Observation:
    """一次发现的完整观察记录（append-only 保存，不覆盖历史）。"""
    observed_at: str
    remote_url: str
    latest_tag: dict[str, Any] | None
    main: dict[str, Any]
    status: str  # up_to_date | unreleased_commit_changed | new_tag | tag_moved | query_failed
    reason: str
    previous: dict[str, Any] | None = None
    error: str | None = None
    all_tags: list[dict[str, Any]] = field(default_factory=list)

    def to_json(self) -> dict[str, Any]:
        out: dict[str, Any] = {
            "schema_version": 1,
            "remote_url": self.remote_url,
            "observed_at": self.observed_at,
            "latest_tag": self.latest_tag,
            "main": self.main,
            "status": self.status,
            "reason": self.reason,
            "all_tags": self.all_tags,
        }
        if self.previous is not None:
            out["previous"] = self.previous
        if self.error is not None:
            out["error"] = self.error
        return out


def discover(remote: str = DEFAULT_REMOTE, *,
             previous: dict[str, Any] | None = None,
             now: float | None = None) -> Observation:
    """一次完整发现。previous=上次成功观察的 to_json()（可 None=首次）。

    判定优先级（UPGRADE-RELEASE §2）：
      1. 网络/结构失败 → query_failed（保留 previous 为 last_successful）；
      2. tag 名存在但 object_sha/peeled 与上次不同 → tag_moved；
      3. main commit 与上次不同 → unreleased_commit_changed（tag_for_main 记录
         main 是否已有 tag，同 commit 补 tag 属于提升引用而非新内容）；
      4. 最新官方 tag 的 commit 与上次不同 → new_tag；
      5. 其余 → up_to_date。
    """
    import datetime
    now_ts = now if now is not None else time.time()
    observed_at = datetime.datetime.fromtimestamp(
        now_ts).astimezone().isoformat(timespec="seconds")
    try:
        refs = ls_remote(remote)
        tags = parse_tags(refs)
        latest = latest_official_tag(tags)
        main_commit = refs.get("refs/heads/main")
        if not main_commit:
            raise DiscoveryError("remote has no refs/heads/main")
        main_declared_version = None  # 由调用方按需从快照/文档核验，发现层不猜
    except DiscoveryError as exc:
        return Observation(
            observed_at=observed_at, remote_url=remote, latest_tag=None,
            main={"commit": None, "declared_version": None,
                  "tag_for_main": None},
            status="query_failed",
            reason=f"discovery failed: {exc}",
            previous=previous, error=str(exc)[:400],
            all_tags=[])

    tag_payload = None
    if latest is not None:
        tag_payload = {"name": latest.name,
                       "object_sha": latest.object_sha,
                       "peeled_commit": latest.commit,
                       "annotated": latest.is_annotated}
    main_payload = {"commit": main_commit, "declared_version": None,
                    "tag_for_main": _tag_for_commit(tags, main_commit)}

    # 状态判定（需要 previous 时）
    status, reason = "up_to_date", "no change in latest official tag or main"
    prev_main = (previous or {}).get("main") or {}
    prev_tag = (previous or {}).get("latest_tag")
    prev_tag_by_name = {t["name"]: t for t in ((previous or {}).get("all_tags") or [])}

    for t in tags:
        prev_t = prev_tag_by_name.get(t.name)
        if prev_t and (prev_t.get("object_sha") != t.object_sha
                       or prev_t.get("peeled_commit") != t.peeled_commit):
            status, reason = "tag_moved", (
                f"tag {t.name} changed object/peeled commit "
                f"({prev_t.get('object_sha')}->{t.object_sha}); isolated, "
                "not treated as same-version update")
            break

    if status != "tag_moved":
        if prev_main.get("commit") and prev_main["commit"] != main_commit:
            status = "unreleased_commit_changed"
            reason = ("main changed; " + (
                f"no tag for main ({main_payload['tag_for_main']} absent)"
                if main_payload["tag_for_main"] is None else
                f"main now tagged as {main_payload['tag_for_main']}"))
        elif (latest is not None
              and (prev_tag or {}).get("peeled_commit") is not None
              and prev_tag["peeled_commit"] != latest.commit):
            status, reason = "new_tag", (
                f"latest official tag {latest.name} -> {latest.commit}")
        elif latest is not None and prev_tag is None:
            status, reason = "new_tag", f"first observation: {latest.name}"

    return Observation(
        observed_at=observed_at, remote_url=remote, latest_tag=tag_payload,
        main=main_payload, status=status, reason=reason,
        previous=previous, all_tags=[
            {"name": t.name, "object_sha": t.object_sha,
             "peeled_commit": t.commit, "annotated": t.is_annotated,
             "official_semver": t.is_official_semver}
            for t in tags])


def _tag_for_commit(tags: list[TagRef], commit: str) -> str | None:
    for t in tags:
        if t.commit == commit:
            return t.name
    return None


def classify_candidate(observation: Observation) -> dict[str, Any]:
    """观察 → 候选判定（幂等）：候选 ID 不含观察时间（UPGRADE-RELEASE §3）。"""
    tag = observation.latest_tag
    main = observation.main
    tag_for_main = main.get("tag_for_main")
    if tag_for_main and tag_for_main == (tag or {}).get("name"):
        pinned = {"kind": "tag", "name": tag["name"],
                  "commit": tag["peeled_commit"]}
    elif tag_for_main:
        pinned = {"kind": "tag", "name": tag_for_main,
                  "commit": main["commit"]}
    else:
        pinned = {"kind": "unreleased_main", "name": None,
                  "commit": main["commit"]}
    return {
        "pin": pinned,
        "candidate_id_basis": {
            "upstream_commit": pinned["commit"],
            "upstream_tag": pinned["name"],
        },
        "note": ("candidate_id also binds extraction-rule hash / patch hash / "
                 "dependency-lock hash / build params / capability set / wire "
                 "version at build time; discovery alone never mints a full ID"),
    }
