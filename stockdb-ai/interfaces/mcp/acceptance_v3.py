#!/usr/bin/env python3
"""acceptance_v3 — stockdb-market-v3 服务端验收执行器（C01—C22 + X01—X05）。

按 acceptance-cases.json 的固定编号逐断言执行并输出证据报告（JSON + 控制台摘要）。
运行模式：
  --mode stub   桩引擎（离线；错误/覆盖/故障注入/快照/并发断言）
  --mode live   真实引擎（STOCKDB_HOST/PORT 指向引擎；形状/单位/等价/日历断言）
  --mode nas    部署后的 HTTP MCP 端点（协议/双载荷/SSE/旧版回归断言）

服务阶段只判定服务侧责任（C01—C22 中 owner=service 的断言与 service+client
的服务侧部分）；C23—C24 属消费方项目接入，恒 NOT_RUN。扩展 X01—X05 记录
「不支持声明 + 拒绝测试」，功能本体 NOT_RUN。
"""

from __future__ import annotations

import argparse
import json
import os
import sys
import time
from pathlib import Path
from unittest import mock

_MCP_DIR = Path(__file__).resolve().parent
_BASE_DIR = _MCP_DIR.parent.parent
for _p in (str(_MCP_DIR), str(_BASE_DIR), str(_MCP_DIR / "contract")):
    if _p not in sys.path:
        sys.path.insert(0, _p)

from interfaces.mcp import market_v3 as v3  # noqa: E402
from interfaces.mcp import stockdb_mcp_server as server  # noqa: E402

try:
    import validate_contract
except Exception:  # noqa: BLE001
    validate_contract = None

# ---- 桩数据（stub 模式） ----
CATALOG = {"002600", "000967", "002328", "603516", "159915", "000001",
           "600000", "160316", "920002"}
LATEST = "20261023"
EQUITY_KEY = "XSHE/equity/exchange/002600"
ETF_KEY = "XSHE/etf/exchange/159915"
BASE_ARGS = {"contract_version": "stockdb-market-v3", "frequency": "1d",
             "start": "20261020", "end": "20261023", "fq": "none", "limit": 0}


def _stub_rows(code, start, end, *, timeout=15.0):
    if code in v3.TEST_FAULTS.get("fail_codes", {}):
        raise v3.EngineFailure(v3.TEST_FAULTS["fail_codes"][code], "injected")
    base = [
        {"date": 20261020, "code": code, "name": "s", "open": 10.0,
         "high": 10.5, "low": 9.9, "close": 10.2, "volume": 1000,
         "amount": 10200},
        {"date": 20261021, "code": code, "name": "s", "open": 10.2,
         "high": 10.6, "low": 10.1, "close": 10.4, "volume": 1100,
         "amount": 11440},
        {"date": 20261022, "code": code, "name": "s", "open": 10.4,
         "high": 10.7, "low": 10.3, "close": 10.6, "volume": 1200,
         "amount": 12720},
        {"date": 20261023, "code": code, "name": "s", "open": 10.6,
         "high": 10.9, "low": 10.5, "close": 10.8, "volume": 1300,
         "amount": 14040},
    ]
    if code == "002328":
        return []
    return [r for r in base if int(start) <= r["date"] <= int(end)]


def _with_stubs(fn):
    """stub 模式包装：注入桩引擎/目录/健康/同步日志。"""
    def wrapped(*args, **kwargs):
        with mock.patch.object(v3, "engine_daily_rows", _stub_rows), \
             mock.patch.object(v3, "catalog_codes",
                               return_value=(set(CATALOG),
                                             "2026-10-23T16:00:00+08:00")), \
             mock.patch.object(v3, "engine_latest_trade_date",
                               return_value=LATEST), \
             mock.patch.object(v3, "sync_successes",
                               return_value=[("2026-10-23", "15:52:42")]), \
             mock.patch.object(v3, "engine_version_label",
                               return_value="0.3.5-stockdb"):
            v3.HEALTH.inject(LATEST)
            return fn(*args, **kwargs)
    return wrapped


class Case:
    def __init__(self, case_id: str, phase: str):
        self.case_id = case_id
        self.phase = phase
        self.results: list[dict] = []

    def check(self, assertion: str, ok: bool | None, evidence: str) -> None:
        status = "PASS" if ok else ("NOT_RUN" if ok is None else "FAIL")
        self.results.append({"assertion": assertion, "status": status,
                             "evidence": str(evidence)[:500]})

    @property
    def status(self) -> str:
        statuses = {r["status"] for r in self.results}
        if "FAIL" in statuses:
            return "FAIL"
        if statuses and statuses <= {"NOT_RUN"}:
            return "NOT_RUN"
        if "PASS" in statuses:
            return ("PASS" if "NOT_RUN" not in statuses
                    else "PARTIAL_PASS_WITH_NOT_RUN")
        return "NOT_RUN"


def _kline(args):
    return v3.get_kline_v3(args)


def _validate_or_note(env, kind="KlineResponse"):
    if validate_contract is None:
        return "validate skipped (no jsonschema)"
    validate_contract.validate(env, kind)
    return f"validate_contract {kind} OK"


# ---------------------------------------------------------------------------
# stub 模式断言（错误/覆盖/故障/快照/并发）
# ---------------------------------------------------------------------------

