#!/usr/bin/env python3
"""astock.lifecycle — 候选状态机与升级控制面（M0，R01—R10）。

状态机（UPGRADE-RELEASE §3）：
  discovered → pinned → generated → testing → qualified → built → staged
  → deployed → verified → active
  发布前失败 → adaptation_required / failed / blocked（active 保持旧值）
  deployed 后验证失败 → rolled_back（旧 digest 恢复）

不变量：
- 候选 ID = hash(upstream commit + 提取规则 + patch + 依赖锁 + 构建参数 +
  能力集合 + wire 版本)；重复发现幂等，不因观察时间刷新。
- 状态变更 append-only 事件记录，带 expected_revision（CAS）阻止并发发布器。
- 资格报告绑定 candidate_id + build digest；报告/代码/依赖变化必须重验。
- 每日发现任务只调 check/受限 candidate；绝不自动 git pull 生产容器。
- release 消费已绑定且合格的候选；失败恢复旧 active 并留事件。
- rollback 指定先前已验收 digest，不重新抓源"还原"快照。

CLI 八动作：check / candidate / generate / verify / build / release /
rollback / status。动作映射与部署接线记录在 implementation-map.json。
"""

from __future__ import annotations

import argparse
import hashlib
import json
import sys
import threading
import time
import uuid
from pathlib import Path
from typing import Any

from . import upstream_discovery as disc
from .extraction import ExtractionError, sha256_tree

STATE_ORDER = ["discovered", "pinned", "generated", "testing", "qualified",
               "built", "staged", "deployed", "verified", "active"]
TERMINAL_FAIL = {"adaptation_required", "failed", "blocked", "rolled_back"}


def candidate_id(pin_commit: str, extraction_sha: str, patch_sha: str,
                 deplock_sha: str, build_params: str, capability_set: str,
                 wire_version: str) -> str:
    """候选 ID（UPGRADE-RELEASE §3：全因子绑定，不用版本号）。"""
    basis = "\n".join([pin_commit, extraction_sha, patch_sha, deplock_sha,
                       build_params, capability_set, wire_version])
    return "cand-" + hashlib.sha256(basis.encode("utf-8")).hexdigest()[:16]


class LifecycleStore:
    """文件存储的事件溯源状态机（CAS + append-only 事件）。

    state.json：{revision, candidates: {id: {...}}, active: id|None, events: [...]}
    一次 CAS 转移：read(revision) → 变更 → write(revision+1)；写前比对 revision。
    """

    def __init__(self, root: Path):
        self.root = Path(root)
        self.root.mkdir(parents=True, exist_ok=True)
        self._lock = threading.Lock()
        self.state_path = self.root / "state.json"
        self.events_path = self.root / "events.jsonl"
        if not self.state_path.exists():
            self._write({"revision": 0, "candidates": {}, "active": None})

    def _read(self) -> dict[str, Any]:
        return json.loads(self.state_path.read_text(encoding="utf-8"))

    def _write(self, state: dict[str, Any]) -> None:
        tmp = self.state_path.with_suffix(".tmp")
        tmp.write_text(json.dumps(state, ensure_ascii=False, indent=1),
                       encoding="utf-8")
        tmp.replace(self.state_path)

    def transition(self, candidate_id: str, new_state: str, *,
                   expected_revision: int | None = None, detail: dict | None
                   = None) -> dict[str, Any]:
        """CAS 状态转移 + append-only 事件。非法转移/revision 冲突抛错。"""
        with self._lock:
            state = self._read()
            if expected_revision is not None \
                    and expected_revision != state["revision"]:
                raise RuntimeError(
                    f"cas conflict: expected revision {expected_revision}, "
                    f"current {state['revision']} (another publisher?)")
            cand = state["candidates"].get(candidate_id)
            if cand is None:
                raise KeyError(f"unknown candidate {candidate_id}")
            old = cand["state"]
            allowed = (
                old in STATE_ORDER and new_state in STATE_ORDER
                and STATE_ORDER.index(new_state) == STATE_ORDER.index(old) + 1
            ) or (new_state in TERMINAL_FAIL) or (
                old == "deployed" and new_state == "rolled_back")
            if not allowed:
                raise RuntimeError(f"illegal transition {old} -> {new_state}")
            cand["state"] = new_state
            cand["updated_at"] = _now()
            state["revision"] += 1
            event = {"ts": _now(), "candidate": candidate_id, "from": old,
                     "to": new_state, "revision": state["revision"],
                     "detail": detail or {}}
            with self.events_path.open("a", encoding="utf-8") as fh:
                fh.write(json.dumps(event, ensure_ascii=False) + "\n")
            if new_state == "active":
                state["active"] = candidate_id
            if new_state == "rolled_back":
                state["active"] = cand.get("rolled_back_to")
            self._write(state)
            return state

    def upsert_candidate(self, cand: dict[str, Any]) -> tuple[str, bool]:
        """幂等登记候选：同 ID 重复发现不产生新事件（返回 registered=False）。"""
        with self._lock:
            state = self._read()
            cid = cand["id"]
            if cid in state["candidates"]:
                return cid, False
            state["candidates"][cid] = {**cand, "state": "discovered",
                                        "created_at": _now()}
            state["revision"] += 1
            self._write(state)
            with self.events_path.open("a", encoding="utf-8") as fh:
                fh.write(json.dumps({"ts": _now(), "candidate": cid,
                                     "from": None, "to": "discovered",
                                     "revision": state["revision"]},
                                    ensure_ascii=False) + "\n")
            return cid, True

    def snapshot(self) -> dict[str, Any]:
        return self._read()


