#!/usr/bin/env python3
"""Offline contract checker; no NAS access, market collection or database writes.

The server and future client may run this same checker against unpacked public
responses. It verifies shape and selected cross-field invariants, not market
truth, calendar correctness, evidence authenticity or operational reliability.
"""

from __future__ import annotations

import argparse
from copy import deepcopy
from datetime import datetime
from decimal import Decimal
import hashlib
import json
from pathlib import Path
import re

from jsonschema import Draft202012Validator, FormatChecker


ROOT = Path(__file__).resolve().parent
FIELDS = ("open", "high", "low", "close", "volume", "amount")
LEGAL_GAPS = {"PRE_LISTING", "POST_DELISTING", "SUSPENDED"}
RETRYABLE = {"NOT_PUBLISHED", "SOURCE_UNAVAILABLE", "SOURCE_TIMEOUT", "BUSY"}
PROFILES = {
    "core_daily_v1", "fullmarket_daily_v1", "index_board_daily_v1",
    "minute_v1", "adjusted_history_v1", "historical_identity_v1",
}
SCHEMA = json.loads((ROOT / "contract.schema.json").read_text())
FORMAT_CHECKER = FormatChecker()


@FORMAT_CHECKER.checks("date-time", raises=(ValueError, TypeError))
def valid_timestamp(value):
    # Do not depend on optional jsonschema format extras in the service repo.
    if not isinstance(value, str):
        return True
    if not re.fullmatch(r"\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}(?:\.\d+)?(?:Z|[+-]\d{2}:\d{2})", value):
        return False
    parsed = datetime.fromisoformat(value)
    return parsed.tzinfo is not None


def require(condition: bool, message: str) -> None:
    if not condition:
        raise ValueError(message)


def canonical_json(value: object) -> bytes:
    def inspect(item: object) -> None:
        require(type(item) is not float, "floating point is forbidden in hashed data/query")
        if type(item) is int:
            require(abs(item) <= 2**53 - 1, "integer exceeds canonical safe range")
        if isinstance(item, dict):
            for key, child in item.items():
                require(isinstance(key, str), "JSON keys must be strings")
                inspect(child)
        elif isinstance(item, list):
            for child in item:
                inspect(child)
    inspect(value)
    return json.dumps(value, ensure_ascii=False, sort_keys=True,
                      separators=(",", ":"), allow_nan=False).encode("utf-8")


def digest(value: object) -> str:
    return hashlib.sha256(canonical_json(value)).hexdigest()


def date8(value: str):
    result = datetime.strptime(value, "%Y%m%d").date()
    require(result.strftime("%Y%m%d") == value, "date must be exactly YYYYMMDD")
    return result


def instrument(value: dict, catalog: dict) -> None:
    parts = value["instrument_id"].split("/")
    require(parts == [value["exchange"], value["asset_type"], value["namespace"], value["code"]],
            "instrument fields do not match qualified id")
    require(value["exchange"] != "CN" or value["asset_type"] == "board", "CN requires board asset")
    if value["identity_verified"]:
        require(value["identity_source_id"] in catalog, "verified identity has no source reference")


def validate_request(payload: dict, definition: str) -> None:
    if definition == "KlineRequest":
        for key in [payload["code"]] if "code" in payload else payload["codes"]:
            exchange, asset, _, _ = key.split("/")
            require(exchange != "CN" or asset == "board", "CN requires board asset")
        if payload["frequency"] in {"1d", "1w", "1M"}:
            start, end = date8(payload["start"]), date8(payload["end"])
        else:
            start, end = datetime.fromisoformat(payload["start"]), datetime.fromisoformat(payload["end"])
        require(start <= end, "request start exceeds end")
        if "adjustment_anchor" in payload:
            date8(payload["adjustment_anchor"])
    elif definition == "StockListRequest":
        date8(payload["as_of"])
    elif definition == "TradingDaysRequest":
        require(date8(payload["start"]) <= date8(payload["end"]), "calendar start exceeds end")