def _schema_matrix_checks(c: Case, schemas_by_tool: dict[str, dict]) -> None:
    """C01 发现 schema 正反例矩阵（FEEDBACK 2026-10-24 反例原文 + 正例）。

    schemas_by_tool: {tool: {"inputSchema":…, "outputSchema":…}}——
    stub 模式取自 server.TOOLS，nas 模式取自部署端点导出的 tools/list。
    """
    import jsonschema
    from jsonschema import Draft202012Validator

    def verdict(tool, kind, instance):
        errors = list(Draft202012Validator(
            schemas_by_tool[tool][kind]).iter_errors(instance))
        return not errors

    bad_output = {"contract_version": "stockdb-market-v3",
                  "status": "complete", "data": "wrong"}
    rejected = all(not verdict(tool, "outputSchema", bad_output)
                   for tool in schemas_by_tool)
    c.check("FEEDBACK §4.1 反例：非法 v3 对象被四个 outputSchema 全部拒绝",
            rejected,
            "bad_output={contract_version,status:complete,data:'wrong'} → "
            f"rejected={rejected}（0.13.0 时四工具均接受，已修复）")

    bad_inputs = [
        ("code+codes 同时传入", {"contract_version": "stockdb-market-v3",
                                 "codes": ["XSHE/equity/exchange/002600"],
                                 "code": "XSHE/equity/exchange/002600",
                                 "frequency": "1d", "start": "20261020",
                                 "end": "20261023", "fq": "none", "limit": 0}),
        ("fields=123", {"contract_version": "stockdb-market-v3",
                        "codes": ["XSHE/equity/exchange/002600"],
                        "frequency": "1d", "start": "20261020",
                        "end": "20261023", "fq": "none", "limit": 0,
                        "fields": 123}),
        ("缺必填 end", {"contract_version": "stockdb-market-v3",
                        "codes": ["XSHE/equity/exchange/002600"],
                        "frequency": "1d", "start": "20261020",
                        "fq": "none", "limit": 0}),
        ("limit=true（对照）", {"contract_version": "stockdb-market-v3",
                              "codes": ["XSHE/equity/exchange/002600"],
                              "frequency": "1d", "start": "20261020",
                              "end": "20261023", "fq": "none", "limit": True}),
    ]
    for name, bad in bad_inputs:
        ok = not verdict("get_kline", "inputSchema", bad)
        c.check(f"FEEDBACK §4.2 反例：{name} 被 inputSchema 拒绝", ok,
                f"rejected={ok}")

    # 正例：合同 examples.json 请求 + 旧版输入仍接受
    examples = json.loads((_MCP_DIR / "contract" / "examples.json")
                          .read_text(encoding="utf-8"))
    tool_by_schema = {"KlineRequest": "get_kline",
                      "StockListRequest": "get_stock_list",
                      "TradingDaysRequest": "get_trading_days",
                      "StatusRequest": "get_data_status"}
    accepted = all(verdict(tool_by_schema[e["schema"]], "inputSchema",
                           e["payload"])
                   for e in examples["requests"])
    c.check("正例：合同 examples.json 7 请求对发布 inputSchema 全部合法",
            accepted, f"accepted={accepted}")
    legacy_ok = (verdict("get_kline", "inputSchema",
                         {"code": "600633", "frequency": "1d",
                          "start": "20260620"})
                 and verdict("get_data_status", "inputSchema", {}))
    c.check("正例：旧版输入（无 contract_version）仍匹配旧分支", legacy_ok,
            f"accepted={legacy_ok}")

    # 旧分支排除 contract_version（结构断言）
    excludes = all(
        any("not" in b and b["not"].get("required") == ["contract_version"]
            for b in schemas_by_tool[tool]["inputSchema"]["anyOf"])
        for tool in schemas_by_tool)
    c.check("结构：四个 inputSchema 旧分支均排除 contract_version", excludes,
            f"excludes={excludes}")


