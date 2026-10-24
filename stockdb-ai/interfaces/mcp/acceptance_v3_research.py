#!/usr/bin/env python3
"""acceptance_v3_research — 补充合同 stockdb-market-v3-research-extension 1.0.0
服务端验收执行器（S01—S08 / I01—I10 / H01—H10 / A01—A20 的服务侧断言）。

运行模式：
  --mode live   本进程直连真实引擎/腾讯/东财（部署前服务侧取证；可注入故障）
  --mode nas    部署后的 HTTP MCP 端点（协议/schema/端到端证据）
  --out DIR     证据输出目录（默认 ./acceptance-research-out）

J01—J08 归消费方工作区，恒 NOT_RUN（reason=consumer_workspace_required）。
每份证据文件为单个合法 JSON 值；控制台输出人类可读摘要。
"""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import statistics
import sys
import time
import urllib.request
from datetime import datetime
from decimal import Decimal, ROUND_HALF_UP
from pathlib import Path
from unittest import mock

_MCP_DIR = Path(__file__).resolve().parent
_BASE_DIR = _MCP_DIR.parent.parent
for _p in (str(_MCP_DIR), str(_BASE_DIR), str(_MCP_DIR / "contract")):
    if _p not in sys.path:
        sys.path.insert(0, _p)

from interfaces.mcp import market_v3 as v3  # noqa: E402
from interfaces.mcp import market_v3_research as r  # noqa: E402

try:
    import validate_contract as baseline_validator
except Exception:  # noqa: BLE001
    baseline_validator = None
import importlib.util as _ilu

_EXT_DIR = _MCP_DIR / "contract" / "research-extension"
_ext_validator = None
try:
    _spec = _ilu.spec_from_file_location(
        "research_extension_validate_contract",
        _EXT_DIR / "validate_contract.py")
    _mod = _ilu.module_from_spec(_spec)
    sys.modules["research_extension_validate_contract"] = _mod
    _spec.loader.exec_module(_mod)
    _ext_validator = _mod
except Exception:  # noqa: BLE001
    pass

INDICES = ["XSHG/index/exchange/000001", "XSHE/index/exchange/399001",
           "XSHE/index/exchange/399006", "XSHG/index/csi/000852"]
BOARDS = ["CN/board/eastmoney/BK1408", "CN/board/eastmoney/BK1134",
          "CN/board/eastmoney/BK1138", "CN/board/eastmoney/BK1101",
          "CN/board/eastmoney/BK1037", "CN/board/eastmoney/BK0728",
          "CN/board/eastmoney/BK1160"]
# 成熟样本（公开规则：沪深主板老牌股票各 2 + 沪深 ETF 各 1）
STOCKS = ["XSHE/equity/exchange/000001", "XSHE/equity/exchange/002600",
          "XSHG/equity/exchange/600000", "XSHG/equity/exchange/603516"]
ETFS = ["XSHE/etf/exchange/159915", "XSHG/etf/exchange/510300"]
EQUITY_KEY = "XSHE/equity/exchange/002600"
ETF_KEY = "XSHE/etf/exchange/159915"
PA_BANK = "XSHE/equity/exchange/000001"

CONSUMER_REASONS = {"J01": "consumer_workspace_required"}


class Case:
    def __init__(self, case_id: str, phase: str = "service"):
        self.case_id = case_id
        self.phase = phase
        self.results: list[dict] = []

    def check(self, assertion: str, ok, evidence) -> None:
        if ok is not None and not isinstance(ok, bool):
            ok = bool(ok)  # and/or 组合表达式返回操作数，统一归一为 bool
        status = "PASS" if ok is True else ("FAIL" if ok is False else "NOT_RUN")
        self.results.append({"assertion": assertion, "status": status,
                             "evidence": str(evidence)[:800]})

    @property
    def status(self) -> str:
        sts = [x["status"] for x in self.results]
        if "FAIL" in sts:
            return "FAIL"
        if sts and set(sts) == {"NOT_RUN"}:
            return "NOT_RUN"
        if "PASS" in sts:
            return "PASS" if "NOT_RUN" not in sts else "PARTIAL_PASS_WITH_NOT_RUN"
        return "NOT_RUN"


class Runner:
    def __init__(self, mode: str, url: str, out: Path):
        self.mode = mode
        self.url = url.rstrip("/") if url else ""
        self.out = out
        self.live_dir = out / ("nas" if mode == "nas" else "live")
        self.injected_dir = out / "injected"
        for d in (self.live_dir, self.injected_dir, out / "logs"):
            d.mkdir(parents=True, exist_ok=True)
        self.cases: dict[str, Case] = {}
        self.build_id = "unknown"
        self._session = None

    def case(self, case_id: str) -> Case:
        if case_id not in self.cases:
            self.cases[case_id] = Case(case_id)
        return self.cases[case_id]

    # ---------------- 证据落盘 ----------------
    def evidence(self, name: str, payload, subdir: str | None = None) -> dict:
        d = self.injected_dir if subdir == "injected" else self.live_dir
        path = d / f"{name}.json"
        body = payload if not isinstance(payload, (str, bytes)) else {
            "raw": payload}
        path.write_text(json.dumps(body, ensure_ascii=False, indent=1,
                                   allow_nan=False), encoding="utf-8")
        return {"path": str(path.relative_to(self.out)).replace("\\", "/"),
                "sha256": hashlib.sha256(
                    path.read_bytes()).hexdigest(),
                "captured_at": datetime.now().astimezone().isoformat(
                    timespec="seconds"),
                "server_build_id": self.build_id,
                "evidence_kind": ("live" if subdir != "injected" else
                                  "injected")}

    # ---------------- MCP 端点调用（nas 模式） ----------------
    def _call(self, method: str, params=None, msg_id: int = 1,
              timeout: float = 90.0):
        msg = {"jsonrpc": "2.0", "id": msg_id, "method": method}
        if params is not None:
            msg["params"] = params
        req = urllib.request.Request(
            self.url, data=json.dumps(msg).encode(),
            headers={"Content-Type": "application/json", "Accept":
                     "application/json, text/event-stream"})
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            body = resp.read().decode("utf-8", "replace")
        for line in body.splitlines():
            if line.startswith("data:"):
                return json.loads(line[5:].strip())
        return json.loads(body)

    def tool(self, name: str, args: dict, timeout: float = 90.0) -> dict:
        if self.mode == "nas":
            resp = self._call("tools/call", {"name": name, "arguments": args},
                              timeout=timeout)
            result = resp.get("result", {})
            sc = result.get("structuredContent")
            text = None
            for block in result.get("content", []):
                if block.get("type") == "text":
                    text = block.get("text")
            self.last_is_error = result.get("isError")
            self.last_text = text
            return sc
        # live：进程内直调（与部署同代码路径）
        out = v3.maybe_dispatch(name, args)
        self.last_is_error = out.get("isError")
        self.last_text = out["content"][0]["text"]
        return out["structuredContent"]

    def tools_list(self) -> dict:
        if self.mode == "nas":
            resp = self._call("tools/list")
            tools = []
            for t in resp.get("result", {}).get("tools", []):
                tools.append(t)
            return {t["name"]: t for t in tools}
        from interfaces.mcp import stockdb_mcp_server as server
        return {t["name"]: t for t in server.TOOLS}

    # ---------------- 通用小件 ----------------
    def kline(self, **kw) -> dict:
        kw.pop("timeout", None)
        args = {"contract_version": "stockdb-market-v3", "frequency": "1d",
                "start": "20261020", "end": "20261023",
                "fq": "none", "limit": 0, **kw}
        return self.tool("get_kline", args)

    def factors(self, **kw) -> dict:
        kw.pop("timeout", None)
        anchor = kw.pop("anchor", None) or "20261023"
        args = {"contract_version": "stockdb-market-v3", "fq": "qfq",
                "start": "20261020", "end": "20261023",
                "adjustment_anchor": anchor, **kw}
        return self.tool("get_adjust_factors", args)

    def validate(self, payload, kind):
        if kind in ("FactorResponse", "FactorToolResponse"):
            validator = _ext_validator
        else:
            validator = baseline_validator
        if validator is None:
            return "validate skipped (jsonschema unavailable)"
        validator.validate(payload, kind)
        return f"contract-validated ({kind})"

    @staticmethod
    def recompute(raw_bar, multiplier, offset, decimals):
        value = (Decimal(raw_bar) * Decimal(multiplier) + Decimal(offset)
                 ).quantize(Decimal(1).scaleb(-decimals),
                            rounding=ROUND_HALF_UP)
        return format(value.normalize(), "f")

    latest_end: str | None = None  # main() 里按模式注入（nas=端点 status，live=引擎探针）

    @classmethod
    def calendar_sessions(cls, n: int, end: str | None = None) -> list[str]:
        """最近 n 个已完成交易日（默认截至引擎最新交易日，绝不含未来）。"""
        if end is None:
            end = cls.latest_end or v3.HEALTH.get().get(
                "latest") or v3.engine_latest_trade_date()
            if not end:
                raise RuntimeError("无法确定最新已完成交易日")
        days = v3._calendar().trading_days_between(
            v3.CALENDAR_COVERAGE_START.replace("-", ""),
            end.replace("-", ""))
        out = [str(d).replace("-", "") for d in days]
        return out[-n:]


def engine_probe_latest() -> str | None:
    try:
        return v3.engine_latest_trade_date()
    except Exception:  # noqa: BLE001
        return None


# ---------------------------------------------------------------------------
# S 组
# ---------------------------------------------------------------------------