def validate_header(payload: dict) -> None:
    served = datetime.fromisoformat(payload["served_at"])
    require(payload["is_partial"] == (payload["status"] != "complete"), "partial/status disagree")
    if payload["status"] == "failed":
        require(bool(payload["errors"]), "failed response must contain a machine error")
    else:
        require(payload["snapshot_id"] is not None, "usable response requires a snapshot")
        require(payload["snapshot_expires_at"] is not None, "usable snapshot requires expiry")
        require(datetime.fromisoformat(payload["snapshot_expires_at"]) > served, "snapshot already expired")
    for error in payload["errors"]:
        require(error["retryable"] == (error["code"] in RETRYABLE), "retryable differs from error table")
        if error["code"] == "BUSY":
            wait = error["details"].get("retry_after_ms")
            require(type(wait) is int and wait > 0, "BUSY requires positive retry_after_ms")
    for source in payload["source_catalog"].values():
        nullable = {"provider", "reference", "source_updated_at", "published_at", "finality_evidence"}
        require(set(source["unknown_fields"]) == {key for key in nullable if source[key] is None},
                "source unknown_fields must exactly identify unknown fields")
        require(datetime.fromisoformat(source["retrieved_at"]) <= served, "retrieved_at exceeds served_at")


def validate_kline(payload: dict) -> None:
    validate_header(payload)
    data, meta, query, catalog = payload["data"], payload["meta"], payload["query"], payload["source_catalog"]
    require(payload["data_hash"] == digest(data), "data_hash mismatch")
    if query is None:
        require(payload["status"] == "failed" and not data and not meta and payload["request_hash"] is None,
                "null query is only valid for a failed unnormalizable request")
        return
    request = {"contract_version": "stockdb-market-v3", **{k: v for k, v in query.items() if v is not None}}
    validate(request, "KlineRequest")
    require(payload["request_hash"] == digest(query), "request_hash mismatch")
    require(query["fields"] == [field for field in FIELDS if field in query["fields"]], "fields not normalized")
    require(set(data) == set(meta) == set(query["codes"]), "data/meta keys differ from requested codes")
    require(all(e["instrument_id"] is None or e["instrument_id"] in data for e in payload["errors"]),
            "error references an unrequested instrument")
    daily = query["frequency"] in {"1d", "1w", "1M"}
    start = date8(query["start"]).isoformat() if daily else query["start"]
    end = date8(query["end"]).isoformat() if daily else query["end"]
    usable = False
    all_complete = True
    for key, rows in data.items():
        info = meta[key]
        instrument(info["instrument"], catalog)
        require(info["instrument"]["instrument_id"] == key, "meta instrument differs from data key")
        coverage, units = info["coverage"], info["units"]
        expected, available, gaps = coverage["expected_rows"], coverage["available_rows"], coverage["gaps"]
        require(coverage["returned_rows"] == len(rows), "returned_rows mismatch")
        require(available is None or available >= len(rows), "available_rows less than returned")
        if expected is not None and available is not None:
            require(expected == available + len(gaps), "expected rows do not equal available plus gaps")
        if not info["truncated"] and available is not None:
            require(available == len(rows), "unreported truncation")
        if query["limit"]:
            require(len(rows) <= query["limit"], "per-code limit exceeded")
        require(info["adjustment"]["mode"] == query["fq"], "adjustment mode mismatch")
        require(info["adjustment"]["anchor_date"] == query["adjustment_anchor"], "adjustment anchor mismatch")
        require(info["adjustment"]["factor_snapshot_id"] == query["factor_snapshot_id"], "factor snapshot mismatch")
        core = info["instrument"]["asset_type"] in {"equity", "etf"}
        if core:
            require(units["price"] == "CNY" and units["amount"] == "CNY", "core monetary units differ")
            correct_volume = "shares" if info["instrument"]["asset_type"] == "equity" else "fund_units"
            require(units["volume"] == correct_volume, "core volume unit differs")
        row_keys = set(query["fields"]) | {"date", "bar_state", "source_id"} | (set() if daily else {"time"})
        positions = []
        for row in rows:
            require(set(row) == row_keys, "projection or mandatory row fields differ")
            position = row["date"] if daily else row["time"]
            positions.append(position)
            require(start <= position <= end, "row outside requested interval")
            if not daily:
                require(row["date"] == datetime.fromisoformat(row["time"]).date().isoformat(), "minute date differs")
            require(row["source_id"] in catalog, "row has no source reference")
            if row["bar_state"] == "final":
                require(bool(catalog[row["source_id"]]["finality_evidence"]), "final bar has no finality evidence")
            for field in query["fields"]:
                value = row[field]
                if value is None:
                    require(not core and bool(units["null_reason"]), "unexplained/null core numeric value")
                elif field in {"open", "high", "low", "close"}:
                    if core and query["fq"] == "none":
                        require(Decimal(value) > 0, "raw core price must be positive")
                    places = len(value.partition(".")[2])
                    require(places <= units["price_decimals"], "price exceeds declared precision")
                elif field == "amount":
                    require(units["amount_decimals"] is not None, "amount precision is unknown")
                    require(len(value.partition(".")[2]) <= units["amount_decimals"], "amount precision mismatch")
            if {"open", "high", "low", "close"} <= row.keys():
                op, hi, lo, cl = (Decimal(row[field]) for field in ("open", "high", "low", "close"))
                require(lo <= min(op, cl) <= max(op, cl) <= hi, "OHLC ordering invalid")
        require(positions == sorted(set(positions)), "duplicate or unsorted bars")
        gap_positions = []
        for gap in gaps:
            position = gap["date"] if daily else gap["time"]
            require(position is not None and start <= position <= end, "gap outside query or missing minute time")
            require(daily == (gap["time"] is None), "gap daily/minute time mismatch")
            gap_positions.append(position)
            for evidence in gap["evidence"]:
                require(evidence["source_id"] in catalog, "gap evidence has no source reference")
        require(len(set(gap_positions)) == len(gap_positions), "duplicate gap")
        require(not set(positions) & set(gap_positions), "bar and gap overlap")
        has_error = any(e["instrument_id"] in (None, key) for e in payload["errors"])
        legal = all(gap["reason"] in LEGAL_GAPS and gap["evidence"] for gap in gaps)
        if coverage["state"] == "complete":
            require(expected is not None and available is not None and legal and not info["truncated"] and not has_error,
                    "coverage falsely complete")
            require(info["instrument"]["identity_verified"], "complete identity is unverified")
            require(info["status"] == ("ok" if rows else "empty"), "complete instrument status mismatch")
        else:
            require(info["status"] == ("partial" if rows else "error"), "incomplete instrument status mismatch")
            if not info["truncated"]:
                require(has_error, "incomplete non-truncated instrument requires a machine error")
        usable |= bool(rows) or (coverage["state"] == "complete" and info["status"] == "empty")
        all_complete &= coverage["state"] == "complete"
    require(payload["truncated"] == any(m["truncated"] for m in meta.values()), "top/per-code truncation mismatch")
    expected_status = "complete" if all_complete and not payload["errors"] and not payload["truncated"] else ("partial" if usable else "failed")
    require(payload["status"] == expected_status, "overall status differs from per-code state")