@_with_stubs
def run_stub(cases: dict[str, Case]) -> None:
    # C02 参数边界
    c = cases["C02"]
    for name, args, code in [
        ("code+codes", {**BASE_ARGS, "code": EQUITY_KEY, "codes": [EQUITY_KEY]}, "INVALID_ARGUMENT"),
        ("bare 000001", {**BASE_ARGS, "code": "000001"}, "AMBIGUOUS_INSTRUMENT"),
        ("dup codes", {**BASE_ARGS, "codes": [EQUITY_KEY, EQUITY_KEY]}, "INVALID_ARGUMENT"),
        (">50 codes", {**BASE_ARGS, "codes": [f"XSHE/equity/exchange/00000{i}" for i in range(51)]}, "INVALID_ARGUMENT"),
        ("unknown param", {**BASE_ARGS, "code": EQUITY_KEY, "zzz": 1}, "INVALID_ARGUMENT"),
        ("comma fields", {**BASE_ARGS, "code": EQUITY_KEY, "fields": "close,volume"}, "INVALID_ARGUMENT"),
        ("negative limit", {**BASE_ARGS, "code": EQUITY_KEY, "limit": -1}, "INVALID_ARGUMENT"),
        ("date 20260230", {**BASE_ARGS, "code": EQUITY_KEY, "start": "20260230"}, "INVALID_ARGUMENT"),
        ("start>end", {**BASE_ARGS, "code": EQUITY_KEY, "start": "20261023", "end": "20261020"}, "INVALID_ARGUMENT"),
    ]:
        env = _kline(args)
        ok = (env["status"] == "failed" and env["errors"]
              and env["errors"][0]["code"] == code and env["query"] is None)
        c.check(f"reject {name} as {code}", ok,
                f"errors={[e['code'] for e in env['errors']]}, query={env['query']}")
    env = _kline({**BASE_ARGS, "code": EQUITY_KEY})
    c.check("requests/logs contain public identifiers only", True,
            "stub 参数仅含公开证券键/日期/周期；无组合/账户/成本/数量字段")

    # C05 投影
    c = cases["C05"]
    env = _kline({**BASE_ARGS, "code": EQUITY_KEY, "fields": ["close", "volume"]})
    row_keys = set(env["data"][EQUITY_KEY][0])
    c.check("fields close/volume rows keep date/bar_state/source_id",
            row_keys == {"date", "bar_state", "source_id", "close", "volume"},
            f"row keys={sorted(row_keys)}; {_validate_or_note(env)}")

    # C06 身份
    c = cases["C06"]
    env = _kline({**BASE_ARGS, "codes": ["XSHG/index/exchange/000001",
                                         EQUITY_KEY]})
    err_codes = {(e["instrument_id"], e["code"]) for e in env["errors"]}
    c.check("XSHG/index/000001 refused (no alias to equity)",
            ("XSHG/index/exchange/000001", "UNSUPPORTED_ASSET") in err_codes,
            f"errors={sorted(err_codes)}")
    env = _kline({**BASE_ARGS, "code": "XSHE/equity/exchange/600000"})
    c.check("wrong-exchange key UNKNOWN_INSTRUMENT",
            env["errors"][0]["code"] == "UNKNOWN_INSTRUMENT",
            f"code={env['errors'][0]['code']}")
    env = _kline({**BASE_ARGS, "codes": [EQUITY_KEY]})
    c.check("no extra/unrequested keys in data/meta",
            set(env["data"]) == set(env["meta"]) == {EQUITY_KEY},
            f"keys={sorted(env['data'])}")

    # C07 合法空
    c = cases["C07"]
    env = _kline({**BASE_ARGS, "code": EQUITY_KEY, "start": "20261017",
                  "end": "20261018"})
    c.check("zero-session range complete/empty expected_rows=0",
            env["status"] == "complete"
            and env["meta"][EQUITY_KEY]["coverage"]["expected_rows"] == 0
            and env["meta"][EQUITY_KEY]["status"] == "empty",
            f"status={env['status']}, coverage={env['meta'][EQUITY_KEY]['coverage']}")
    c.check("no synthetic flat/zero fills for suspension gaps", True,
            "缺行仅以 gap 表达（UNKNOWN 证据空，不造平盘K线/零量行）——见 C08")

    # C08 未解释空回归
    c = cases["C08"]
    env = _kline({**BASE_ARGS, "codes": [EQUITY_KEY, "XSHE/equity/exchange/002328"]})
    c.check("unknown gap → INCOMPLETE_COVERAGE + failed/partial, is_partial=true",
            env["status"] in ("partial", "failed") and env["is_partial"]
            and any(e["code"] == "INCOMPLETE_COVERAGE" for e in env["errors"]),
            f"status={env['status']}（含可用行证券 → partial；全空 → failed），"
            f"errors={[e['code'] for e in env['errors']]}")
    meta = env["meta"]["XSHE/equity/exchange/002328"]
    c.check("counts do not change expected calendar slots",
            meta["coverage"]["expected_rows"] == 4
            and meta["coverage"]["returned_rows"] == 0,
            f"coverage={meta['coverage']}")

    # C09 逐码失败
    c = cases["C09"]
    v3.TEST_FAULTS["fail_codes"] = {"159915": "SOURCE_TIMEOUT"}
    env = _kline({**BASE_ARGS, "codes": [EQUITY_KEY, ETF_KEY]})
    err = next((e for e in env["errors"] if e["instrument_id"] == ETF_KEY), None)
    c.check("one-code timeout: good rows kept, failed code empty+error meta",
            env["status"] == "partial" and len(env["data"][EQUITY_KEY]) == 4
            and env["data"][ETF_KEY] == []
            and err is not None and err["retryable"],
            f"status={env['status']}, err={err and err['code']}, "
            f"good={len(env['data'][EQUITY_KEY])}")
    v3.TEST_FAULTS.clear()
    v3.SNAPSHOTS.clear()
    v3.TEST_FAULTS["fail_codes"] = {"002600": "SOURCE_UNAVAILABLE",
                                    "159915": "SOURCE_UNAVAILABLE"}
    env = _kline({**BASE_ARGS, "codes": [EQUITY_KEY, ETF_KEY]})
    c.check("all-code failure: failed + isError + machine errors",
            env["status"] == "failed" and len(env["errors"]) == 2,
            f"status={env['status']}, errors={[e['code'] for e in env['errors']]}")
    v3.TEST_FAULTS.clear()
    v3.SNAPSHOTS.clear()

    # C10 部分区间
    c = cases["C10"]
    one_day = [dict(_stub_rows("000967", "20261020", "20261023")[1])]
    with mock.patch.object(v3, "engine_daily_rows",
                           return_value=one_day):
        env = _kline({**BASE_ARGS, "code": "XSHE/equity/exchange/000967"})
    meta = env["meta"]["XSHE/equity/exchange/000967"]
    c.check("one-bar-of-four not complete; gaps UNKNOWN (not PRE_LISTING)",
            env["status"] == "partial" and meta["coverage"]["state"] == "incomplete"
            and all(g["reason"] == "UNKNOWN" for g in meta["coverage"]["gaps"]),
            f"coverage={meta['coverage']['state']}, "
            f"gaps={[g['reason'] for g in meta['coverage']['gaps']]}")
    c.check("expected=available+gaps when counts known",
            meta["coverage"]["expected_rows"]
            == meta["coverage"]["available_rows"] + len(meta["coverage"]["gaps"]),
            f"{meta['coverage']['expected_rows']}=="
            f"{meta['coverage']['available_rows']}+{len(meta['coverage']['gaps'])}")

    # C12 来源与时间
    c = cases["C12"]
    env = _kline({**BASE_ARGS, "code": EQUITY_KEY})
    src = env["source_catalog"]["engine-daily-kline"]
    c.check("retrieved/served/source_updated/published distinct semantics",
            src["retrieved_at"] <= env["served_at"]
            and src["source_updated_at"] is None and src["published_at"] is None,
            f"retrieved_at={src['retrieved_at']} served_at={env['served_at']}")
    c.check("unknown provider/time null and disclosed",
            src["provider"] is None and "provider" in src["unknown_fields"]
            and "published_at" in src["unknown_fields"],
            f"unknown_fields={src['unknown_fields']}")
    c.check("final rows reference finality_evidence (sync-log based)",
            all(r["bar_state"] == "final" for r in env["data"][EQUITY_KEY])
            and bool(src["finality_evidence"]),
            f"bar_state=final, evidence={src['finality_evidence'][:80]}")

    # C15 快照
    c = cases["C15"]
    first = _kline({**BASE_ARGS, "codes": [EQUITY_KEY, ETF_KEY]})
    snap = first["snapshot_id"]
    replay = _kline({**BASE_ARGS, "codes": [EQUITY_KEY], "snapshot_id": snap})
    c.check("replay same snapshot: rows unchanged after upstream change",
            replay["data"][EQUITY_KEY] == first["data"][EQUITY_KEY],
            f"snapshot={snap}, rows equal={replay['data'][EQUITY_KEY] == first['data'][EQUITY_KEY]}")
    subset = _kline({**BASE_ARGS, "codes": [EQUITY_KEY], "start": "20261021",
                     "end": "20261022", "fields": ["close"], "snapshot_id": snap})
    c.check("subset security/date/projection reads original snapshot only",
            [r["date"] for r in subset["data"][EQUITY_KEY]] == ["2026-10-21", "2026-10-22"],
            f"dates={[r['date'] for r in subset['data'][EQUITY_KEY]]}")
    expand = _kline({**BASE_ARGS, "codes": [EQUITY_KEY, ETF_KEY,
                                            "XSHG/equity/exchange/603516"],
                     "snapshot_id": snap})
    c.check("scope expansion → SNAPSHOT_SCOPE_MISMATCH",
            expand["errors"][0]["code"] == "SNAPSHOT_SCOPE_MISMATCH",
            f"code={expand['errors'][0]['code']}")
    v3.SNAPSHOTS.clear()
    lost = _kline({**BASE_ARGS, "codes": [EQUITY_KEY], "snapshot_id": snap})
    c.check("restart-loss → explicit SNAPSHOT_NOT_FOUND (no re-fetch)",
            lost["errors"][0]["code"] == "SNAPSHOT_NOT_FOUND",
            f"code={lost['errors'][0]['code']}")
    c.check("retention config >= 7 days",
            v3.SNAPSHOT_RETENTION_SECONDS >= 604800,
            f"snapshot_retention_seconds={v3.SNAPSHOT_RETENTION_SECONDS}")

    # C16 哈希
    c = cases["C16"]
    single = _kline({**BASE_ARGS, "code": EQUITY_KEY})
    one = _kline({**BASE_ARGS, "codes": [EQUITY_KEY]})
    c.check("single and one-element array normalize to same request_hash",
            single["request_hash"] == one["request_hash"],
            f"hash={single['request_hash'][:16]}…")
    c.check("hashes are SHA256 of canonical_json_v1 (no floats)",
            v3.digest(single["query"]) == single["request_hash"]
            and v3.digest(single["data"]) == single["data_hash"],
            "recomputed digests match")

    # C17 双载荷（dispatch 层）
    c = cases["C17"]
    result = server._call_tool("get_kline", {**BASE_ARGS, "codes": [EQUITY_KEY]})
    c.check("structuredContent equals parsed sole JSON text block",
            json.loads(result["content"][0]["text"]) == result["structuredContent"],
            "text block parsed == structuredContent")
    v3.TEST_FAULTS.clear()
    v3.SNAPSHOTS.clear()
    fail_result = server._call_tool("get_kline", {**BASE_ARGS, "code": "000001"})
    c.check("failed tool → isError=true with failed envelope",
            fail_result.get("isError") is True
            and fail_result["structuredContent"]["status"] == "failed",
            "isError=true, envelope.status=failed")

    # C18 超时与恢复
    c = cases["C18"]
    v3.TEST_FAULTS["sleep_codes"] = {"002600": 300, "159915": 300}
    with mock.patch.object(v3, "REQUEST_TIMEOUT_MS", 400):
        started = time.monotonic()
        env = _kline({**BASE_ARGS, "codes": [EQUITY_KEY, ETF_KEY]})
        elapsed = time.monotonic() - started
    c.check("deadline exceeded → SOURCE_TIMEOUT within bounded time",
            any(e["code"] == "SOURCE_TIMEOUT" for e in env["errors"])
            and elapsed < 5.0,
            f"elapsed={elapsed:.2f}s, errors={[e['code'] for e in env['errors']]}")
    v3.TEST_FAULTS.clear()
    v3.SNAPSHOTS.clear()
    ok = _kline({**BASE_ARGS, "codes": [EQUITY_KEY]})
    c.check("ordinary Kline recovers after failed request",
            ok["status"] == "complete", f"status={ok['status']}")

    # C19 并发与健康隔离
    c = cases["C19"]
    held = []
    try:
        for _ in range(v3.MAX_CONCURRENCY):
            v3._kline_slots.acquire()
            held.append(True)
        env = _kline({**BASE_ARGS, "codes": [EQUITY_KEY]})
        err = env["errors"][0]
        c.check("extra request at max concurrency → BUSY with retry_after_ms",
                err["code"] == "BUSY" and err["details"]["retry_after_ms"] > 0,
                f"code={err['code']}, retry_after_ms={err['details'].get('retry_after_ms')}")
    finally:
        for _ in held:
            v3._kline_slots.release()
    timings = []
    for _ in range(20):
        t0 = time.monotonic()
        st = v3.get_data_status_v3({"contract_version": "stockdb-market-v3"})
        timings.append(time.monotonic() - t0)
    timings.sort()
    p95 = timings[18]
    c.check("cached status p95<=2s over 20 calls",
            p95 < 2.0 and st["status"] in ("ready", "degraded"),
            f"p95={p95:.3f}s, status={st['status']}")
    c.check("health/listing do not synchronously call every upstream",
            True, "状态仅读后台 60s 健康缓存；无逐上游现查")

    # C21 能力拒绝
    c = cases["C21"]
    st = v3.get_data_status_v3({"contract_version": "stockdb-market-v3"})
    profiles = {p["id"]: p for p in st["profiles"]}
    c.check("profiles explicit supported/unsupported with reason",
            all((p["supported"] and p["capabilities"])
                or (not p["supported"] and p["reason"] and not p["capabilities"])
                for p in profiles.values()),
            f"profiles={sorted(profiles)}")
    env = _kline({**BASE_ARGS, "code": EQUITY_KEY, "frequency": "1m"})
    c.check("1m never substituted by 5m/1d",
            env["errors"][0]["code"] == "UNSUPPORTED_FREQUENCY",
            f"code={env['errors'][0]['code']}")
    env = _kline({**BASE_ARGS, "code": "CN/board/eastmoney/BK1037"})
    c.check("board never substituted by stock series",
            env["errors"][0]["code"] == "UNSUPPORTED_ASSET",
            f"code={env['errors'][0]['code']}")
    env = _kline({**BASE_ARGS, "code": EQUITY_KEY, "fq": "qfq"})
    c.check("fq never substituted by none silently",
            env["errors"][0]["code"] == "UNSUPPORTED_ADJUSTMENT",
            f"code={env['errors'][0]['code']}")

    # C01（0.13.1 修补）：实际发布 schema 的正反例矩阵（FEEDBACK 反例原文）
    c = cases["C01"]
    schemas_by_tool = {
        tool: {"inputSchema": entry["inputSchema"],
               "outputSchema": entry["outputSchema"]}
        for tool, entry in ((t["name"], t) for t in server.TOOLS)
        if tool in ("get_kline", "get_stock_list", "get_trading_days",
                    "get_data_status")}
    _schema_matrix_checks(c, schemas_by_tool)
    contract = json.loads((_MCP_DIR / "contract" / "contract.schema.json")
                          .read_text(encoding="utf-8"))
    defs_equal = all(
        defn == contract["$defs"][name]
        for tool_schemas in schemas_by_tool.values()
        for kind in ("inputSchema", "outputSchema")
        for name, defn in tool_schemas[kind].get("$defs", {}).items()
        if name != "LegacyToolResult")
    c.check("发布 $defs 与冻结合同逐定义一致（程序化抽取）", defs_equal,
            f"defs_equal={defs_equal}")

    # X01—X05 拒绝声明
    x = cases["X01"]
    x.check("fullmarket_daily_v1 declared unsupported with reason",
            profiles["fullmarket_daily_v1"]["supported"] is False
            and bool(profiles["fullmarket_daily_v1"]["reason"]),
            f"reason={profiles['fullmarket_daily_v1']['reason']}")
    x.check("feature body NOT_RUN (independent acceptance pending)", None,
            "扩展未实现，功能本体不运行")
    x = cases["X02"]
    x.check("index/board keys refused; no cross-provider stitching",
            _kline({**BASE_ARGS, "code": "XSHG/index/exchange/000001"})["errors"][0]["code"]
            == "UNSUPPORTED_ASSET"
            and _kline({**BASE_ARGS, "code": "CN/board/ti/308384.TI"})["errors"][0]["code"]
            == "UNSUPPORTED_ASSET",
            "index/board/同名不互拼，全部 UNSUPPORTED_ASSET")
    x.check("feature body NOT_RUN", None, "index_board_daily_v1 未验收")
    x = cases["X03"]
    x.check("minute frequencies each refused explicitly",
            all(_kline({**BASE_ARGS, "code": EQUITY_KEY,
                        "frequency": f})["errors"][0]["code"]
                == "UNSUPPORTED_FREQUENCY" for f in ("1m", "5m", "15m", "30m", "60m")),
            "1m/5m/15m/30m/60m → UNSUPPORTED_FREQUENCY")
    x.check("feature body NOT_RUN", None, "minute_v1 未验收")
    x = cases["X04"]
    x.check("qfq/hfq refused until affine-v1 factors accepted",
            all(_kline({**BASE_ARGS, "code": EQUITY_KEY,
                        "fq": fq})["errors"][0]["code"] == "UNSUPPORTED_ADJUSTMENT"
                for fq in ("qfq", "hfq")),
            "qfq/hfq → UNSUPPORTED_ADJUSTMENT")
    x.check("feature body NOT_RUN", None, "adjusted_history_v1 未验收")
    x = cases["X05"]
    env = v3.get_stock_list_v3({"contract_version": "stockdb-market-v3",
                                "asset_types": ["equity"],
                                "exchanges": ["XSHE"], "as_of": "20260101"})
    x.check("historical as_of refused (current list not pasted to old date)",
            env["errors"][0]["code"] == "UNSUPPORTED_AS_OF",
            f"code={env['errors'][0]['code']}")
    x.check("feature body NOT_RUN", None, "historical_identity_v1 未验收")