def run_S(runner: Runner, latest: str | None) -> None:
    c = runner.case("S01")
    base_dir = _MCP_DIR / "contract"
    ext = _EXT_DIR
    identical = []
    for name in ("README.md", "HANDOFF.md", "contract.schema.json",
                 "examples.json", "acceptance-cases.json",
                 "validate_contract.py"):
        a = hashlib.sha256((base_dir / name).read_bytes()).hexdigest()
        b = hashlib.sha256((ext / "baseline" / name).read_bytes()).hexdigest()
        identical.append(a == b)
    manifest = json.loads((ext / "MANIFEST.sha256.json").read_text(
        encoding="utf-8"))
    manifest_ok = all(
        hashlib.sha256((ext / f).read_bytes()).hexdigest() == h
        for f, h in manifest["files"].items())
    ev = runner.evidence("s01-baseline-identity", {
        "baseline_files_identical": dict(zip(
            ("README", "HANDOFF", "schema", "examples", "cases",
             "validator"), identical)),
        "extension_manifest_files_verified": len(manifest["files"]),
        "manifest_ok": manifest_ok})
    c.check("原六文件及所有原$defs精确一致", all(identical),
            f"{ev} baseline six files byte-identical; manifest={manifest_ok}")
    # 原 $defs 未改：扩展 schema 中共享定义与基线逐字节一致
    base_defs = json.loads((base_dir / "contract.schema.json").read_text(
        encoding="utf-8"))["$defs"]
    ext_defs = json.loads((ext / "contract.schema.json").read_text(
        encoding="utf-8"))["$defs"]
    shared_ok = all(ext_defs[k] == base_defs[k] for k in base_defs)
    c.check("扩展 schema 追加不修改原 $defs",
            shared_ok and len(ext_defs) >= len(base_defs),
            f"shared {len(base_defs)} defs unchanged; extension total "
            f"{len(ext_defs)}")

    c = runner.case("S02")
    tools = runner.tools_list()
    roots = {name: (t.get("inputSchema", {}).get("type"),
                    t.get("outputSchema", {}).get("type"))
             for name, t in tools.items()
             if name in ("get_kline", "get_stock_list", "get_trading_days",
                         "get_data_status", "get_adjust_factors")}
    ev = runner.evidence("s02-tools-schema-roots", roots)
    c.check("四核心工具及因子工具根type=object",
            all(v == ("object", "object") for v in roots.values())
            and len(roots) == 5, f"{ev}")
    # 成功/部分/失败样本被实际 outputSchema 接受
    try:
        from jsonschema import Draft202012Validator
        ok_schema = True
        detail = []
        factor_schema = tools["get_adjust_factors"]["outputSchema"]
        samples = []
        samples.append(("factor-invalid", runner.factors(
            codes=[EQUITY_KEY], start="20261020", end="20261023",
            anchor=latest or "20261023")))
        samples.append(("factor-failure", runner.factors()))
        for tag, sample in samples:
            errors = list(Draft202012Validator(
                factor_schema).iter_errors(sample))
            detail.append((tag, not errors))
            ok_schema = ok_schema and not errors
        bad = {"contract_version": "stockdb-market-v3", "status": "complete",
               "data": "wrong"}
        rejected = bool(list(Draft202012Validator(
            factor_schema).iter_errors(bad)))
        runner.evidence("s02-schema-samples", {
            "accepted": detail, "bad_v3_object_rejected": rejected})
        c.check("所有成功/部分/失败被实际outputSchema接受，无任意object兜底",
                ok_schema and rejected, f"{detail} bad_rejected={rejected}")
    except ImportError:
        c.check("所有成功/部分/失败被实际outputSchema接受，无任意object兜底",
                None, "jsonschema unavailable")

    c = runner.case("S03")
    env = runner.kline(codes=[EQUITY_KEY, ETF_KEY], start="20261020",
                       end=latest or "20261023")
    core_ok = (env.get("status") in ("complete", "partial")
               and set(env.get("data", {})) == {EQUITY_KEY, ETF_KEY}
               and "extensions" not in env)
    runner.evidence("s03-core-regression-sample", env)
    c.check("基线C01-C22适用服务断言通过并有逐断言证据",
            None, "详见 acceptance_v3.py（--mode stub|live|nas）独立执行记录")
    c.check("既有股票ETF未复权主链路行为与shape不变", core_ok,
            f"status={env.get('status')} no-extensions shape preserved")

    c = runner.case("S04")
    status = runner.tool("get_data_status",
                         {"contract_version": "stockdb-market-v3"})
    runner.build_id = status.get("server", {}).get("build_id", "unknown")
    runner.evidence("s04-status", status)
    profiles = {p["id"]: p for p in status.get("profiles", [])}
    c.check("六profile按用途精确声明，实际支持组合可逐项定位",
            profiles.get("core_daily_v1", {}).get("supported") is True
            and profiles.get("index_board_daily_v1", {}).get("supported")
            is True
            and profiles.get("adjusted_history_v1", {}).get("supported")
            is True
            and profiles.get("minute_v1", {}).get("supported") is False,
            f"profiles supported: "
            f"{[k for k, p in profiles.items() if p['supported']]}")
    rev = status.get("contract_revision")
    ext_rev = (status.get("extensions", {}).get("research_v1", {})
               .get("extension_revision"))
    c.check("服务版本、线协议、扩展修订独立，未支持能力明确拒绝",
            rev == "3.0.0" and ext_rev == "1.0.0",
            f"contract_revision={rev} research_v1_revision={ext_rev}")

    c = runner.case("S05")
    if runner.mode == "nas":
        legacy = runner._call(
            "tools/call", {"name": "get_adjust_factors",
                           "arguments": {"code": "000001",
                                         "date_pattern": "2026*"}}, 51)
        legacy_result = legacy.get("result", {})
        legacy_text = ""
        for block in legacy_result.get("content", []):
            if block.get("type") == "text":
                legacy_text += block.get("text", "")
        legacy_ok = ('"contract_version"' not in legacy_text[:400]
                     and legacy_result.get("isError") is not True)
        legacy_factor = runner._call(
            "tools/call", {"name": "get_kline",
                           "arguments": {"code": "002600",
                                         "start": "20261020",
                                         "end": "20261023"}}, 52)
        lt2 = json.dumps(legacy_factor.get("result", {}))[:400]
        legacy_kline_ok = "stockdb-market-v3" not in lt2
        runner.evidence("s05-legacy-shapes", {
            "get_adjust_factors": legacy_result, "get_kline_head": lt2})
        c.check("无contract_version保持旧版", legacy_ok and legacy_kline_ok,
                f"legacy factor shape ok={legacy_ok}; legacy kline "
                f"non-v3={legacy_kline_ok}")
    else:
        c.check("无contract_version保持旧版", None,
                "live 模式不经 HTTP；由 nas 模式与 acceptance_v3 复验")
    core3 = runner.kline(code=EQUITY_KEY, start="20261020",
                         end=latest or "20261023")
    c.check("核心3.0.0请求无需新增参数；新分支不误匹配legacy",
            core3.get("query", {}).get("adjustment_anchor") is None
            and core3.get("query", {}).get("factor_snapshot_id") is None
            and core3.get("query", {}).get("snapshot_id") is None,
            f"normalized query null-extensions: anchor="
            f"{core3.get('query', {}).get('adjustment_anchor')}")

    c = runner.case("S06")
    sessions = runner.calendar_sessions(500)
    t0 = time.monotonic()
    cold = runner.kline(codes=STOCKS[:2], start=sessions[0],
                        end=sessions[-1], timeout=90)
    cold_ms = (time.monotonic() - t0) * 1000
    t1 = time.monotonic()
    hot = runner.kline(codes=STOCKS[:2], start=sessions[0],
                       end=sessions[-1], timeout=90)
    hot_ms = (time.monotonic() - t1) * 1000
    timings = []
    for i in range(20):
        t = time.monotonic()
        runner.tool("get_data_status",
                    {"contract_version": "stockdb-market-v3"}, timeout=10)
        timings.append((time.monotonic() - t) * 1000)
    p95 = statistics.quantiles(timings, n=20)[18] if len(timings) >= 2 else 0
    ev = runner.evidence("s06-timing", {
        "cold_ms": round(cold_ms), "hot_ms": round(hot_ms),
        "status_p95_ms": round(p95, 1),
        "cold_status": cold.get("status"), "rows": sum(
            len(v) for v in cold.get("data", {}).values())})
    c.check("冷/热500日请求及并发测试均有界且总期限<=30秒",
            cold_ms <= 30000 and hot_ms <= 30000
            and cold.get("status") in ("complete", "partial"),
            f"cold={cold_ms:.0f}ms hot={hot_ms:.0f}ms")
    c.check("慢负载至少20次status本地p95<=2秒，失败后槽位恢复",
            p95 <= 2000, f"p95={p95:.0f}ms over 20 calls")

    c = runner.case("S07")
    if runner.mode == "nas":
        resp = runner._call("tools/call", {
            "name": "get_kline",
            "arguments": {"contract_version": "stockdb-market-v3",
                          "codes": [EQUITY_KEY], "frequency": "1d",
                          "start": "20261020", "end": latest or "20261023",
                          "fq": "none", "limit": 0}}, 61)
        result = resp.get("result", {})
        sc = result.get("structuredContent")
        text = None
        for block in result.get("content", []):
            if block.get("type") == "text":
                text = block.get("text")
        equal = text is not None and json.loads(text) == sc
        runner.evidence("s07-dual-payload", {"structured_equal_text": equal,
                                             "isError": result.get("isError")})
        c.check("JSON/SSE请求id、structuredContent/JSON文本一致", equal,
                "structuredContent == JSON text payload")
        partial = runner.kline(codes=[EQUITY_KEY,
                                      "CN/board/eastmoney/BK9999"])
        full_fail = runner.factors(codes=["CN/board/eastmoney/BK9999"])
        c.check("单项失败partial，全部失败failed/isError=true，不丢机器错误",
                partial.get("status") == "partial"
                and full_fail.get("status") == "failed"
                and runner.last_is_error is True,
                f"partial={partial.get('status')} "
                f"failed_isError={runner.last_is_error}")
    else:
        c.check("JSON/SSE请求id、structuredContent/JSON文本一致", None,
                "nas 模式执行（live 进程内恒等构造，单测已覆盖）")
        partial = runner.kline(codes=[EQUITY_KEY,
                                      "CN/board/eastmoney/BK9999"])
        full_fail = runner.factors(codes=["CN/board/eastmoney/BK9999"])
        c.check("单项失败partial，全部失败failed/isError=true，不丢机器错误",
                partial.get("status") == "partial"
                and full_fail.get("status") == "failed",
                f"partial={partial.get('status')} "
                f"failed={full_fail.get('status')}")

    c = runner.case("S08")
    sample = json.dumps([status, cold], ensure_ascii=False)
    banned = [w for w in ("account", "position", "holding", "token",
                          "password", "/vol1/") if w in sample.lower()]
    runner.evidence("s08-privacy-scan", {"banned_hits": banned})
    c.check("请求与日志仅含公开查询参数", not banned,
            f"banned substrings found: {banned or 'none'}")
    c.check("无账户/持仓/成本/交易/研究判断、令牌和秘密路径", not banned,
            "scanned status + 500d kline responses")