def validate(payload: dict, definition: str) -> None:
    root = {**SCHEMA, "$ref": f"#/$defs/{definition}"}
    Draft202012Validator(root, format_checker=FORMAT_CHECKER).validate(payload)
    if definition.endswith("Request"):
        validate_request(payload, definition)
    elif definition in {"KlineResponse", "ToolFailureResponse"}:
        validate_kline(payload)
    elif definition in {"StockListResponse", "TradingDaysResponse"}:
        validate_header(payload)
        values = payload["data"] if definition == "StockListResponse" else payload["trading_days"]
        require(payload["data_hash"] == digest(values), "data_hash mismatch")
        if definition == "StockListResponse":
            require(payload["total"] == len(values), "universe total mismatch")
            require(len({v["instrument_id"] for v in values}) == len(values), "duplicate universe id")
            for item in values:
                instrument(item, payload["source_catalog"])
                require(payload["scope"] is not None, "universe rows require scope")
                require(item["asset_type"] in payload["scope"]["asset_types"] and item["exchange"] in payload["scope"]["exchanges"], "universe item outside scope")
            if payload["status"] == "complete":
                require(payload["universe_complete"] and payload["scope"] is not None and payload["as_of_type"] is not None,
                        "complete universe scope is incomplete")
        else:
            require(values == sorted(set(values)), "calendar must be sorted/unique")
            if payload["start"] is not None and payload["end"] is not None:
                start, end = date8(payload["start"]).isoformat(), date8(payload["end"]).isoformat()
                require(start <= end and all(start <= day <= end for day in values), "calendar outside query")
                if payload["status"] == "complete":
                    require(payload["calendar_id"] is not None and payload["coverage_start"] is not None and payload["coverage_end"] is not None,
                            "complete calendar lacks identity or bounds")
                    require(payload["coverage_start"] <= start <= end <= payload["coverage_end"], "calendar request exceeds coverage")
    elif definition == "StatusResponse":
        profiles = {p["id"]: p for p in payload["profiles"]}
        require(len(profiles) == len(payload["profiles"]) and set(profiles) == PROFILES, "profiles must be explicit and unique")
        for p in profiles.values():
            if not p["supported"]:
                require(bool(p["reason"]) and not p["capabilities"], "unsupported profile must have reason and no capabilities")
        for issue in payload["issues"]:
            require(issue["retryable"] == (issue["code"] in RETRYABLE), "status issue retryable differs from error table")
        if payload["status"] == "ready":
            require(profiles["core_daily_v1"]["supported"], "ready requires core support")
        if profiles["core_daily_v1"]["supported"]:
            caps = profiles["core_daily_v1"]["capabilities"]
            require(all(any(c["asset_type"] == asset and c["exchange"] == exchange and c["namespace"] == "exchange"
                            and "1d" in c["frequencies"] and "none" in c["fq_modes"] for c in caps)
                        for asset in ("equity", "etf") for exchange in ("XSHG", "XSHE")), "core capability matrix incomplete")