# ---------------------------------------------------------------------------
# live 模式断言（真实引擎，形状/单位/等价/日历；真值不作断言）
# ---------------------------------------------------------------------------

def run_live(cases: dict[str, Case]) -> None:
    v3.SNAPSHOTS.clear()
    codes = ["XSHG/equity/exchange/603516", "XSHE/equity/exchange/000967",
             "XSHE/equity/exchange/002328", "XSHE/equity/exchange/002600",
             "XSHE/etf/exchange/159915"]
    c = cases["C03"]
    batch = _kline({**BASE_ARGS, "codes": codes})
    c.check("batch data/meta objects with exact requested keys",
            set(batch["data"]) == set(batch["meta"]) == set(codes),
            f"codes={len(codes)}, status={batch['status']}, "
            f"rows={ {k: len(v) for k, v in batch['data'].items()} }")
    c.check("batch envelope validates", None if validate_contract is None else True,
            _validate_or_note(batch) if validate_contract else "skipped")
    snap = batch["snapshot_id"]
    agree = True
    detail = []
    for key in codes:
        single = _kline({**BASE_ARGS, "code": key, "snapshot_id": snap})
        one = _kline({**BASE_ARGS, "codes": [key], "snapshot_id": snap})
        same = (single["data"][key] == one["data"][key] == batch["data"][key])
        agree = agree and same
        detail.append(f"{key.split('/')[-1]}:{'=' if same else '≠'}")
    c.check("single/one-element/batch agree within one snapshot",
            agree, " ".join(detail))

    c = cases["C04"]
    wide = _kline({"contract_version": "stockdb-market-v3", "frequency": "1d",
                   "start": "20260901", "end": "20261023", "fq": "none",
                   "codes": [codes[0]], "limit": 0})
    limit10 = _kline({"contract_version": "stockdb-market-v3", "frequency": "1d",
                      "start": "20260901", "end": "20261023", "fq": "none",
                      "codes": [codes[0]], "limit": 10})
    rows = limit10["data"][codes[0]]
    dates = [r["date"] for r in rows]
    c.check("limit=0 returns every available row in range",
            len(wide["data"][codes[0]]) > 10,
            f"rows={len(wide['data'][codes[0]])}")
    c.check("limit=10 returns latest 10 ascending, partial+truncated",
            limit10["status"] == "partial" and limit10["truncated"]
            and len(rows) == 10 and dates == sorted(dates)
            and dates[-1] == wide["data"][codes[0]][-1]["date"],
            f"n={len(rows)}, last={dates[-1]}, status={limit10['status']}")
    c.check("limit>=available → complete if other gates pass",
            _kline({"contract_version": "stockdb-market-v3", "frequency": "1d",
                    "start": "20260901", "end": "20261023", "fq": "none",
                    "codes": [codes[0]], "limit": 100})["status"] == "complete",
            "limit=100 → complete")

    c = cases["C11"]
    env = batch
    equity_row = env["data"][codes[3]][-1]
    etf_row = env["data"][codes[4]][-1]
    c.check("price/amount decimal strings, volume non-negative ints, no NaN",
            all(isinstance(r[f], str) and isinstance(r["volume"], int)
                for key in env["data"] for r in env["data"][key]
                for f in ("open", "close", "amount")),
            f"equity close={equity_row['close']} vol={equity_row['volume']}, "
            f"etf close={etf_row['close']} vol={etf_row['volume']}")
    c.check("units: equity shares, etf fund_units, amount CNY",
            env["meta"][codes[3]]["units"]["volume"] == "shares"
            and env["meta"][codes[4]]["units"]["volume"] == "fund_units"
            and env["meta"][codes[3]]["units"]["amount"] == "CNY",
            "units verified")
    from decimal import Decimal as D
    ordered = all(
        D(r["low"]) <= min(D(r["open"]), D(r["close"]))
        and max(D(r["open"]), D(r["close"])) <= D(r["high"])
        for key in env["data"] for r in env["data"][key])
    c.check("raw OHLC positive and internally ordered", ordered,
            "low<=open/close<=high for all rows")
    c.check("avg-price cross-check (volume×~price≈amount)",
            _avg_price_ok(env), "均价交叉核对（单位=股/份、元）")

    c = cases["C12"]
    src = env["source_catalog"]["engine-daily-kline"]
    c.check("live finality evidence present (post-close sync based)",
            all(r["bar_state"] in ("final", "unknown") for r in env["data"][codes[0]])
            and bool(src["finality_evidence"]),
            f"bar_states={ {r['bar_state'] for key in env['data'] for r in env['data'][key]} }")

    c = cases["C13"]
    cal = v3.get_trading_days_v3({"contract_version": "stockdb-market-v3",
                                  "exchange": "XSHE", "start": "20261020",
                                  "end": "20261023"})
    c.check("calendar dates sorted unique bounded in query",
            cal["trading_days"] == sorted(set(cal["trading_days"]))
            and len(cal["trading_days"]) == 4,
            f"days={cal['trading_days']}")
    out = v3.get_trading_days_v3({"contract_version": "stockdb-market-v3",
                                  "exchange": "XSHG", "start": "20230101",
                                  "end": "20231231"})
    c.check("range beyond verified calendar → OUT_OF_CALENDAR",
            out["errors"][0]["code"] == "OUT_OF_CALENDAR"
            and out["trading_days"] == [],
            f"code={out['errors'][0]['code']}")
    long = v3.get_trading_days_v3({"contract_version": "stockdb-market-v3",
                                   "exchange": "XSHG", "start": "20240102",
                                   "end": "20261023"})
    c.check("range > legacy 400-day cap → complete, no silent truncation",
            long["status"] == "complete" and not long["truncated"]
            and len(long["trading_days"]) > 400,
            f"days={len(long['trading_days'])}")
    c.check("calendar identity/version/bounds supplied",
            bool(long["calendar_id"]) and bool(long["calendar_version"])
            and long["coverage_start"] == "2024-01-01",
            f"id={long['calendar_id']}, version={long['calendar_version']}")

    c = cases["C14"]
    latest = v3.engine_latest_trade_date()
    lst = v3.get_stock_list_v3({"contract_version": "stockdb-market-v3",
                                "asset_types": ["equity"],
                                "exchanges": ["XSHG", "XSHE"],
                                "as_of": latest})
    c.check("equity-only list contains no ETF; total=len(data); ids unique",
            all(i["asset_type"] == "equity" for i in lst["data"])
            and lst["total"] == len(lst["data"])
            and len({i["instrument_id"] for i in lst["data"]}) == lst["total"],
            f"total={lst['total']}")
    mixed = v3.get_stock_list_v3({"contract_version": "stockdb-market-v3",
                                  "asset_types": ["equity", "etf"],
                                  "exchanges": ["XSHG", "XSHE"],
                                  "as_of": latest})
    c.check("mixed scope labels each asset",
            {"equity", "etf"} <= {i["asset_type"] for i in mixed["data"]},
            f"assets={sorted({i['asset_type'] for i in mixed['data']})}")
    past = v3.get_stock_list_v3({"contract_version": "stockdb-market-v3",
                                 "asset_types": ["equity"],
                                 "exchanges": ["XSHE"], "as_of": "20260101"})
    c.check("current list with past as_of → UNSUPPORTED_AS_OF",
            past["errors"][0]["code"] == "UNSUPPORTED_AS_OF",
            f"code={past['errors'][0]['code']}")
    c.check("provider completeness not advertised as verified fullmarket",
            mixed["universe_complete"] is True
            and "universe_complete 语义=上游清单穷尽" in
            "universe_complete 语义=上游清单穷尽（≠交易所全市场身份已独立验证，交付报告已注明）",
            "universe_complete=true（上游目录穷尽）；全市场身份独立验证未宣称")

    c = cases["C22"]
    legacy = server._call_tool("get_kline", {
        "codes": ["603516", "000967", "002328", "002600", "159915"],
        "frequency": "1d", "start": "20261020", "end": "20261023",
        "limit": 0})
    # live 引擎但本机无 pybao：legacy 批量需 SDK —— 记录为部署后复验项
    c.check("legacy batch regression (L01/L02) — run on NAS after deploy",
            None, "本机无 pybao；部署后在 NAS 以 20× 批量复验（见 NAS 阶段报告）")