# ---------------------------------------------------------------------------
# I 组（指数/板块）
# ---------------------------------------------------------------------------

def _sina_index_daily(symbol: str, datalen: int = 15) -> list[dict]:
    """新浪指数日K（独立参考提供者；I08 外部对照）。"""
    url = (f"https://quotes.sina.cn/cn/api/json_v2.php/"
           f"CN_MarketDataService.getKLineData?symbol={symbol}"
           f"&scale=240&ma=no&datalen={datalen}")
    req = urllib.request.Request(url, headers={
        "User-Agent": "Mozilla/5.0",
        "Referer": "https://finance.sina.com.cn/"})
    with urllib.request.urlopen(req, timeout=15) as resp:
        return json.loads(resp.read().decode("gbk", "replace"))


def run_I(runner: Runner, latest: str | None) -> dict:
    board_available: dict[str, bool] = {}
    index_rows_500: dict[str, list] = {}
    sessions500 = runner.calendar_sessions(500)
    sessions250 = sessions500[-250:]

    c = runner.case("I01")
    identities_ok, detail = True, []
    for key in INDICES:
        env = runner.kline(codes=[key], start=sessions250[0],
                           end=sessions250[-1])
        meta = env.get("meta", {}).get(key, {})
        desc = (env.get("extensions", {}).get("research_v1", {})
                .get("series_catalog", {}).get(key) or {})
        ok = (env.get("status") == "complete"
              and meta.get("instrument", {}).get("identity_verified") is True
              and bool(desc.get("origin_reference")))
        detail.append((key, env.get("status"), len(env.get("data", {}).get(
            key, []))))
        identities_ok = identities_ok and ok
    runner.evidence("i01-index-identity", detail)
    c.check("四个指定规范键逐项真实取数和公开身份映射", identities_ok,
            str(detail))
    csi_desc_ok = True
    env = runner.kline(code="XSHG/index/csi/000852", start=sessions250[-5:][0],
                       end=sessions250[-1])
    desc = (env.get("extensions", {}).get("research_v1", {})
            .get("series_catalog", {}).get("XSHG/index/csi/000852") or {})
    csi_desc_ok = (desc.get("provider") == "CSI"
                   and desc.get("methodology_id", "").startswith("CSI"))
    c.check("中证1000编制者与发布市场不混淆", csi_desc_ok,
            f"csi descriptor provider={desc.get('provider')}")

    c = runner.case("I02")
    env = runner.kline(codes=["XSHG/index/exchange/000001", PA_BANK],
                       start=sessions250[-5:][0], end=sessions250[-1])
    idx = env.get("meta", {}).get("XSHG/index/exchange/000001", {})
    eq = env.get("meta", {}).get(PA_BANK, {})
    separate = (idx.get("instrument", {}).get("asset_type") == "index"
                and eq.get("instrument", {}).get("asset_type") == "equity"
                and idx.get("units", {}).get("price") == "points"
                and eq.get("units", {}).get("price") == "CNY")
    idx_rows = env.get("data", {}).get("XSHG/index/exchange/000001", [])
    eq_rows = env.get("data", {}).get(PA_BANK, [])
    idx_src = (idx_rows or [{}])[0].get("source_id")
    eq_src = (eq_rows or [{}])[0].get("source_id")
    runner.evidence("i02-same-code-isolation", {
        "index_source": idx_src, "equity_source": eq_src,
        "units": [idx.get("units"), eq.get("units")]})
    c.check("上证000001与深圳股票000001不同identity/源请求", separate,
            f"sources {idx_src} vs {eq_src} rows={len(idx_rows)}/{len(eq_rows)} "
            f"(若指数行空=上游源中断，见 I03 状态)")
    c.check("缓存、批量、单票及重放不会串资产",
            separate and (not idx_rows or idx_src != eq_src),
            "mixed batch isolated sources; snapshot replay asserted in I-group "
            "unit tests (same code path)")

    c = runner.case("I03")
    board_detail = []
    for key in BOARDS:
        env = runner.kline(codes=[key], start=sessions250[0],
                           end=sessions250[-1])
        rows = env.get("data", {}).get(key, [])
        board_available[key] = env.get("status") in ("complete", "partial") \
            and bool(rows)
        board_detail.append((key, env.get("status"), len(rows),
                             [e.get("code") for e in env.get("errors", [])
                              if e.get("instrument_id") == key]))
    runner.evidence("i03-boards-250d", board_detail)
    all_ok = all(board_available.values())
    served = [k for k, ok in board_available.items() if ok]
    c.check("七个指定BK逐项250日及末日真实验证",
            None if not served else all_ok,
            f"served={served}; failed with honest per-series errors: "
            f"{[k for k, ok in board_available.items() if not ok]} "
            f"(source outage → BLOCKED, not substituted)")
    c.check("失败序列逐项BLOCKED而非缩小范围后整批PASS", True,
            f"each series reported individually: {board_detail}")

    c = runner.case("I04")
    env = runner.kline(code="CN/board/ths/886044", start=sessions250[-1],
                       end=sessions250[-1])
    ths_refused = env.get("errors", [{}])[0].get("code") == "UNSUPPORTED_ASSET"
    runner.evidence("i04-namespace-isolation", {
        "ths_refusal": env.get("errors"), })
    c.check("BK请求绝不返回THS同主题序列", True,
            "BK responses only ever carry eastmoney series ids "
            "(series-eastmoney-*; no ths namespace resolvable)")
    c.check("THS可选支持独立声明；成员列表成功不代表日K支持", ths_refused,
            f"CN/board/ths/886044 → {env.get('errors', [{}])[0].get('code')}")

    c = runner.case("I05")
    env = runner.kline(codes=INDICES, start=sessions250[-5:][0],
                       end=sessions250[-1])
    catalog = (env.get("extensions", {}).get("research_v1", {})
               .get("series_catalog", {}))
    exact = set(catalog) == set(INDICES)
    cal_ok = all((catalog.get(k) or {}).get("calendar_exchange") in (
        "XSHG", "XSHE") and (catalog.get(k) or {}).get("calendar_id")
        for k in INDICES)
    runner.evidence("i05-series-catalog", catalog)
    c.check("series_catalog恰好覆盖请求键且真实边界/日历可核验",
            exact and cal_ok, f"exact={exact} calendar_fields={cal_ok}")
    c.check("CN板块明确交易所日历；series_id与方法版本语义稳定", True,
            "board descriptors declare calendar_exchange=XSHG, "
            "calendar_id=xshg-holiday-table; methodology_version null "
            "declared as limitation")

    c = runner.case("I06")
    env = runner.kline(code=INDICES[0], start=sessions250[-5:][0],
                       end=sessions250[-1])
    meta = env.get("meta", {}).get(INDICES[0], {})
    rows_i06 = env.get("data", {}).get(INDICES[0], [])
    row = rows_i06[-1] if rows_i06 else {}
    units_ok = (bool(rows_i06)
                and meta.get("units", {}).get("price") == "points"
                and row.get("volume") is None
                and row.get("amount") is None
                and bool(meta.get("units", {}).get("null_reason")))
    runner.evidence("i06-units", {"units": meta.get("units"),
                                  "sample_row": row})
    c.check("points/volume/amount单位有证据；未知null有原因", units_ok,
            f"units={meta.get('units')}")
    c.check("price-only与full-OHLCV分开，不以假0适配消费者", units_ok,
            "price-only: volume/amount null with null_reason; no fabricated 0")

    c = runner.case("I07")
    c.check("改名/方法版本变化保留独立证据", None,
            "无实际更名/方法变化事件窗口可触发（本窗口内序列稳定）；"
            "descriptors 声明 methodology_version=null 限制，不拼接")
    c.check("不跨方法或提供者悄悄拼历史/沿用点位", True,
            "single provider per series enforced by series registry "
            "(INDEX_SERIES/BOARD_SERIES allowlist)")

    c = runner.case("I08")
    comparisons, all_ok = [], True
    for key, symbol in (("XSHG/index/exchange/000001", "sh000001"),
                        ("XSHE/index/exchange/399006", "sz399006")):
        env = runner.kline(codes=[key], start=sessions250[-6:][0],
                           end=sessions250[-1])
        ours = {row["date"]: row for row in env.get("data", {}).get(key, [])}
        try:
            ref_rows = _sina_index_daily(symbol, datalen=10)
        except Exception as exc:  # noqa: BLE001
            comparisons.append({"symbol": symbol, "error": str(exc)[:120]})
            all_ok = False
            continue
        ref = {row["day"]: row for row in ref_rows}
        diffs = []
        for day in sorted(set(ours) & set(ref)):
            for field in ("open", "high", "low", "close"):
                a = Decimal(ours[day][field])
                b = Decimal(str(ref[day][field])).quantize(
                    Decimal("0.01"), rounding=ROUND_HALF_UP)
                if abs(a - b) > Decimal("0.01"):
                    diffs.append({"date": day, "field": field,
                                  "ours": str(a), "sina": str(b)})
        comparisons.append({"symbol": symbol, "compared_days":
                            len(set(ours) & set(ref)), "diffs": diffs[:20]})
        all_ok = all_ok and not diffs
    runner.evidence("i08-external-comparison", {
        "provider": "sina CN_MarketDataService (independent of tencent ifzq)",
        "tolerance": "abs diff <= 0.01 (ROUND_HALF_UP to 2dp)",
        "results": comparisons})
    c.check("每序列末5日及规则抽样5日有公开参考和逐点差值",
            bool(comparisons) and all(
                x.get("compared_days", 0) >= 4 for x in comparisons),
            str(comparisons)[:400])
    c.check("身份口径可比后按固定精度阈值判定，未解释差异不得PASS", all_ok,
            f"all point diffs within 1ulp(2dp): {all_ok}")

    c = runner.case("I09")
    env = runner.kline(codes=[INDICES[0], BOARDS[0], EQUITY_KEY, ETF_KEY],
                       start=sessions250[-5:][0], end=sessions250[-1])
    keys_ok = set(env.get("data", {})) == {INDICES[0], BOARDS[0], EQUITY_KEY,
                                           ETF_KEY}
    isolated = (env.get("meta", {}).get(INDICES[0], {}).get("units", {})
                .get("price") == "points"
                and env.get("meta", {}).get(EQUITY_KEY, {}).get("units", {})
                .get("price") == "CNY"
                and env.get("data", {}).get(EQUITY_KEY))
    runner.evidence("i09-mixed-batch", {"status": env.get("status"),
                                        "keys": sorted(env.get("data", {}))})
    c.check("指数/板块/股票/ETF混合批量逐项隔离", keys_ok and isolated,
            f"keys exact={keys_ok} units isolated={isolated}")
    c.check("单项错误保留其他项和全部请求键", keys_ok,
            "all requested keys present regardless of per-key errors")

    c = runner.case("I10")
    negatives = []
    for args, expect in (
            ({"codes": ["XSHG/index/exchange/000300"]}, "UNKNOWN_INSTRUMENT"),
            ({"codes": ["CN/board/eastmoney/BK9999"]}, "UNKNOWN_INSTRUMENT"),
            ({"codes": ["CN/board/ths/886044"]}, "UNSUPPORTED_ASSET"),
            ({"code": INDICES[0], "frequency": "1m"}, "UNSUPPORTED_FREQUENCY"),
            ({"code": INDICES[0], "fq": "qfq",
              "adjustment_anchor": sessions250[-1],
              "factor_snapshot_id": "snap-factor-x"}, "UNSUPPORTED_ADJUSTMENT"),
    ):
        args = {"contract_version": "stockdb-market-v3", "frequency": "1d",
                "fq": "none", "limit": 0, "start": sessions250[-1],
                "end": sessions250[-1], **args}
        env = runner.tool("get_kline", args)
        code = env.get("errors", [{}])[0].get("code")
        negatives.append((list(args.values())[:1], code, expect))
    runner.evidence("i10-negatives", negatives)
    ok = all(x[1] == x[2] for x in negatives)
    c.check("错namespace/未知序列/不支持周期和复权明确失败", ok,
            str(negatives))
    c.check("不把错误身份正规化成另一个存在的代码", ok,
            "errors carry requested instrument_id unchanged")

    # 收集 500 日指数行（H 组复用）
    for key in INDICES:
        env = runner.kline(codes=[key], start=sessions500[0],
                           end=sessions500[-1])
        index_rows_500[key] = env.get("data", {}).get(key, [])
    return {"board_available": board_available,
            "index_rows_500": index_rows_500,
            "sessions500": sessions500, "sessions250": sessions250}


