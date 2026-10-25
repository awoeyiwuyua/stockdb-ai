#!/usr/bin/env python3
"""R3 端到端验证：F→A→重放（qfq/hfq × 股票/ETF × 单票/混合），
用反馈包冻结校验器逐份校验首次与重放响应。"""
import importlib.util
import json
import sys
import urllib.request
from pathlib import Path

# 冻结校验器 = 消费方反馈包原件（与仓库 contract/research-extension 逐字节
# 一致，已核验）；用仓库内副本保证任意检出可运行。
FB = Path(__file__).resolve().parent / "contract" / "research-extension"
URL = "http://192.168.50.146:8081/mcp"

spec = importlib.util.spec_from_file_location(
    "frozen_replay_contract", FB / "validate_contract.py")
wire = importlib.util.module_from_spec(spec)
sys.modules["frozen_replay_contract"] = wire
spec.loader.exec_module(wire)


def call(method, params, msg_id=1, timeout=90):
    msg = {"jsonrpc": "2.0", "id": msg_id, "method": method, "params": params}
    req = urllib.request.Request(
        URL, data=json.dumps(msg).encode(),
        headers={"Content-Type": "application/json",
                 "Accept": "application/json, text/event-stream"})
    with urllib.request.urlopen(req, timeout=timeout) as resp:
        body = resp.read().decode("utf-8", "replace")
    for line in body.splitlines():
        if line.startswith("data:"):
            return json.loads(line[5:].strip())
    return json.loads(body)


def tool(name, args, msg_id=1):
    return call("tools/call", {"name": name, "arguments": args},
                msg_id).get("result", {}).get("structuredContent", {})


def main():
    out = {"cases": []}
    ok_all = True
    msg_id = 10
    for fq, anchor in (("qfq", "20261023"), ("hfq", "20261020")):
        for codes in ([["XSHE/equity/exchange/000001"],
                       ["XSHG/etf/exchange/510300"],
                       ["XSHE/equity/exchange/000001",
                        "XSHG/etf/exchange/510300"]]):
            msg_id += 1
            f = tool("get_adjust_factors", {
                "contract_version": "stockdb-market-v3", "codes": codes,
                "start": "20261020", "end": "20261023", "fq": fq,
                "adjustment_anchor": anchor}, msg_id)
            wire.validate(f, "FactorResponse")
            base = {"contract_version": "stockdb-market-v3", "codes": codes,
                    "frequency": "1d", "start": "20261020", "end": "20261023",
                    "fq": fq, "limit": 0, "adjustment_anchor": anchor,
                    "factor_snapshot_id": f["snapshot_id"]}
            a1 = tool("get_kline", base, msg_id + 1000)
            wire.validate(a1, "KlineResponse")
            replay = tool("get_kline", {**base,
                                        "snapshot_id": a1["snapshot_id"]},
                          msg_id + 2000)
            wire.validate(replay, "KlineResponse")
            same_adj = all(
                a1["meta"][k]["adjustment"] == replay["meta"][k]["adjustment"]
                and replay["meta"][k]["adjustment"]["mode"] == fq
                for k in codes)
            same_data = a1["data"] == replay["data"]
            same_sources = a1["source_catalog"] == replay["source_catalog"]
            case_ok = same_adj and same_data and same_sources
            ok_all = ok_all and case_ok
            out["cases"].append({
                "fq": fq, "codes": codes, "factor_id": f["snapshot_id"],
                "original_id": a1["snapshot_id"],
                "adjustment_first": a1["meta"][codes[0]]["adjustment"],
                "adjustment_replay": replay["meta"][codes[0]]["adjustment"],
                "adjustment_equal": same_adj, "data_equal": same_data,
                "source_catalog_equal": same_sources, "ok": case_ok})
            msg_id += 3000

    # 子日期重放（锚点在子区间外）+ fields 投影重放
    f = tool("get_adjust_factors", {
        "contract_version": "stockdb-market-v3",
        "codes": ["XSHE/equity/exchange/000001"], "start": "20261020",
        "end": "20261023", "fq": "qfq", "adjustment_anchor": "20261023"}, 9001)
    base = {"contract_version": "stockdb-market-v3",
            "codes": ["XSHE/equity/exchange/000001"], "frequency": "1d",
            "start": "20261020", "end": "20261023", "fq": "qfq", "limit": 0,
            "adjustment_anchor": "20261023",
            "factor_snapshot_id": f["snapshot_id"]}
    a1 = tool("get_kline", base, 9002)
    for tag, extra in (("subrange", {"start": "20261021", "end": "20261022"}),
                       ("projection", {"fields": ["close"]})):
        replay = tool("get_kline", {**base, "snapshot_id": a1["snapshot_id"],
                                    **extra}, 9003)
        wire.validate(replay, "KlineResponse")
        adj_ok = (replay["meta"]["XSHE/equity/exchange/000001"]
                  ["adjustment"]["mode"] == "qfq"
                  and replay["meta"]["XSHE/equity/exchange/000001"]
                  ["adjustment"]["anchor_date"] == "20261023")
        ok_all = ok_all and adj_ok
        out["cases"].append({"case": tag, "adjustment_kept": adj_ok,
                             "ok": adj_ok})

    # 负例：错 fq / 互传 R→factor / none 重放 A
    negatives = {}
    negatives["wrong_fq"] = tool("get_kline", {
        **base, "snapshot_id": a1["snapshot_id"], "fq": "hfq",
        "adjustment_anchor": "20261020"}).get("errors", [{}])[0].get("code")
    raw_id = a1["extensions"]["research_v1"]["raw_snapshot_id"]
    negatives["raw_replay_as_qfq"] = tool("get_kline", {
        **base, "snapshot_id": raw_id}).get("errors", [{}])[0].get("code")
    negatives["none_replay_of_A"] = tool("get_kline", {
        "contract_version": "stockdb-market-v3",
        "codes": ["XSHE/equity/exchange/000001"], "frequency": "1d",
        "start": "20261020", "end": "20261023", "fq": "none", "limit": 0,
        "snapshot_id": a1["snapshot_id"]}).get("errors", [{}])[0].get("code")
    neg_ok = all(v == "SNAPSHOT_SCOPE_MISMATCH" for v in negatives.values())
    ok_all = ok_all and neg_ok
    out["negatives"] = {"results": negatives, "all_refused": neg_ok}

    out["all_ok"] = ok_all
    dest = Path(sys.argv[1] if len(sys.argv) > 1 else "replay-verify.json")
    dest.write_text(json.dumps(out, ensure_ascii=False, indent=1),
                    encoding="utf-8")
    print(json.dumps(out, ensure_ascii=False, indent=1)[:2200])
    sys.exit(0 if ok_all else 1)


if __name__ == "__main__":
    main()