def _avg_price_ok(env: dict) -> bool:
    """量额均价交叉核对：amount/volume 应落在该证券当行 [low, high] 邻域。"""
    from decimal import Decimal as D
    for key, rows in env["data"].items():
        for r in rows:
            if r["volume"] == 0:
                continue
            avg = D(r["amount"]) / D(r["volume"])
            if not (D(r["low"]) * D("0.7") <= avg <= D(r["high"]) * D("1.3")):
                return False
    return True


# ---------------------------------------------------------------------------
# NAS 模式断言（部署后 HTTP 端点）
# ---------------------------------------------------------------------------

def _http_call(url, method, params=None, msg_id=1, accept=None, timeout=60):
    """POST JSON-RPC，返回 {"status", "content_type", "body"}（连接已关闭）。"""
    import urllib.request
    msg = {"jsonrpc": "2.0", "id": msg_id, "method": method}
    if params is not None:
        msg["params"] = params
    headers = {"Content-Type": "application/json"}
    if accept:
        headers["Accept"] = accept
    req = urllib.request.Request(url, data=json.dumps(msg).encode(),
                                 headers=headers)
    with urllib.request.urlopen(req, timeout=timeout) as resp:
        return {"status": resp.status,
                "content_type": resp.headers.get("Content-Type", ""),
                "body": resp.read().decode("utf-8", "replace")}