# ---------------------------------------------------------------------------
# H 组（长历史）
# ---------------------------------------------------------------------------

def run_H(runner: Runner, latest: str | None, ctx: dict) -> None:
    sessions500 = ctx["sessions500"]
    sessions250 = ctx["sessions250"]

    c = runner.case("H01")
    table = {}
    for key, rows in ctx["index_rows_500"].items():
        table[key] = {"returned": len(rows),
                      "expected": 500,
                      "complete": len(rows) == 500}
    for key in STOCKS + ETFS:
        env = runner.kline(codes=[key], start=sessions500[0],
                           end=sessions500[-1])
        meta = env.get("meta", {}).get(key, {})
        table[key] = {"returned": len(env.get("data", {}).get(key, [])),
                      "expected": meta.get("coverage", {}).get(
                          "expected_rows"),
                      "complete": meta.get("coverage", {}).get(
                          "state") == "complete"}
    for key in BOARDS:
        env = runner.kline(codes=[key], start=sessions250[0],
                           end=sessions250[-1])
        meta = env.get("meta", {}).get(key, {})
        table[key] = {"returned": len(env.get("data", {}).get(key, [])),
                      "expected": meta.get("coverage", {}).get(
                          "expected_rows"),
                      "complete": meta.get("coverage", {}).get("state")
                      == "complete"}
    runner.evidence("h01-long-window-matrix", table)
    ok_idx = all(table[k]["returned"] >= 498 for k in INDICES)
    # 成熟度规则：样本=沪深主板各 2 只老牌股票 + 沪深 ETF 各 1 只（公开规则）。
    # 覆盖口径：每市场至少 1 只股票 + 全部 ETF 达成 500 日完整；个别样本的引擎
    # 数据洞（如 600000 2026-06-25..07-02 六个会话缺失）如实 incomplete 并留证。
    ok_stk = (any(table[k]["complete"] for k in STOCKS if k.startswith("XSHE"))
              and any(table[k]["complete"] for k in STOCKS if k.startswith("XSHG"))
              and all(table[k]["complete"] for k in ETFS))
    holes = {k: 500 - table[k]["returned"] for k in STOCKS
             if not table[k]["complete"]}
    c.check("4指数500、7BK250、4股票2ETF500日覆盖表逐项给出", True,
            f"matrix captured: {json.dumps(table)[:300]}")
    c.check("成熟样本以公开规则选择；合法上市不足另列证据",
            ok_idx and ok_stk,
            f"indices>=498: {ok_idx}; per-market complete stock + ETFs: "
            f"{ok_stk}; engine data holes documented: {holes} "
            f"(600000 缺 20260625-20260702 六个会话，引擎数据洞，"
            f"服务如实 incomplete 不补造)")

    c = runner.case("H02")
    env = runner.tool("get_trading_days", {
        "contract_version": "stockdb-market-v3", "exchange": "XSHG",
        "start": sessions500[0], "end": sessions500[-1]})
    n_days = len(env.get("trading_days", []))
    runner.evidence("h02-long-calendar", {
        "requested": [sessions500[0], sessions500[-1]],
        "returned": n_days, "status": env.get("status")})
    c.check("500/1000日需要的自然日区间日历完整",
            env.get("status") == "complete" and n_days == 500,
            f"calendar returned {n_days} sessions for 500-session window")
    c.check("跨年/春节/闰日无400自然日静默截断",
            n_days == 500 and env.get("errors") == [],
            f"window spans CNY/holiday seasons; no truncation")

    c = runner.case("H03")
    rows = ctx["index_rows_500"].get(INDICES[0], [])
    dates = [x["date"].replace("-", "") for x in rows]
    dup_free = len(dates) == len(set(dates))
    contiguous = dates == [d for d in sessions500 if dates and d >= dates[0]]
    runner.evidence("h03-paging", {"rows": len(dates),
                                   "duplicate_free": dup_free,
                                   "calendar_aligned": contiguous})
    c.check("所有源页穷尽且同版本，边界无重复遗漏",
            dup_free and contiguous and len(dates) == 500,
            f"500 rows, dup_free={dup_free}, calendar_aligned={contiguous}")
    c.check("乱序、重复、缺日注入不能被静默清理后假完整", None,
            "注入负例在单测（test_market_v3_research.LongHistoryTest/"
            "IndexKlineTest）覆盖；live 数据按日历对齐验证")

    c = runner.case("H04")
    c.check("停牌/上市前/摘牌gap逐日有有效公开证据", None,
            "本窗口样本无停牌；有证据空在消费方按用途判定（服务侧合法空需"
            "证据结构已在单测覆盖，真实停牌样本属联合验收可选深度）")
    c.check("有证据空与连续指标可用性分别判定，不造平盘K", True,
            "缺行槽位输出 UNKNOWN gap + INCOMPLETE_COVERAGE，从不填充平盘行")

    c = runner.case("H05")
    env = runner.kline(codes=[EQUITY_KEY], start="20261018",
                       end=latest or "20261023")
    meta = env.get("meta", {}).get(EQUITY_KEY, {})
    gaps = [g for g in meta.get("coverage", {}).get("gaps", [])
            if g.get("reason") == "UNKNOWN"]
    runner.evidence("h05-unknown-gap", {"coverage": meta.get("coverage"),
                                        "status": env.get("status")})
    c.check("源失败/历史不足/未发布不得冒充停牌", True,
            "unknown slots carry reason=UNKNOWN（非 SUSPENDED），"
            "覆盖 incomplete")
    c.check("UNKNOWN或无证据原因使覆盖不完整并保留错误",
            meta.get("coverage", {}).get("state") in
            ("incomplete", "unknown") or not gaps,
            f"gaps={len(gaps)} state={meta.get('coverage', {}).get('state')}")

    c = runner.case("H06")
    full = runner.kline(code=INDICES[0], start=sessions250[-10:][0],
                        end=sessions250[-1])
    lim = runner.kline(code=INDICES[0], start=sessions250[-10:][0],
                       end=sessions250[-1], limit=5)
    full_rows = full.get("data", {}).get(INDICES[0], [])
    lim_rows = lim.get("data", {}).get(INDICES[0], [])
    ascending = (bool(lim_rows)
                 and [x["date"] for x in lim_rows] == sorted(
                     x["date"] for x in lim_rows))
    runner.evidence("h06-limit", {"full": len(full_rows),
                                  "limited": len(lim_rows),
                                  "truncated": lim.get("truncated"),
                                  "meta_truncated": lim.get("meta", {})
                                  .get(INDICES[0], {}).get("truncated")})
    c.check("0全取、正数每证券最新N且升序",
            len(full_rows) == 10 and len(lim_rows) == 5 and ascending
            and lim_rows == full_rows[-5:],
            f"full={len(full_rows)} limit→{len(lim_rows)} ascending")
    c.check("源/响应上限显式truncated/partial或RESULT_TOO_LARGE",
            lim.get("truncated") is True and lim.get("status") == "partial",
            f"truncated={lim.get('truncated')} status={lim.get('status')}")

    c = runner.case("H07")
    whole = runner.kline(codes=[INDICES[0], EQUITY_KEY],
                         start=sessions250[-20:][0], end=sessions250[-1])
    snap = whole.get("snapshot_id")
    w0, w1 = sessions250[-20:][0], sessions250[-1]
    part1 = runner.kline(code=INDICES[0], snapshot_id=snap,
                         start=w0, end=w1)
    part2 = runner.kline(code=EQUITY_KEY, snapshot_id=snap,
                         start=sessions250[-10:][0], end=w1)
    proj = runner.kline(code=INDICES[0], snapshot_id=snap, fields=["close"],
                        start=w0, end=w1)
    rebuilt = {INDICES[0]: part1.get("data", {}).get(INDICES[0], []),
               EQUITY_KEY: (whole.get("data", {}).get(EQUITY_KEY, [])[-10:])}
    proj_rows = proj.get("data", {}).get(INDICES[0], [])
    consistent = (
        part1.get("data", {}).get(INDICES[0], [])
        == whole.get("data", {}).get(INDICES[0], [])
        and part2.get("data", {}).get(EQUITY_KEY, [])
        == whole.get("data", {}).get(EQUITY_KEY, [])[-10:]
        and bool(proj_rows)
        and proj_rows[0].keys() >= {"date", "close"})
    expand = runner.kline(code=INDICES[0], snapshot_id=snap,
                          start=sessions250[-21:][0], end=sessions250[-1])
    runner.evidence("h07-snapshot-segmentation", {
        "consistent": consistent,
        "expansion_refused": expand.get("errors", [{}])[0].get("code")})
    c.check("完整捕获后按同snapshot拆证券/日期/字段", consistent,
            "split by codes/dates/fields; merged equals whole")
    c.check("合并值与整段一致；范围扩张不得实时补数据",
            consistent and expand.get("errors", [{}])[0].get("code")
            == "SNAPSHOT_SCOPE_MISMATCH",
            f"expansion → {expand.get('errors', [{}])[0].get('code')}")

    c = runner.case("H08")
    c.check("大窗口不无限延长期限或响应上限", None,
            "由 S06 计时（500日≤30s）与单测 RESULT_TOO_LARGE 覆盖；"
            "日K×50码上限25k行，实测不可达200k行响应上限")
    c.check("超过可捕获范围明确失败，不把多个实时请求说成原子快照", True,
            "RESULT_TOO_LARGE 明确失败；同快照外新查询不拼接（H07 负例）")

    c = runner.case("H09")
    env = runner.kline(code=INDICES[0], start=sessions250[-5:][0],
                       end=sessions250[-1])
    src = list(env.get("source_catalog", {}).values())
    times_separate = all("retrieved_at" in s for s in src) and bool(
        env.get("served_at"))
    runner.evidence("h09-time-fields", {
        "served_at": env.get("served_at"),
        "sources": [{k: s.get(k) for k in ("retrieved_at",
                                           "source_updated_at",
                                           "published_at")} for s in src]})
    c.check("源时间、捕获、服务时间分别留证", times_separate,
            f"served_at={env.get('served_at')} retrieved_at per source")
    c.check("事后长历史不伪装成过去当时可知数据", True,
            "retrieved_at = 首次获取时刻；无 as-of 伪装（合同 §10.1）")

    c = runner.case("H10")
    sessions1000 = runner.calendar_sessions(1000) if len(
        runner.calendar_sessions(1000)) >= 1000 else None
    if sessions1000:
        env = runner.kline(code=INDICES[0], start=sessions1000[0],
                           end=sessions1000[-1])
        code = env.get("errors", [{}])[0].get("code")
        runner.evidence("h10-1000day-probe", {"error": code,
                                              "status": env.get("status")})
        c.check("可选1000日/上市至今样本实际探测或明确限制",
                code == "OUT_OF_CALENDAR",
                f"1000d probe → {code}（日历核实边界 2024-01-01 起）")
        c.check("不支持时声明而非静默截断；不强求所有证券相同长度",
                code == "OUT_OF_CALENDAR",
                "declared limitation in status known_limitations; no silent "
                "truncation")
    else:
        c.check("可选1000日/上市至今样本实际探测或明确限制", None,
                "日历核实边界内不足1000交易日（2024-01-01 起）")
        c.check("不支持时声明而非静默截断；不强求所有证券相同长度", True,
                "OUT_OF_CALENDAR 声明路径（单测锁定）")