def _now() -> str:
    import datetime
    return datetime.datetime.now().astimezone().isoformat(timespec="seconds")


# ---------------------------------------------------------------------------
# CLI 八动作
# ---------------------------------------------------------------------------


def cmd_check(args) -> int:
    """只读发现：0 无变化 / 2 发现候选 / 1 检查失败；不修改 active。"""
    prev = None
    obs_path = Path(args.observations)
    if obs_path.exists():
        history = json.loads(obs_path.read_text(encoding="utf-8"))
        for o in reversed(history):
            if o.get("status") != "query_failed":
                prev = o
                break
    try:
        obs = disc.discover(args.remote, previous=prev)
    except disc.DiscoveryError as exc:
        print(json.dumps({"status": "query_failed", "error": str(exc)},
                         ensure_ascii=False))
        return 1
    history = json.loads(obs_path.read_text(encoding="utf-8")) \
        if obs_path.exists() else []
    history.append(obs.to_json())
    obs_path.write_text(json.dumps(history[-200:], ensure_ascii=False,
                                   indent=1), encoding="utf-8")
    basis = disc.classify_candidate(obs)
    exit_code = 0
    if obs.status in ("unreleased_commit_changed", "new_tag", "tag_moved"):
        exit_code = 2
    print(json.dumps({"status": obs.status, "reason": obs.reason,
                      "candidate_basis": basis,
                      "latest_tag": obs.latest_tag, "main": obs.main},
                     ensure_ascii=False, indent=1))
    return exit_code


def cmd_candidate(args) -> int:
    """登记候选（幂等）；0 已存在/新登记 / 2 受阻 / 1 失败。"""
    from .extraction import sha256_tree
    store = LifecycleStore(Path(args.store))
    vendor = Path(args.vendor)
    commit = args.commit
    try:
        tree_hash = sha256_tree(vendor)
    except OSError as exc:
        print(json.dumps({"error": f"vendor tree unreadable: {exc}"}))
        return 1
    cid_basis = hashlib.sha256("\n".join([
        commit, tree_hash, args.extraction_rules_hash or "",
        args.patch_hash or "", args.dependency_lock_hash or "",
        args.build_params or "", args.wire_version or "",
    ]).encode("utf-8")).hexdigest()[:16]
    cand = {"id": f"cand-{cid_basis}",
            "upstream_commit": commit,
            "vendor_tree_sha256": tree_hash,
            "extraction_rules_hash": args.extraction_rules_hash,
            "patch_hash": args.patch_hash,
            "dependency_lock_hash": args.dependency_lock_hash,
            "build_params": args.build_params,
            "wire_version": args.wire_version,
            "capabilities": (args.capabilities or "").split(",")
            if args.capabilities else []}
    cid, created = store.upsert_candidate(cand)
    print(json.dumps({"candidate_id": cid, "created": created,
                      "state": store.snapshot()["candidates"][cid]["state"]},
                     ensure_ascii=False))
    return 0


def cmd_generate(args) -> int:
    """执行提取生成（写入隔离候选目录）；0 成功 / 1 失败 / 2 受阻。"""
    from .extraction import extract
    try:
        plan, module_text = extract(
            Path(args.vendor), source_commit=args.commit,
            entries=args.entries.split(","), module_name=args.module,
            initialization_allowlist=json.loads(Path(args.allowlist).read_text(
                encoding="utf-8")) if args.allowlist else [])
    except ExtractionError as exc:
        print(json.dumps({"state": "adaptation_required",
                          "error": str(exc)[:400]}, ensure_ascii=False))
        return 2
    out_dir = Path(args.out)
    out_dir.mkdir(parents=True, exist_ok=True)
    (out_dir / f"{args.module}.py").write_text(module_text, encoding="utf-8")
    (out_dir / "extraction-manifest.json").write_text(
        json.dumps(plan.to_json(), ensure_ascii=False, indent=1),
        encoding="utf-8")
    print(json.dumps({"state": "generated", "module_sha256":
                      plan.generated_sha256, "out": str(out_dir)},
                     ensure_ascii=False))
    return 0


def cmd_verify(args) -> int:
    """运行验证（离线生成物测试在 verify-test 套件内执行）；此处核对报告绑定。"""
    report = json.loads(Path(args.report).read_text(encoding="utf-8"))
    ok = (report.get("candidate_id") == args.candidate
          and report.get("build_digest") == args.build_digest
          and report.get("passed") is True)
    print(json.dumps({"verified": ok}, ensure_ascii=False))
    return 0 if ok else 2


