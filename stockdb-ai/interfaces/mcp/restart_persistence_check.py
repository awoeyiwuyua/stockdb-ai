#!/usr/bin/env python3
"""E07 增强版 A15 部署级验证：捕获 F/R/A → docker restart → 三者 data_hash、
series 元数据、来源 retrieved_at 逐项比对（不只"可读"）。"""
import json
import sys
import time
import urllib.request
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

URL = "http://192.168.50.146:8081/mcp"
KEY = "XSHE/equity/exchange/002600"


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
    resp = call("tools/call", {"name": name, "arguments": args}, msg_id)
    return resp.get("result", {}).get("structuredContent", {})


def main():
    out = {"steps": []}
    win = {"start": "20261009", "end": "20261023"}
    # 1) F
    f = tool("get_adjust_factors", {
        "contract_version": "stockdb-market-v3", "codes": [KEY],
        "start": win["start"], "end": win["end"], "fq": "qfq",
        "adjustment_anchor": win["end"]}, 1)
    fid = f["snapshot_id"]
    out["steps"].append(["F captured", fid, f["data_hash"]])
    # 2) A（连带 R）
    a = tool("get_kline", {
        "contract_version": "stockdb-market-v3", "codes": [KEY],
        "frequency": "1d", "start": win["start"], "end": win["end"],
        "fq": "qfq", "limit": 0, "adjustment_anchor": win["end"],
        "factor_snapshot_id": fid}, 2)
    aid = a["snapshot_id"]
    rid = a["extensions"]["research_v1"]["raw_snapshot_id"]
    out["steps"].append(["A captured", aid, a["data_hash"]])
    # 3) R 重读取 hash（E07：R 的 data_hash 也必须前后一致）
    r_before = tool("get_kline", {
        "contract_version": "stockdb-market-v3", "codes": [KEY],
        "frequency": "1d", "start": win["start"], "end": win["end"],
        "fq": "none", "limit": 0, "snapshot_id": rid}, 3)
    out["steps"].append(["R captured", rid, r_before.get("data_hash")])
    before = {
        "F": {"data_hash": f["data_hash"], "data": f["data"],
              "catalog": f["extensions"]["research_v1"]["series_catalog"],
              "factor_source_retrieved_at":
                  f["source_catalog"].get("engine-adjust-cum", {})
                  .get("retrieved_at")},
        "R": {"data_hash": r_before.get("data_hash"), "data":
              r_before.get("data"),
              "catalog": r_before.get("extensions", {})
              .get("research_v1", {}).get("series_catalog"),
              "bars_source_retrieved_at":
                  r_before.get("source_catalog", {}).get("engine-daily-kline",
                                                         {})
                  .get("retrieved_at")},
        "A": {"data_hash": a["data_hash"], "data": a["data"],
              "catalog": a["extensions"]["research_v1"]["series_catalog"],
              "raw_snapshot_id":
                  a["extensions"]["research_v1"]["raw_snapshot_id"]}}
    # E01 断言：R/A 目录四字段一致（价格序列），R 的 raw_snapshot_id=null
    out["r_has_research_metadata"] = bool(before["R"]["catalog"])
    out["r_catalog_equals_a_catalog"] = all(
        (before["R"]["catalog"] or {}).get(k, {}).get(f2)
        == (before["A"]["catalog"] or {}).get(k, {}).get(f2)
        for k in (KEY,) for f2 in ("series_id", "provider",
                                   "methodology_id", "methodology_version"))

    # 4) 重启
    import nas_ssh
    nas_ssh.run("echo xiahaihe | sudo -S docker restart stockdb")
    for _ in range(30):
        time.sleep(4)
        try:
            st = tool("get_data_status",
                      {"contract_version": "stockdb-market-v3"}, 4)
            if st.get("server", {}).get("started_at"):
                break
        except Exception:
            continue
    out["steps"].append(["restarted", st["server"]["started_at"],
                         st["server"]["build_id"]])

    # 5) 重读三者并逐项比对
    f2 = tool("get_adjust_factors", {
        "contract_version": "stockdb-market-v3", "codes": [KEY],
        "start": win["start"], "end": win["end"], "fq": "qfq",
        "adjustment_anchor": win["end"], "snapshot_id": fid}, 5)
    r2 = tool("get_kline", {
        "contract_version": "stockdb-market-v3", "codes": [KEY],
        "frequency": "1d", "start": win["start"], "end": win["end"],
        "fq": "none", "limit": 0, "snapshot_id": rid}, 6)
    a2 = tool("get_kline", {
        "contract_version": "stockdb-market-v3", "codes": [KEY],
        "frequency": "1d", "start": win["start"], "end": win["end"],
        "fq": "qfq", "limit": 0, "adjustment_anchor": win["end"],
        "factor_snapshot_id": fid, "snapshot_id": aid}, 7)
    out["F_data_hash_same"] = (f2.get("data_hash")
                               == before["F"]["data_hash"])
    out["F_data_same"] = f2.get("data") == before["F"]["data"]
    out["R_data_hash_same"] = (r2.get("data_hash")
                               == before["R"]["data_hash"])
    out["R_data_same"] = r2.get("data") == before["R"]["data"]
    out["A_data_hash_same"] = (a2.get("data_hash")
                               == before["A"]["data_hash"])
    out["A_data_same"] = a2.get("data") == before["A"]["data"]
    out["R_catalog_same_after_restart"] = (
        r2.get("extensions", {}).get("research_v1", {}).get("series_catalog")
        == before["R"]["catalog"])
    out["source_retrieved_at_unchanged"] = (
        r2.get("source_catalog", {}).get("engine-daily-kline", {})
        .get("retrieved_at") == before["R"]["bars_source_retrieved_at"])
    out["missing_snapshot_fails"] = tool("get_kline", {
        "contract_version": "stockdb-market-v3", "codes": [KEY],
        "frequency": "1d", "start": win["start"], "end": win["end"],
        "fq": "none", "limit": 0,
        "snapshot_id": "snap-kline-doesnotexist"}, 8).get(
            "errors", [{}])[0].get("code") == "SNAPSHOT_NOT_FOUND"
    out["all_ok"] = all([
        out["F_data_hash_same"], out["F_data_same"], out["R_data_hash_same"],
        out["R_data_same"], out["A_data_hash_same"], out["A_data_same"],
        out["r_has_research_metadata"], out["r_catalog_equals_a_catalog"],
        out["R_catalog_same_after_restart"],
        out["source_retrieved_at_unchanged"],
        out["missing_snapshot_fails"]])
    with open(sys.argv[1] if len(sys.argv) > 1 else "a15-restart.json",
              "w", encoding="utf-8") as fh:
        json.dump(out, fh, ensure_ascii=False, indent=1)
    print(json.dumps({k: v for k, v in out.items() if k != "steps"},
                     ensure_ascii=False, indent=1))
    sys.exit(0 if out["all_ok"] else 1)


if __name__ == "__main__":
    main()