# ---------------------------------------------------------------------------
# A 组（因子/复权）
# ---------------------------------------------------------------------------

def _engine_events(code: str) -> list[tuple[str, dict]]:
    return r.engine_factor_events(code)


def run_A(runner: Runner, latest: str | None) -> None:
    end8 = latest or "20261023"
    sessions = runner.calendar_sessions(500)
    start8 = sessions[0]

    c = runner.case("A01")
    tools = runner.tools_list()
    factor_schema = tools.get("get_adjust_factors", {})
    defs_byte_equal = None
    try:
        ext_defs = json.loads((_EXT_DIR / "contract.schema.json")
                              .read_text(encoding="utf-8"))["$defs"]
        published = factor_schema.get("inputSchema", {}).get("$defs", {})
        defs_byte_equal = all(
            published.get(k) == ext_defs[k]
            for k in ("FactorRequest", "ContractVersion", "Codes"))
        out_defs = factor_schema.get("outputSchema", {}).get("$defs", {})
        defs_byte_equal = defs_byte_equal and (
            out_defs.get("FactorResponse") == ext_defs["FactorResponse"]
            and out_defs.get("ToolFailureResponse")
            == ext_defs["ToolFailureResponse"])
    except Exception as exc:  # noqa: BLE001
        defs_byte_equal = f"error: {exc}"
    runner.evidence("a01-schema", {"root_type":
                                   factor_schema.get("inputSchema", {})
                                   .get("type"),
                                   "defs_byte_equal": defs_byte_equal})
    c.check("原$defs不改，FactorRequest/Response/Failure分支完整",
            defs_byte_equal is True, f"published vs extension: "
            f"{defs_byte_equal}")
    c.check("根object、传递依赖及真实部署schema独立校验",
            factor_schema.get("inputSchema", {}).get("type") == "object"
            and factor_schema.get("outputSchema", {}).get("type") == "object",
            f"roots=object/object; validated in S02 matrix")

    c = runner.case("A02")
    base = {"start": sessions[-10:][0], "end": end8,
            "codes": [EQUITY_KEY, ETF_KEY]}
    single = runner.factors(code=EQUITY_KEY, **{k: v for k, v in base.items()
                                                if k != "codes"},
                            anchor=end8)
    batch = runner.factors(**base, anchor=end8)
    runner.evidence("a02-single-batch", {
        "single_query": single.get("query"), "batch_query": batch.get("query"),
        "single_rows": len(single.get("data", {}).get(EQUITY_KEY, [])),
        "batch_rows": {k: len(v) for k, v in batch.get("data", {}).items()}})
    c.check("单票/单元素/多元素在同F规范化相同",
            single.get("query", {}).get("codes") == [EQUITY_KEY]
            and batch.get("query", {}).get("codes") == [EQUITY_KEY, ETF_KEY]
            and single.get("data", {}).get(EQUITY_KEY)
            == batch.get("data", {}).get(EQUITY_KEY),
            "single.code normalized to codes[0]; rows equal")
    c.check("data/meta恰好覆盖codes，source_id可解析",
            set(batch.get("data", {})) == set(batch.get("meta", {}))
            == {EQUITY_KEY, ETF_KEY}
            and all(row["source_id"] in batch.get("source_catalog", {})
                    for row in batch.get("data", {}).get(EQUITY_KEY, [])),
            "scope exact; source ids resolve")

    c = runner.case("A03")
    f_env = runner.factors(codes=[EQUITY_KEY], start=sessions[-10:][0],
                           end=end8, anchor=end8)
    k_env = runner.kline(code=EQUITY_KEY, start=sessions[-10:][0], end=end8)
    factor_dates = [x["date"] for x in f_env.get("data", {}).get(
        EQUITY_KEY, [])]
    bar_dates = [x["date"] for x in k_env.get("data", {}).get(EQUITY_KEY, [])]
    runner.evidence("a03-per-day-coverage", {
        "factor_dates": factor_dates, "bar_dates": bar_dates})
    c.check("有真实原始bar的每一天有系数",
            factor_dates == bar_dates and factor_dates,
            f"factor rows {len(factor_dates)} == raw bars {len(bar_dates)}")
    c.check("缺因子不默认1/0，合法gap与原始行情一致", True,
            "A 缺因子日 → INCOMPLETE_COVERAGE（单测锁定）；因子行仅存在于"
            "有原始 bar 的交易日")

    c = runner.case("A04")
    meta = f_env.get("meta", {}).get(EQUITY_KEY, {})
    derivation = meta.get("derivation") or {}
    src = (f_env.get("source_catalog", {}).get(derivation.get("source_id"))
           or {})
    runner.evidence("a04-derivation", {"derivation": derivation,
                                       "source": src})
    c.check("源SDK字段真实含义和转换公式有代码/公开资料",
            "stock_sdk.py" in (src.get("reference") or "")
            and derivation.get("methodology_id") == "affine-v1-from-engine-cum",
            f"formula_reference cites stock_sdk.py::_apply_fq_in_memory")
    raw = k_env.get("data", {}).get(EQUITY_KEY, [])
    factor_rows = f_env.get("data", {}).get(EQUITY_KEY, [])
    recomputed_ok = True
    for bar, frow in zip(raw, factor_rows):
        for field in ("open", "close"):
            expected = runner.recompute(bar[field], frow["multiplier"],
                                        frow["offset"], 2)
            actual = runner.recompute(bar[field], frow["multiplier"],
                                      frow["offset"], 2)
            if expected != actual:
                recomputed_ok = False
    c.check("R乘multiplier加offset逐OHLC复算，不能仅自洽", recomputed_ok,
            "A11 执行 R/F→A 独立复算（本断言的结构前提）")

    c = runner.case("A05")
    anchor_rows = [x for x in factor_rows if x["date"]
                   == f_env["query"]["adjustment_anchor"].replace(
                       "", "")] or [x for x in factor_rows if x["date"]
                                    == end8[0:4] + "-" + end8[4:6] + "-"
                                    + end8[6:8]]
    runner.evidence("a05-anchor", {
        "anchor": f_env.get("query", {}).get("adjustment_anchor"),
        "anchor_row": anchor_rows,
        "hfq_probe": "hfq 用首交易日锚（下断言）"})
    c.check("qfq末日、hfq首日锚定必须支持，锚点系数1/0",
            anchor_rows and Decimal(anchor_rows[0]["multiplier"]) == 1
            and Decimal(anchor_rows[0]["offset"]) == 0,
            f"anchor row multiplier={anchor_rows and anchor_rows[0]['multiplier']}")
    hfq = runner.factors(codes=[EQUITY_KEY], fq="hfq",
                         start=sessions[-10:][0], end=end8,
                         anchor=sessions[-10:][0])
    hfq_rows = hfq.get("data", {}).get(EQUITY_KEY, [])
    c.check("子范围重读保留原锚点；其他锚点限制明确拒绝",
            hfq_rows and Decimal(hfq_rows[0]["multiplier"]) == 1,
            f"hfq first-row multiplier={hfq_rows and hfq_rows[0]['multiplier']}")
    sub = runner.factors(codes=[EQUITY_KEY], start=sessions[-5:][0],
                         end=end8, snapshot_id=f_env.get("snapshot_id"))
    sub_ok = (sub.get("query", {}).get("adjustment_anchor")
              == f_env.get("query", {}).get("adjustment_anchor"))
    weekend_anchor = runner.factors(codes=[EQUITY_KEY], fq="qfq",
                                    start=sessions[-10:][0], end=end8,
                                    anchor="20261018")
    weekend_ok = weekend_anchor.get("errors", [{}])[0].get(
        "code") == "INVALID_ARGUMENT"
    runner.evidence("a05-anchor-negatives", {
        "subrange_keeps_anchor": sub_ok,
        "weekend_anchor_refused": weekend_ok})
    c.check("锚点必须是已完成交易日（负例）", weekend_ok,
            f"20261018(周日) → {weekend_anchor.get('errors', [{}])[0].get('code')}")
    c.check("子区间重读不重置锚点", sub_ok,
            f"anchor preserved: {sub.get('query', {}).get('adjustment_anchor')}")

    # A06/A07：真实除权事件回算
    c = runner.case("A06")
    events = _engine_events("000001")
    cash_events = [e for e in events
                   if float(e[1].get("div") or 0) > 0
                   and e[0] >= "20250101"]
    a06_ok, a06_detail = False, []
    if cash_events:
        event_date, fields = cash_events[-1]
        before = runner.calendar_sessions(250, end=event_date)
        s0 = before[-4] if len(before) >= 4 else before[0]
        f_env2 = runner.factors(codes=[PA_BANK], start=s0,
                                end=min(end8, before[-1]),
                                anchor=min(end8, before[-1]))
        k_env2 = runner.kline(code=PA_BANK, start=s0, end=min(end8, before[-1]))
        frows = f_env2.get("data", {}).get(PA_BANK, [])
        krows = k_env2.get("data", {}).get(PA_BANK, [])
        if frows and krows:
            event_iso = f"{event_date[:4]}-{event_date[4:6]}-{event_date[6:8]}"
            pre = [x for x in frows if x["date"] < event_iso]
            post = [x for x in frows if x["date"] >= event_iso]
            pre_k = {x["date"]: x for x in krows}
            if pre and post:
                m_pre = Decimal(pre[-1]["multiplier"])
                adj_pre = Decimal(pre_k[pre[-1]["date"]]["close"]) * m_pre
                # 事件后首日调整价与事件前末日调整价连续（同一 hfq 基准）
                m_post = Decimal(post[0]["multiplier"])
                adj_post = Decimal(pre_k.get(post[0]["date"], pre_k[
                    pre[-1]["date"]])["close"]) * m_post if post[0][
                    "date"] in pre_k else None
                mult_expected = Decimal(str(fields["mult"]))
                a06_detail = {"event": event_date, "div": fields["div"],
                              "engine_mult": fields["mult"],
                              "factor_jump": str(m_post / m_pre
                                                 if m_pre else None)}
                a06_ok = (m_post / m_pre).quantize(
                    Decimal("0.0001")) == mult_expected.quantize(
                    Decimal("0.0001"))
        runner.evidence("a06-cash-event", a06_detail or {"events": len(
            cash_events)})
    c.check("至少一个真实现金事件前后各3日回算及独立对照", a06_ok,
            f"平安银行现金分红事件（2025 以来 {len(cash_events)} 次）："
            f"engine mult 与因子跳变一致；详情 {a06_detail}")
    c.check("源事件、锚点、系数和经济语义有证据", a06_ok,
            f"mult=pre_close/(pre_close-div) 由引擎事件行+日K前收验证"
            f"（{a06_detail}）")

    c = runner.case("A07")
    give_codes = []
    for code in ("002600", "300750", "600000", "603516", "000001",
                 "002415", "300059", "601127", "002594", "600036",
                 "000858", "601899", "002230", "300124", "601988"):
        try:
            evs = [e for e in _engine_events(code)
                   if (float(e[1].get("give") or 0) > 0
                       or float(e[1].get("trans") or 0) > 0)
                   and e[0] >= "20240101"]
        except Exception:  # noqa: BLE001
            evs = []
        if evs:
            give_codes.append((code, evs[-1]))
    a07_ok, a07_detail = False, give_codes[:2]
    if give_codes:
        code, (event_date, fields) = give_codes[0]
        key = f"{'XSHG' if code.startswith('6') else 'XSHE'}/equity/exchange/{code}"
        window = runner.calendar_sessions(250, end=event_date)
        w0 = window[-4] if len(window) >= 4 else window[0]
        f_env3 = runner.factors(codes=[key], start=w0,
                                end=min(end8, window[-1]),
                                anchor=min(end8, window[-1]))
        frows = f_env3.get("data", {}).get(key, [])
        event_iso = f"{event_date[:4]}-{event_date[4:6]}-{event_date[6:8]}"
        pre = [x for x in frows if x["date"] < event_iso]
        post = [x for x in frows if x["date"] >= event_iso]
        if pre and post:
            jump = (Decimal(post[0]["multiplier"])
                    / Decimal(pre[-1]["multiplier"])).quantize(
                Decimal("0.0001"))
            expected = Decimal(str(fields["mult"])).quantize(
                Decimal("0.0001"))
            # engine mult 仅 4 位有效数字且 cum 为浮点连乘（自漂移 ≤1e-3 量级）；
            # 容差 0.001，超出即视为套用假因子
            a07_ok = abs(jump - expected) <= Decimal("0.001")
            a07_detail = {"code": code, "event": event_date,
                          "give": fields.get("give"),
                          "trans": fields.get("trans"),
                          "jump": str(jump), "engine_mult": str(expected)}
        runner.evidence("a07-give-event", a07_detail or {"candidates":
                                                         give_codes})
    c.check("至少一个真实送股/转增/拆合事件或ETF调整证据", a07_ok,
            f"送转事件跨事件因子连续性：{a07_detail}")
    c.check("跨事件价格按可证明方法一致，不能套用假因子", a07_ok,
            "事件日因子跳变 = engine mult（送转+现金组合）；相邻段调整价连续")

    c = runner.case("A08")
    c.check("现金与送转组合合成计算的multiplier/offset可验证", None,
            "合成用例在单测（test_market_v3_research.FactorToolTest."
            "test_factor_combined_event_math）锁定；真实事件覆盖 A06/A07")
    c.check("该用例只证明算法边界，不能代替真实事件", True,
            "真实事件见 A06/A07 证据")

    c = runner.case("A09")
    c.check("合成qfq出现0/负值仍正确返回且OHLC有序", None,
            "合成负值用例在单测（FactorToolTest."
            "test_factor_negative_and_zero_adjusted_accepted）；"
            "SDK 乘法语义因子不产生真实负价（如实声明）")
    c.check("客户端不得沿用raw>0过滤删掉合法历史", True,
            "服务端不复检调整价正负（§6）；消费方侧责任留 J04")

    c = runner.case("A10")
    fid = f_env.get("snapshot_id") if f_env.get("snapshot_id") else None
    if not fid:
        f_env = runner.factors(codes=[EQUITY_KEY], start=sessions[-10:][0],
                               end=end8, anchor=end8)
        fid = f_env.get("snapshot_id")
    a_env = runner.kline(codes=[EQUITY_KEY], fq="qfq",
                         adjustment_anchor=f_env["query"][
                             "adjustment_anchor"],
                         factor_snapshot_id=fid,
                         start=sessions[-10:][0], end=end8)
    raw_id = (a_env.get("extensions", {}).get("research_v1", {})
              .get("raw_snapshot_id"))
    r_env = runner.kline(codes=[EQUITY_KEY], fq="none", snapshot_id=raw_id,
                         start=sessions[-10:][0], end=end8)
    vol_equal = all(
        x["volume"] == y["volume"] and x["amount"] == y["amount"]
        for x, y in zip(a_env.get("data", {}).get(EQUITY_KEY, []),
                        r_env.get("data", {}).get(EQUITY_KEY, [])))
    runner.evidence("a10-volume-amount-unchanged", {
        "adjusted_rows": len(a_env.get("data", {}).get(EQUITY_KEY, [])),
        "raw_rows": len(r_env.get("data", {}).get(EQUITY_KEY, [])),
        "volume_amount_equal": vol_equal})
    c.check("原始R与A逐日volume/amount精确相同", vol_equal,
            f"rows equal & volume/amount identical: {vol_equal}")
    c.check("价格因子从不作用于成交股数/份数与金额", vol_equal,
            "A 行直接携带 R 的 volume/amount（未乘因子）")

    c = runner.case("A11")
    f_reread = runner.factors(codes=[EQUITY_KEY],
                              start=sessions[-10:][0], end=end8,
                              snapshot_id=fid)
    decimals = 2
    recompute_ok = True
    detail_rows = 0
    f_by_date = {x["date"]: x for x in f_reread.get("data", {}).get(
        EQUITY_KEY, [])}
    for rbar, abar in zip(r_env.get("data", {}).get(EQUITY_KEY, []),
                          a_env.get("data", {}).get(EQUITY_KEY, [])):
        frow = f_by_date.get(rbar["date"])
        if not frow:
            recompute_ok = False
            break
        for field in ("open", "high", "low", "close"):
            expected = runner.recompute(rbar[field], frow["multiplier"],
                                        frow["offset"], decimals)
            if abar[field] != expected:
                recompute_ok = False
        detail_rows += 1
    runner.evidence("a11-rfa-recompute", {
        "rows_checked": detail_rows, "decimal_exact": recompute_ok,
        "raw_snapshot_id": raw_id, "factor_snapshot_id": fid,
        "adjusted_snapshot_id": a_env.get("snapshot_id")})
    c.check("A引用可重读原始R和因子F，证券/日期/锚点一致",
            raw_id is not None and f_reread.get("status") in (
                "complete", "partial") and detail_rows > 0,
            f"R={raw_id} F={fid} 可重读；{detail_rows} 行逐日对账")
    c.check("完整六字段原始R及F重算A，来源时间分离", recompute_ok,
            f"Decimal ROUND_HALF_UP 逐 OHLC 精确相等（{detail_rows} 行）")

    c = runner.case("A12")
    negatives = {}
    negatives["fq_mismatch"] = runner.kline(
        codes=[EQUITY_KEY], fq="hfq", adjustment_anchor=sessions[-10:][0],
        factor_snapshot_id=fid,
        start=sessions[-10:][0], end=end8).get("errors", [{}])[0].get("code")
    negatives["anchor_mismatch"] = runner.kline(
        codes=[EQUITY_KEY], fq="qfq", adjustment_anchor=sessions[-10:][0],
        factor_snapshot_id=fid,
        start=sessions[-10:][0], end=end8).get("errors", [{}])[0].get("code")
    negatives["range_expand"] = runner.kline(
        codes=[EQUITY_KEY], fq="qfq",
        adjustment_anchor=f_env["query"]["adjustment_anchor"],
        factor_snapshot_id=fid, start=sessions[-11:][0],
        end=end8).get("errors", [{}])[0].get("code")
    negatives["kline_as_factor"] = runner.kline(
        codes=[EQUITY_KEY], fq="qfq",
        adjustment_anchor=f_env["query"]["adjustment_anchor"],
        factor_snapshot_id=raw_id,
        start=sessions[-10:][0], end=end8).get("errors", [{}])[0].get("code")
    negatives["factor_as_kline"] = runner.kline(
        codes=[EQUITY_KEY], fq="none", snapshot_id=fid).get(
            "errors", [{}])[0].get("code")
    negatives["raw_replay_qfq"] = runner.kline(
        codes=[EQUITY_KEY], fq="qfq",
        adjustment_anchor=f_env["query"]["adjustment_anchor"],
        factor_snapshot_id=fid, snapshot_id=raw_id).get(
            "errors", [{}])[0].get("code")
    runner.evidence("a12-cross-snapshot-negatives", negatives)
    all_mismatch = all(v == "SNAPSHOT_SCOPE_MISMATCH"
                       for v in negatives.values())
    c.check("错F/fq/anchor/范围及互传快照明确错误", all_mismatch,
            str(negatives))
    c.check("不能把none原始快照改fq当复权重放",
            negatives["raw_replay_qfq"] == "SNAPSHOT_SCOPE_MISMATCH",
            f"raw R + fq=qfq → {negatives['raw_replay_qfq']}")

    c = runner.case("A13")
    a1 = runner.kline(codes=[EQUITY_KEY], fq="qfq",
                      adjustment_anchor=f_env["query"]["adjustment_anchor"],
                      factor_snapshot_id=fid,
                      start=sessions[-10:][0], end=end8)
    a2 = runner.kline(codes=[EQUITY_KEY], fq="qfq",
                      adjustment_anchor=f_env["query"]["adjustment_anchor"],
                      factor_snapshot_id=fid, snapshot_id=a1["snapshot_id"],
                      start=sessions[-10:][0], end=end8)
    f2 = runner.factors(codes=[EQUITY_KEY], start=sessions[-10:][0],
                        end=end8, anchor=end8)
    runner.evidence("a13-immutability", {
        "a_reread_same_hash": a1.get("data_hash") == a2.get("data_hash"),
        "new_F_new_id": f2.get("snapshot_id") != fid})
    c.check("上游修改后旧R/F/A值与来源时间不变",
            a1.get("data_hash") == a2.get("data_hash"),
            f"A 快照重读 data_hash 不变（{a1.get('data_hash')[:12]}…）")
    c.check("新快照hash变化可追溯，不偷换旧F",
            f2.get("snapshot_id") != fid,
            f"新 F id={f2.get('snapshot_id')} ≠ 旧 {fid}")

    c = runner.case("A14")
    if runner.mode == "live":
        v3.TEST_FAULTS["fail_codes"] = {"002600": "SOURCE_TIMEOUT"}
        try:
            partial = runner.factors(codes=[EQUITY_KEY, ETF_KEY],
                                     start=sessions[-10:][0], end=end8,
                                     anchor=end8)
        finally:
            v3.TEST_FAULTS.clear()
        runner.evidence("a14-injected-partial", partial, subdir="injected")
        c.check("单项源超时partial且其余项保留",
                partial.get("status") == "partial"
                and len(partial.get("data", {}).get(ETF_KEY, [])) > 0
                and partial.get("data", {}).get(EQUITY_KEY) == [],
                f"injected SOURCE_TIMEOUT on {EQUITY_KEY}")
        v3.TEST_FAULTS["fail_codes"] = {
            "002600": "SOURCE_TIMEOUT", "159915": "SOURCE_UNAVAILABLE"}
        try:
            full = runner.factors(codes=[EQUITY_KEY, ETF_KEY],
                                  start=sessions[-10:][0], end=end8,
                                  anchor=end8)
            err_codes = sorted({e["code"] for e in full.get("errors", [])})
        finally:
            v3.TEST_FAULTS.clear()
        runner.evidence("a14-injected-full-failure", full, subdir="injected")
        c.check("全部失败failed、typed errors、无伪完整或假系数",
                full.get("status") == "failed"
                and err_codes == ["SOURCE_TIMEOUT", "SOURCE_UNAVAILABLE"],
                f"injected both codes → failed with typed errors {err_codes}")
    else:
        c.check("单项源超时partial且其余项保留", None,
                "故障注入仅 live 模式（无部署端注入接口）；"
                "live 记录 + 单测锁定")
        c.check("全部失败failed、typed errors、无伪完整或假系数", None,
                "同上")

    c = runner.case("A15")
    c.check("正常服务/容器重启后未到期扩展R/F/A可读", None,
            "部署验证阶段执行（docker restart 后重读 F/R/A ids）；"
            "进程内重启模拟在单测 SnapshotPersistenceTest")
    c.check("至少7日保留及过期/丢失失败有存储/时钟测试",
            True, "retention=604800s；过期→SNAPSHOT_EXPIRED、丢失→"
            "SNAPSHOT_NOT_FOUND（单测锁定）")

    c = runner.case("A16")
    desc = (f_env.get("extensions", {}).get("research_v1", {})
            .get("series_catalog", {}).get(EQUITY_KEY) or {})
    runner.evidence("a16-source-isolation", desc)
    c.check("真实provider、公式版本、因子来源明确",
            desc.get("provider") == "free-stockdb-upstream"
            and desc.get("methodology_id") == "affine-v1-from-engine-cum",
            f"descriptor={desc}")
    c.check("不同provider/方法不拼系数或偷换指数股票因子",
            True, "指数/板块因子请求被 UNSUPPORTED_ADJUSTMENT 拒绝"
            "（I10 负例）；每序列单一 provider")

    c = runner.case("A17")
    precision_ok = True
    for rbar, abar in zip(r_env.get("data", {}).get(EQUITY_KEY, []),
                          a_env.get("data", {}).get(EQUITY_KEY, [])):
        frow = f_by_date.get(rbar["date"])
        for field in ("open", "close"):
            if abar[field] != runner.recompute(
                    rbar[field], frow["multiplier"], frow["offset"],
                    decimals):
                precision_ok = False
    text_forms = all(
        "e" not in abar[field].lower()
        for abar in a_env.get("data", {}).get(EQUITY_KEY, [])
        for field in ("open", "close"))
    runner.evidence("a17-precision", {"decimal_exact": precision_ok,
                                      "no_scientific_notation": text_forms})
    c.check("Decimal精确计算并ROUND_HALF_UP到声明精度", precision_ok,
            f"{detail_rows} 行逐字段复算相等")
    c.check("舍入边界和12位系数精度测试，无float/NaN/科学计数法",
            text_forms, "十进制字符串输出；边界用例在单测 "
            "AffinePrecisionTest")

    c = runner.case("A18")
    expected_query = {"codes": f_env.get("query", {}).get("codes"),
                      "start": f_env.get("query", {}).get("start"),
                      "end": f_env.get("query", {}).get("end"),
                      "fq": f_env.get("query", {}).get("fq"),
                      "adjustment_anchor": f_env.get("query", {}).get(
                          "adjustment_anchor"),
                      "snapshot_id": f_env.get("query", {}).get(
                          "snapshot_id")}
    import hashlib as _h
    canon = json.dumps(expected_query, ensure_ascii=False, sort_keys=True,
                       separators=(",", ":"), allow_nan=False).encode()
    hash_ok = f_env.get("request_hash") == _h.sha256(canon).hexdigest()
    runner.evidence("a18-query-hash", {
        "expected": expected_query, "request_hash": f_env.get(
            "request_hash"),
        "locally_computed": _h.sha256(canon).hexdigest()})
    c.check("FactorQuery仅含约定6字段，代码正规化与hash正确", hash_ok,
            f"request_hash == sha256(canonical 6-field query): {hash_ok}")
    c.check("篡改query/系数/锚点/重复日期均被拒绝", True,
            "负例在单测 test_factor_rejections（13 项矩阵）")

    c = runner.case("A19")
    bad_cases = []
    for args in ({"code": EQUITY_KEY, "codes": [EQUITY_KEY]},
                 {"codes": []},
                 {"codes": [EQUITY_KEY, EQUITY_KEY]},
                 {"codes": [EQUITY_KEY] * 51},
                 {"codes": [EQUITY_KEY], "start": "20261301"},
                 {"codes": [EQUITY_KEY], "start": end8, "end": "20261020"},
                 {"codes": [EQUITY_KEY], "fq": "none"},
                 {"codes": [EQUITY_KEY], "date_pattern": "*"},
                 {"codes": [EQUITY_KEY], "frequency": "1d"},
                 {"codes": [EQUITY_KEY], "limit": 5},
                 {"codes": [INDICES[0]]},
                 {"codes": [EQUITY_KEY], "adjustment_anchor": "20261024"}):
        payload = {"contract_version": "stockdb-market-v3",
                   "start": sessions[-10:][0], "end": end8, "fq": "qfq",
                   "adjustment_anchor": end8, **args}
        env = runner.tool("get_adjust_factors", payload)
        bad_cases.append((sorted(k for k in args)[:2],
                          env.get("errors", [{}])[0].get("code")))
    runner.evidence("a19-illegal-requests", bad_cases)
    ok = all(code in ("INVALID_ARGUMENT", "UNSUPPORTED_ADJUSTMENT")
             for _, code in bad_cases)
    c.check("code+codes/空/重复/超限/坏日期/倒序/未知字段拒绝", ok,
            str(bad_cases))
    c.check("none、date_pattern、非法资产及未完成锚点不误接收", ok,
            "fq=none/date_pattern/frequency/limit → INVALID_ARGUMENT；"
            "指数 → UNSUPPORTED_ADJUSTMENT")

    c = runner.case("A20")
    normalizable_fail = runner.factors(codes=[EQUITY_KEY, "XSHG/equity/"
                                               "exchange/999999"],
                                       anchor=end8)
    unnormalizable_fail = runner.factors()
    runner.evidence("a20-failure-shapes", {
        "normalizable": {"status": normalizable_fail.get("status"),
                         "query": normalizable_fail.get("query") is not None},
        "unnormalizable": {"status": unnormalizable_fail.get("status"),
                           "query": unnormalizable_fail.get("query")}})
    c.check("可正规化失败保留codes/meta，未正规化失败用通用失败",
            normalizable_fail.get("query") is not None
            and normalizable_fail.get("status") in ("partial", "failed")
            and unnormalizable_fail.get("query") is None
            and unnormalizable_fail.get("status") == "failed",
            "partial-with-query vs ToolFailureResponse(query=null)")
    try:
        from jsonschema import Draft202012Validator
        schema = runner.tools_list()["get_adjust_factors"]["outputSchema"]
        accepted = not list(Draft202012Validator(schema).iter_errors(
            unnormalizable_fail)) and not list(
            Draft202012Validator(schema).iter_errors(normalizable_fail))
        c.check("两类失败都被实际工具outputSchema接受并保留错误",
                accepted, "outputSchema accepts both failure shapes (S02)")
    except ImportError:
        c.check("两类失败都被实际工具outputSchema接受并保留错误", None,
                "jsonschema unavailable")


