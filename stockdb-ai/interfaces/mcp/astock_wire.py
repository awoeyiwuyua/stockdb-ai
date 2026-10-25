#!/usr/bin/env python3
"""astock.mcp_wire — public-facts 两工具的 tools/list 装配与分发（M2，D13/D14）。

schema 从任务包冻结的 public-facts.schema.json 抽取 $defs 传递闭包（与
market_v3 同模式：不手抄、不改一个字节）。outputSchema 为成功/部分
（PublicFactsResponse）与失败（ToolFailureResponse）联合，根 type=object，
无任意 object 后门。
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

SCHEMA_PATH = (Path(__file__).resolve().parents[3] / "stockdb-ai" / "astock"
               / "vendor" / "public-facts.schema.json")
_DRAFT = "https://json-schema.org/draft/2020-12/schema"

_TOOLS = ("get_public_records", "get_public_artifact")


def _defs_closure(*roots: str) -> dict[str, Any]:
    schema = json.loads(SCHEMA_PATH.read_text(encoding="utf-8"))
    defs = schema["$defs"]
    needed: set[str] = set()

    def walk(node: Any) -> None:
        if isinstance(node, dict):
            ref = node.get("$ref")
            if isinstance(ref, str) and ref.startswith("#/$defs/"):
                name = ref.split("/")[2]
                if name not in needed:
                    needed.add(name)
                    walk(defs[name])
            for v in node.values():
                walk(v)
        elif isinstance(node, list):
            for v in node:
                walk(v)

    for name in roots:
        needed.add(name)
        walk(defs[name])
    return {n: defs[n] for n in sorted(needed)}


def published_tool_schemas() -> dict[str, dict]:
    """两工具的 inputSchema/outputSchema（tools/list 装配）。"""
    try:
        in_defs = _defs_closure("Request", "ArtifactRequest")
        out_defs = _defs_closure("RecordsResponse", "ArtifactResponse", "ToolFailureResponse")
        # 失败信封若合同未单列，则用 Response 的 failed 分支 + 拒绝坏成功
        return {
            "get_public_records": {
                "inputSchema": {"$schema": _DRAFT, "type": "object",
                                "$ref": "#/$defs/Request",
                                "$defs": in_defs},
                "outputSchema": {"$schema": _DRAFT, "type": "object",
                                 "anyOf": [
                                     {"$ref": "#/$defs/RecordsResponse"},
                                     {"$ref": "#/$defs/ToolFailureResponse"}],

                                 "$defs": out_defs},
            },
            "get_public_artifact": {
                "inputSchema": {"$schema": _DRAFT, "type": "object",
                                "$ref": "#/$defs/ArtifactRequest",
                                "$defs": in_defs},
                "outputSchema": {"$schema": _DRAFT, "type": "object",
                                 "anyOf": [
                                     {"$ref": "#/$defs/ArtifactResponse"},
                                     {"$ref": "#/$defs/ToolFailureResponse"}],

                                 "$defs": out_defs},
            },
        }
    except Exception as exc:  # noqa: BLE001 - schema 装配失败降级留痕
        import sys
        print(f"astock.mcp_wire: schema 装配失败: {type(exc).__name__}: {exc}",
              file=sys.stderr)
        return {}


def maybe_dispatch(tool_name: str, args: Any) -> dict | None:
    """v1 请求分发（contract_version=stockdb-public-facts-v1）；非本域返回 None。"""
    from astock import public_facts as pf
    if not isinstance(args, dict):
        return None
    cv = args.get("contract_version")
    if cv is None or cv == "stockdb-public-facts-v1":
        pass
    else:
        return None  # 非 public-facts 合同：交回 market 分支处理
    if tool_name == "get_public_records":
        try:
            envelope = pf.get_public_records_v3(args)
        except pf.PublicFactsError as exc:
            envelope = pf._failure(exc)
        except Exception as exc:  # noqa: BLE001
            envelope = pf._failure(pf.PublicFactsError(
                "INTERNAL_ERROR", f"{type(exc).__name__}: {exc}"[:200]))
        return _wrap(envelope, envelope.get("status") == "failed")
    if tool_name == "get_public_artifact":
        try:
            envelope = pf.get_public_artifact_v3(args)
        except pf.PublicFactsError as exc:
            envelope = pf._failure(exc)
        except Exception as exc:  # noqa: BLE001
            envelope = pf._failure(pf.PublicFactsError(
                "INTERNAL_ERROR", f"{type(exc).__name__}: {exc}"[:200]))
        return _wrap(envelope, envelope.get("status") == "failed")
    return None


def _wrap(envelope: dict, is_error: bool) -> dict:
    text = json.dumps(envelope, ensure_ascii=False, allow_nan=False)
    return {"content": [{"type": "text", "text": text}],
            "structuredContent": envelope, "isError": is_error}


def tool_descriptions() -> dict[str, str]:
    return {
        "get_public_records": (
            "公开事实记录（M2）：announcements(巨潮公告)/news(东财新闻)/"
            "investor_qa(上证e互动·互动易)。contract_version=stockdb-public-"
            "facts-v1；严格字段 dataset_id/source_provider/start_date/end_date"
            "（YYYYMMDD）/instrument_id(公告与问答必填，news 可 null=全市场)"
            "/limit(1-200)/cursor+snapshot_id。快照固定后分页，重放不实时抓源。"
            "失败信封 status=failed 且 isError=true。"),
        "get_public_artifact": (
            "公开事实原件分块读取：仅接受服务已发出的 artifact_id+offset+"
            "length（≤262144 字节/块，≤20MiB）；逐块 chunk_sha256、整件 "
            "content_sha256。不接受 URL/路径/SQL。"),
    }