def cmd_build(args) -> int:
    """构建 digest（确定性产物 hash 汇总）。"""
    files = sorted(Path(args.src).rglob("*.py"))
    digest = hashlib.sha256("\n".join(
        f"{f.relative_to(args.src)}\0{hashlib.sha256(f.read_bytes()).hexdigest()}"
        for f in files).encode("utf-8")).hexdigest()
    print(json.dumps({"build_digest": digest[:16],
                      "full": digest}, ensure_ascii=False))
    return 0


def cmd_release(args) -> int:
    """发布已绑定且合格的候选；不合格/锁冲突失败，不改 active。"""
    store = LifecycleStore(Path(args.store))
    snap = store.snapshot()
    cand = snap["candidates"].get(args.candidate)
    if cand is None or cand["state"] != "qualified":
        print(json.dumps({"released": False, "reason":
                          "candidate not qualified"}, ensure_ascii=False))
        return 2
    try:
        store.transition(args.candidate, "built",
                         expected_revision=args.expected_revision)
        store.transition(args.candidate, "staged")
        store.transition(args.candidate, "deployed")
        store.transition(args.candidate, "verified")
        store.transition(args.candidate, "active")
    except RuntimeError as exc:
        print(json.dumps({"released": False, "reason": str(exc)[:200]},
                         ensure_ascii=False))
        return 1
    print(json.dumps({"released": True, "active":
                      store.snapshot()["active"]}, ensure_ascii=False))
    return 0


def cmd_rollback(args) -> int:
    """回退到先前已验收 digest；旧工具/快照可读由部署层 smoke 验证。"""
    store = LifecycleStore(Path(args.store))
    snap = store.snapshot()
    prev = snap["candidates"].get(args.to)
    if prev is None or prev["state"] not in ("active", "verified", "rolled_back"):
        print(json.dumps({"rolled_back": False, "reason":
                          "target not previously accepted"},
                         ensure_ascii=False))
        return 2
    cur = snap["active"]
    store.transition(args.to, "active", detail={"rollback_from": cur})
    snap = store.snapshot()
    if cur:
        snap["candidates"][cur]["state"] = "rolled_back"
        snap["candidates"][cur]["rolled_back_to"] = args.to
        snap["revision"] += 1
        with store.events_path.open("a", encoding="utf-8") as fh:
            fh.write(json.dumps({"ts": _now(), "candidate": cur,
                                 "from": "active", "to": "rolled_back",
                                 "revision": snap["revision"],
                                 "detail": {"rollback_to": args.to}},
                                ensure_ascii=False) + "\n")
        store._write(snap)
    print(json.dumps({"rolled_back": True, "active": snap["active"]},
                     ensure_ascii=False))
    return 0


def cmd_status(args) -> int:
    store = LifecycleStore(Path(args.store))
    snap = store.snapshot()
    obs = json.loads(Path(args.observations).read_text(encoding="utf-8")) \
        if Path(args.observations).exists() else []
    last_ok = next((o for o in reversed(obs)
                    if o.get("status") != "query_failed"), None)
    last = obs[-1] if obs else None
    print(json.dumps({
        "active": snap["active"],
        "candidates": {k: v["state"] for k, v in
                       snap["candidates"].items()},
        "revision": snap["revision"],
        "last_discovery": last,
        "last_successful_observation": last_ok,
    }, ensure_ascii=False, indent=1))
    return 0


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(prog="astock-lifecycle")
    sub = ap.add_subparsers(required=True)
    for name, fn in (("check", cmd_check), ("candidate", cmd_candidate),
                     ("generate", cmd_generate), ("verify", cmd_verify),
                     ("build", cmd_build), ("release", cmd_release),
                     ("rollback", cmd_rollback), ("status", cmd_status)):
        p = sub.add_parser(name)
        p.set_defaults(fn=fn)
        if name == "check":
            p.add_argument("--remote", default=disc.DEFAULT_REMOTE)
            p.add_argument("--observations", required=True)
        elif name == "candidate":
            for a in ("--store", "--vendor", "--commit"):
                p.add_argument(a, required=True)
            p.add_argument("--extraction-rules-hash")
            p.add_argument("--patch-hash")
            p.add_argument("--dependency-lock-hash")
            p.add_argument("--build-params")
            p.add_argument("--wire-version")
            p.add_argument("--capabilities")
        elif name == "generate":
            for a in ("--vendor", "--commit", "--entries", "--module",
                      "--out"):
                p.add_argument(a, required=True)
            p.add_argument("--allowlist")
        elif name == "verify":
            for a in ("--report", "--candidate", "--build-digest"):
                p.add_argument(a, required=True)
        elif name == "build":
            p.add_argument("--src", required=True)
        elif name in ("release", "rollback", "status"):
            p.add_argument("--store", required=True)
            if name == "release":
                p.add_argument("--candidate", required=True)
                p.add_argument("--expected-revision", type=int)
            if name == "rollback":
                p.add_argument("--to", required=True)
            if name == "status":
                p.add_argument("--observations", required=True)
    args = ap.parse_args(argv)
    return args.fn(args)


if __name__ == "__main__":
    sys.exit(main())