def rehash(payload: dict) -> None:
    payload["data_hash"] = digest(payload["data"])
    payload["request_hash"] = digest(payload["query"]) if payload["query"] is not None else None


def self_check() -> tuple[int, int]:
    Draft202012Validator.check_schema(SCHEMA)
    examples = json.loads((ROOT / "examples.json").read_text())
    cases = json.loads((ROOT / "acceptance-cases.json").read_text())
    count = 0
    for example in examples["requests"] + examples["responses"]:
        try:
            validate(example["payload"], example["schema"])
        except Exception as exc:
            raise ValueError(f"example {example['name']}: {exc}") from exc
        count += 1
    require([c["id"] for c in cases["core_cases"]] == [f"C{i:02d}" for i in range(1, 25)], "core case ids missing")
    require([c["id"] for c in cases["extension_cases"]] == [f"X{i:02d}" for i in range(1, 6)], "extension ids missing")
    for case in cases["core_cases"]:
        if "request" in case:
            validate(case["request"]["arguments"], "KlineRequest")
    baseline = next(e["payload"] for e in examples["responses"] if e["name"] == "daily_complete")
    equity, etf = baseline["query"]["codes"]
    bad = []

    def add(name, mutate, *, refresh_hash=True):
        candidate = deepcopy(baseline)
        mutate(candidate)
        if refresh_hash:
            rehash(candidate)
        bad.append((name, candidate))

    add("flat single data", lambda p: p.update(data=p["data"][equity]))
    add("hash tamper", lambda p: p["data"][equity][0].update(close="10.30"), refresh_hash=False)
    add("false count", lambda p: p["meta"][equity]["coverage"].update(returned_rows=99))
    add("identity collision", lambda p: p["meta"][equity]["instrument"].update(code="000001"))
    add("ETF shares unit", lambda p: p["meta"][etf]["units"].update(volume="shares"))
    add("missing projection field", lambda p: p["data"][equity][0].pop("close"))
    add("wrong source reference", lambda p: p["data"][equity][0].update(source_id="missing"))
    add("fake finality", lambda p: p["source_catalog"]["s1"].update(finality_evidence=None, unknown_fields=["published_at", "finality_evidence"]))
    add("unknown not disclosed", lambda p: p["source_catalog"]["s1"].update(provider=None))
    add("OHLC invalid", lambda p: p["data"][equity][0].update(high="9.00"))
    add("duplicate dates", lambda p: p["data"][equity][1].update(date="2026-10-20"))
    add("out of interval", lambda p: p["data"][equity][0].update(date="2026-10-19"))
    add("timestamp without timezone", lambda p: p.update(served_at="2026-10-21T16:01:00"))

    def false_empty(p):
        p["data"][etf] = []
        p["meta"][etf]["status"] = "empty"
        p["meta"][etf]["coverage"].update(available_rows=0, returned_rows=0, gaps=[
            {"date": day, "time": None, "reason": "UNKNOWN", "evidence": []}
            for day in ("2026-10-20", "2026-10-21")
        ])
    add("unexplained successful empty", false_empty)
    rejected = 0
    for name, candidate in bad:
        try:
            validate(candidate, "KlineResponse")
        except Exception:
            rejected += 1
        else:
            raise ValueError(f"negative control was accepted: {name}")

    # Legal suspension and an intentional limit are important positive controls.
    legal = deepcopy(baseline)
    false_empty(legal)
    for gap in legal["meta"][etf]["coverage"]["gaps"]:
        gap.update(reason="SUSPENDED", evidence=[{"source_id": "s1", "reference": "urn:fixture:suspension", "known_at": None}])
    rehash(legal)
    validate(legal, "KlineResponse")
    limited = deepcopy(baseline)
    limited.update(status="partial", is_partial=True, truncated=True)
    limited["query"]["limit"] = 1
    for key in limited["query"]["codes"]:
        limited["data"][key] = limited["data"][key][-1:]
        limited["meta"][key].update(status="partial", truncated=True, truncation_reason="caller_limit")
        limited["meta"][key]["coverage"].update(state="incomplete", returned_rows=1)
    rehash(limited)
    validate(limited, "KlineResponse")
    count += 2
    base_request = examples["requests"][0]["payload"]
    invalid_requests = [
        {**base_request, "codes": [equity]}, {**base_request, "code": "000001"},
        {**base_request, "start": "20260230"}, {**base_request, "start": "20261023"},
        {**base_request, "fields": "close,volume"}, {**base_request, "limit": -1},
    ]
    for candidate in invalid_requests:
        try:
            validate(candidate, "KlineRequest")
        except Exception:
            rejected += 1
        else:
            raise ValueError("invalid request control was accepted")
    return count, rejected


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--response", type=Path, help="Unpacked public tool payload JSON; no network access")
    parser.add_argument("--kind", choices=("KlineResponse", "ToolFailureResponse", "StockListResponse", "TradingDaysResponse", "StatusResponse"), default="KlineResponse")
    args = parser.parse_args()
    count, rejected = self_check()
    print(f"PASS: schema and {count} positive examples/controls; {rejected} negative controls rejected; C01-C24/X01-X05 present")
    if args.response:
        validate(json.loads(args.response.read_text()), args.kind)
        print(f"PASS: {args.kind} {args.response}")
    print("Offline contract checks only; live service, evidence truth and deployment acceptance are not implied.")


if __name__ == "__main__":
    main()