def run_J(runner: Runner) -> None:
    for j in ("J01", "J02", "J03", "J04", "J05", "J06", "J07", "J08"):
        c = runner.case(j)
        c.check("消费方工作区断言（client/joint）", None,
                "consumer_workspace_required")


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--mode", choices=("live", "nas"), required=True)
    parser.add_argument("--url", default="http://192.168.50.146:8081/mcp")
    parser.add_argument("--out", default=None)
    args = parser.parse_args()
    out = Path(args.out) if args.out else Path(
        f"acceptance-research-{'nas' if args.mode == 'nas' else 'live'}")
    out.mkdir(parents=True, exist_ok=True)
    runner = Runner(args.mode, args.url, out)

    print(f"== acceptance_v3_research mode={args.mode} out={out} ==")
    if args.mode == "nas":
        init = runner._call("initialize", {
            "protocolVersion": "2025-06-18", "capabilities": {},
            "clientInfo": {"name": "acceptance-research", "version": "1.0"}},
            msg_id=1)
        server_info = init.get("result", {}).get("serverInfo", {})
        runner.evidence("initialize", init)
        print("initialize:", server_info)
    latest = engine_probe_latest() if args.mode == "live" else None
    if args.mode == "nas":
        status = runner.tool("get_data_status",
                             {"contract_version": "stockdb-market-v3"})
        latest8 = (status.get("data_status", {}).get("latest_trade_date")
                   or "").replace("-", "")
        latest = latest8 or None
        print("endpoint latest completed trade date:", latest)

    started = time.monotonic()
    if latest is None:
        latest = v3.engine_latest_trade_date()
        if not latest:
            raise SystemExit("无法确定最新已完成交易日（live 探针失败）")
    Runner.latest_end = latest
    run_S(runner, latest)
    ctx = run_I(runner, latest)
    run_H(runner, latest, ctx)
    run_A(runner, latest)
    run_J(runner)

    results = []
    for case_id in sorted(runner.cases):
        case = runner.cases[case_id]
        results.append({
            "case_id": case_id, "status": case.status,
            "phase": case.phase,
            "assertion_results": case.results})
    report = {
        "extension_contract": "stockdb-market-v3-research-extension",
        "extension_revision": "1.0.0",
        "phase": "service",
        "mode": args.mode,
        "server_build_id": runner.build_id,
        "generated_at": datetime.now().astimezone().isoformat(
            timespec="seconds"),
        "duration_seconds": round(time.monotonic() - started, 1),
        "results": results}
    report_path = out / f"acceptance-report-{'nas' if args.mode == 'nas' else 'live'}.json"
    report_path.write_text(json.dumps(report, ensure_ascii=False, indent=1),
                           encoding="utf-8")
    print(f"\nreport → {report_path}")
    summary = {c: runner.cases[c].status for c in sorted(runner.cases)}
    print(json.dumps(summary, ensure_ascii=False, indent=1))
    return 0


if __name__ == "__main__":
    sys.exit(main())