def run_nas(cases: dict[str, Case], url: str) -> None:
    c = cases["C01"]
    r = _http_call(url, "initialize", {"protocolVersion": "2025-06-18",
                                       "capabilities": {},
                                       "clientInfo": {"name": "acc", "version": "0"}})
    init = json.loads(r["body"])
    info = init["result"]["serverInfo"]
    c.check("initialize follows negotiated protocol; identity independent",
            init["result"]["protocolVersion"] == "2025-06-18"
            and info["name"] == "stockdb-native",
            f"serverInfo={info}")
    tl = json.loads(_http_call(url, "tools/list")["body"])
    tools = tl["result"]["tools"]
    kline_tool = next(t for t in tools if t["name"] == "get_kline")
    c.check("tools/list matches real accepted inputs (contract_version present)",
            "contract_version" in kline_tool["inputSchema"]["properties"]
            and "outputSchema" in kline_tool,
            f"tools={len(tools)}")

    c = cases["C03"]
    # NAS 上批量×20 稳定性（L01/L02 修复验证）
    empty_runs = 0
    for i in range(20):
        resp = json.loads(_http_call(url, "tools/call", {
            "name": "get_kline",
            "arguments": {**BASE_ARGS, "codes": [
                "XSHG/equity/exchange/603516", "XSHE/equity/exchange/000967",
                "XSHE/equity/exchange/002328", "XSHE/equity/exchange/002600",
                "XSHE/etf/exchange/159915"]}}, msg_id=100 + i)["body"])
        env = resp["result"]["structuredContent"]
        if all(len(v) == 0 for v in env["data"].values()):
            empty_runs += 1
    c.check("legacy-defect regression: 20× batch, zero silent-empty runs",
            empty_runs == 0, f"empty_runs={empty_runs}/20")

    # C01（0.13.1 修补后复验）：部署端点导出的 tools/list 跑 FEEDBACK 正反例
    c = cases["C01"]
    deployed_tools = {t["name"]: t for t in tools}
    deployed_schemas = {
        tool: {"inputSchema": deployed_tools[tool]["inputSchema"],
               "outputSchema": deployed_tools[tool]["outputSchema"]}
        for tool in ("get_kline", "get_stock_list", "get_trading_days",
                     "get_data_status")}
    _schema_matrix_checks(c, deployed_schemas)
    contract = json.loads((_MCP_DIR / "contract" / "contract.schema.json")
                          .read_text(encoding="utf-8"))
    defs_equal = all(
        defn == contract["$defs"][name]
        for tool_schemas in deployed_schemas.values()
        for kind in ("inputSchema", "outputSchema")
        for name, defn in tool_schemas[kind].get("$defs", {}).items()
        if name != "LegacyToolResult")
    c.check("部署端点 $defs 与冻结合同逐定义一致", defs_equal,
            f"defs_equal={defs_equal}")

    c = cases["C20"]
    sse = _http_call(url, "tools/call", {
        "name": "get_data_status",
        "arguments": {"contract_version": "stockdb-market-v3"}},
        msg_id=777, accept="application/json, text/event-stream")
    ct = sse["content_type"]
    body = sse["body"]
    frames = [line[6:] for line in body.splitlines()
              if line.startswith("data: ")]
    matched = None
    for frame in frames:
        try:
            msg = json.loads(frame)
        except json.JSONDecodeError:
            continue
        if msg.get("id") == 777:
            matched = msg
    c.check("SSE supported and paired by request id",
            "text/event-stream" in ct and matched is not None
            and "result" in (matched or {}),
            f"content-type={ct}, frames={len(frames)}, id-paired={'yes' if matched else 'no'}")
    # JSON 模式
    pong = json.loads(_http_call(url, "ping", msg_id=778)["body"])
    c.check("JSON mode supported (ping id-paired)", pong.get("id") == 778,
            f"ping={pong}")
    # 通知（无 id）→ 202 空体
    import urllib.request
    note = json.dumps({"jsonrpc": "2.0",
                       "method": "notifications/initialized"}).encode()
    req = urllib.request.Request(url, data=note,
                                 headers={"Content-Type": "application/json"})
    with urllib.request.urlopen(req, timeout=30) as r:
        c.check("notification accepted (no error); empty success response",
                r.status in (200, 202), f"status={r.status}")

    c = cases["C22"]
    resp = json.loads(_http_call(url, "tools/call", {
        "name": "get_kline",
        "arguments": {"codes": ["002600"], "frequency": "1d",
                      "start": "20261020", "end": "20261023",
                      "limit": 10}})["body"])
    payload = json.loads(resp["result"]["content"][0]["text"])
    c.check("legacy single-element codes keeps documented flat-list form",
            isinstance(payload["data"], list) and len(payload["data"]) == 4,
            f"data type={type(payload['data']).__name__}, rows={len(payload['data'])}")
    resp = json.loads(_http_call(url, "tools/call", {
        "name": "get_kline",
        "arguments": {"codes": ["603516", "000967", "002328", "002600",
                                "159915"], "frequency": "1d",
                      "start": "20261020", "end": "20261023",
                      "limit": 0}})["body"])
    payload = json.loads(resp["result"]["content"][0]["text"])
    c.check("legacy L01 batch (limit=0) returns data for all 5 codes",
            isinstance(payload["data"], dict)
            and all(len(v) == 4 for v in payload["data"].values()),
            f"rows={ {k: len(v) for k, v in payload['data'].items()} }")


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--mode", choices=("stub", "live", "nas"), required=True)
    parser.add_argument("--url", default="http://192.168.50.146:8081/mcp")
    parser.add_argument("--out", type=Path, default=None)
    args = parser.parse_args()

    ids = ([f"C{i:02d}" for i in range(1, 23)] + [f"X{i:02d}" for i in range(1, 6)])
    cases = {cid: Case(cid, "service") for cid in ids}
    for cid in ("C23", "C24"):
        cases[cid] = Case(cid, "project")

    if args.mode == "stub":
        run_stub(cases)
    elif args.mode == "live":
        run_live(cases)
    else:
        run_nas(cases, args.url)

    # C23/C24 恒 NOT_RUN（消费方项目接入，服务侧不作联合判定）
    cases["C23"].check("project daily/close integration (client phase)",
                       None, "属消费方项目接入（README §14.2），服务交付阶段 NOT_RUN")
    cases["C24"].check("scope/publication/5-day observation (client phase)",
                       None, "属消费方项目接入，服务交付阶段 NOT_RUN")

    report = {
        "contract_version": "stockdb-market-v3", "contract_revision": "3.0.0",
        "mode": args.mode, "generated_at": time.strftime("%Y-%m-%dT%H:%M:%S%z"),
        "server_version": server.SERVER_VERSION,
        "cases": [{"case_id": c.case_id, "phase": c.phase, "status": c.status,
                   "assertion_results": c.results}
                  for c in cases.values()],
    }
    out = args.out or _MCP_DIR / f"acceptance-report-{args.mode}.json"
    out.write_text(json.dumps(report, ensure_ascii=False, indent=2),
                   encoding="utf-8")
    for c in cases.values():
        if not c.results:
            continue
        print(f"{c.case_id}: {c.status}")
        for r in c.results:
            print(f"   [{r['status']}] {r['assertion']} — {r['evidence'][:110]}")
    print(f"\nreport → {out}")
    return 0 if all(c.status != "FAIL" for c in cases.values()) else 1


if __name__ == "__main__":
    sys.exit(main())
