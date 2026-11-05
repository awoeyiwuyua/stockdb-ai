#!/usr/bin/env python3
"""reconcile_four_points.py - Controlled 4-Point C19 Reconciliation CLI

Strictly scoped, gated, and deterministic reconciliation tool for the 4 Eastmoney
board daily price discrepancy points discovered in post-deployment verification.

Architecture & Contract Compliance:
1. Strict Whitelist: Only touches exactly 3 series, 4 dates, and 4 fields:
   - CN/board/eastmoney/BK1134: 2026-02-24, field 'low', 2031.22 -> 2031.24 (+0.02)
   - CN/board/eastmoney/BK1037: 2026-08-10, field 'low', 1988.39 -> 1988.42 (+0.03)
   - CN/board/eastmoney/BK0728: 2026-02-24, field 'high', 596.51 -> 596.50 (-0.01)
   - CN/board/eastmoney/BK0728: 2026-06-23, field 'high', 574.93 -> 574.92 (-0.01)
2. In-Lock Old-Value Precondition Gate:
   - Acquires cross_process_file_lock.
   - Verifies prior value strictly matches expected_old (or already expected_new).
   - Rejects unexpected mutations, prevents data corruption.
3. Decimal Equivalence Row Preservation:
   - Compares all numeric values via Decimal.
   - Numerically equivalent values (e.g. 2082 vs 2082.00, 564.1 vs 564.10) preserve
     existing cache string representations, preventing spurious diff revisions.
   - Unrelated dates retain their exact original metadata (raw_hash, source_evidence)
     to avoid spurious bulk metadata rewriting.
4. Authoritative Origin Binding:
   - Binds to push2his.eastmoney.com raw JSON response bodies, observation timestamps,
     and 64-hex SHA256 hashes.
5. Standard Reconcile API:
   - Invokes existing reconcile_series_cache() API in board_reliability.py.
   - Preserves 500-day window boundary, audit append-only revisions, and snapshots immutability.
6. Execution Safety:
   - Defaults to dry-run mode. Requires explicit --apply to write to disk.
   - Automatically creates timestamped pre-reconciliation backup (.bak.json).
   - Idempotent: repeated application produces 0 revisions.
"""

from __future__ import annotations

import argparse
from datetime import datetime, timezone
from decimal import Decimal
import hashlib
import json
import os
from pathlib import Path
import shutil
import sys
from typing import Any

# Ensure stockdb-ai is importable
REPO_ROOT = Path(__file__).resolve().parent.parent.parent
STOCKDB_AI_DIR = REPO_ROOT / "stockdb-ai"
if STOCKDB_AI_DIR.exists() and str(STOCKDB_AI_DIR) not in sys.path:
    sys.path.insert(0, str(STOCKDB_AI_DIR))

try:
    import interfaces.mcp.board_reliability as br
except ImportError:
    try:
        import board_reliability as br
    except ImportError:
        raise RuntimeError("Could not import interfaces.mcp.board_reliability")


# ---------------------------------------------------------------------------
# Authoritative Whitelist & Provenance Bindings
# ---------------------------------------------------------------------------

WHITELIST_TARGETS: dict[str, dict[str, Any]] = {
    "CN/board/eastmoney/BK1134": {
        "dates": {
            "20260224": {
                "field": "low",
                "expected_old": "2031.22",
                "expected_new": "2031.24",
                "date_iso": "2026-02-24",
                "divergence": "+0.02",
            }
        },
        "origin_file": "BK1134-history.body",
        "origin_url": "https://push2his.eastmoney.com/api/qt/stock/kline/get?secid=90.BK1134&fields1=f1,f2,f3,f4,f5,f6&fields2=f51,f52,f53,f54,f55,f56,f57,f58&klt=101&fqt=0&beg=20241001&end=20261105&lmt=1000000",
        "expected_raw_sha256": "6cad3589a1ee31ee1ef9e1f185297873e093e56bc0ecfc4da9cbb7d1e6abf19e",
        "observed_at": "2026-11-05T18:00:27.019064+08:00",
        "provider": "eastmoney",
        "source_evidence": "eastmoney_kline_api",
    },
    "CN/board/eastmoney/BK1037": {
        "dates": {
            "20260810": {
                "field": "low",
                "expected_old": "1988.39",
                "expected_new": "1988.42",
                "date_iso": "2026-08-10",
                "divergence": "+0.03",
            }
        },
        "origin_file": "BK1037-history.body",
        "origin_url": "https://push2his.eastmoney.com/api/qt/stock/kline/get?secid=90.BK1037&fields1=f1,f2,f3,f4,f5,f6&fields2=f51,f52,f53,f54,f55,f56,f57,f58&klt=101&fqt=0&beg=20241001&end=20261105&lmt=1000000",
        "expected_raw_sha256": "bcf87f09cd0e9c2f79bd74abce15fc324cf89584782bdbdafebc28b287790579",
        "observed_at": "2026-11-05T18:00:30.532880+08:00",
        "provider": "eastmoney",
        "source_evidence": "eastmoney_kline_api",
    },
    "CN/board/eastmoney/BK0728": {
        "dates": {
            "20260224": {
                "field": "high",
                "expected_old": "596.51",
                "expected_new": "596.50",
                "date_iso": "2026-02-24",
                "divergence": "-0.01",
            },
            "20260623": {
                "field": "high",
                "expected_old": "574.93",
                "expected_new": "574.92",
                "date_iso": "2026-06-23",
                "divergence": "-0.01",
            },
        },
        "origin_file": "BK0728-history.body",
        "origin_url": "https://push2his.eastmoney.com/api/qt/stock/kline/get?secid=90.BK0728&fields1=f1,f2,f3,f4,f5,f6&fields2=f51,f52,f53,f54,f55,f56,f57,f58&klt=101&fqt=0&beg=20241001&end=20261105&lmt=1000000",
        "expected_raw_sha256": "9a1987ea4bcee017f6e7c353d880afc758f0d78ce54bf5f4f1cacdaa4c8582f8",
        "observed_at": "2026-11-05T18:00:31.746620+08:00",
        "provider": "eastmoney",
        "source_evidence": "eastmoney_kline_api",
    },
}


class PreconditionError(ValueError):
    """Raised when on-disk cache state fails strict precondition checks."""
    pass


def resolve_origin_dir(specified: str | None) -> Path:
    candidates = []
    if specified:
        candidates.append(Path(specified))
    script_dir = Path(__file__).resolve().parent
    candidates.extend([
        script_dir.parent / "evidence" / "origin_bodies",
        REPO_ROOT / "stockdb-candidate-0.17.0-r10-reconcile-proposal" / "evidence" / "origin_bodies",
        Path("C:/Users/75393/Desktop/mingli/tmp/stockdb-r10-live-20261105/origin-full"),
        Path("/vol1/stockdb/origin-full"),
        Path("/data/origin-full"),
    ])
    for c in candidates:
        if c.exists() and (c / "BK1134-history.body").exists():
            return c
    raise FileNotFoundError(f"Could not locate origin bodies directory. Checked: {[str(c) for c in candidates]}")


def parse_origin_klines(origin_path: Path, expected_sha256: str) -> dict[str, dict[str, str]]:
    raw_bytes = origin_path.read_bytes()
    actual_sha256 = hashlib.sha256(raw_bytes).hexdigest()
    if actual_sha256 != expected_sha256:
        raise PreconditionError(
            f"Origin body hash mismatch on {origin_path.name}: expected {expected_sha256}, got {actual_sha256}"
        )
    data = json.loads(raw_bytes.decode("utf-8"))
    klines = (data.get("data") or {}).get("klines") or []
    bars_by_date = {}
    for line in klines:
        parts = str(line).split(",")
        if len(parts) >= 5:
            d = parts[0].replace("-", "")
            bars_by_date[d] = {
                "date8": d,
                "open": parts[1],
                "close": parts[2],
                "high": parts[3],
                "low": parts[4],
            }
    return bars_by_date


def prepare_aligned_rows(
    key: str,
    existing_store: dict[str, Any],
    origin_bars: dict[str, dict[str, str]],
    raw_sha256: str,
) -> tuple[list[dict[str, Any]], list[dict[str, Any]], int]:
    cfg = WHITELIST_TARGETS[key]
    existing_bars: dict[str, dict[str, Any]] = existing_store.get("bars", {})
    if len(existing_bars) != 500:
        raise PreconditionError(f"{key}: existing cache bar count must be 500, found {len(existing_bars)}")

    aligned_rows = []
    planned_diffs = []
    already_reconciled_count = 0

    sorted_dates = sorted(existing_bars.keys())
    for d in sorted_dates:
        p_bar = existing_bars[d]
        if d not in origin_bars:
            raise PreconditionError(f"Date {d} from existing cache not found in origin klines for {key}")
        o_bar = origin_bars[d]

        # For whitelisted revision dates, use the new raw_hash and evidence
        # For unrelated dates, preserve existing metadata to prevent bulk metadata mutations
        is_target_date = d in cfg["dates"]
        row_raw_hash = raw_sha256 if is_target_date else p_bar.get("raw_hash", raw_sha256)
        row_evidence = cfg["source_evidence"] if is_target_date else p_bar.get("source_evidence", cfg["source_evidence"])
        row_archive = p_bar.get("archive_status", "archived")

        candidate_row = {
            "date8": d,
            "open": p_bar.get("open"),
            "high": p_bar.get("high"),
            "low": p_bar.get("low"),
            "close": p_bar.get("close"),
            "bar_state": "final",
            "source_evidence": row_evidence,
            "archive_status": row_archive,
            "raw_hash": row_raw_hash,
        }

        # Check if this date has a whitelisted target point
        if is_target_date:
            spec = cfg["dates"][d]
            f = spec["field"]
            p_val = str(p_bar.get(f))
            if p_val == spec["expected_new"]:
                already_reconciled_count += 1
                planned_diffs.append({
                    "key": key,
                    "date": d,
                    "date_iso": spec["date_iso"],
                    "field": f,
                    "prior_value": p_val,
                    "target_value": spec["expected_new"],
                    "divergence": spec["divergence"],
                    "origin_url": cfg["origin_url"],
                    "origin_sha256": raw_sha256,
                    "status": "ALREADY_RECONCILED",
                })
            elif p_val == spec["expected_old"]:
                candidate_row[f] = spec["expected_new"]
                planned_diffs.append({
                    "key": key,
                    "date": d,
                    "date_iso": spec["date_iso"],
                    "field": f,
                    "prior_value": p_val,
                    "target_value": spec["expected_new"],
                    "divergence": spec["divergence"],
                    "origin_url": cfg["origin_url"],
                    "origin_sha256": raw_sha256,
                    "status": "NEEDS_RECONCILE",
                })
            else:
                raise PreconditionError(
                    f"Precondition failed on {key} {d} {f}: current '{p_val}' does not match "
                    f"expected_old '{spec['expected_old']}' or expected_new '{spec['expected_new']}'"
                )

        # Field-by-field decimal comparison across all 4 prices
        for f in ("open", "high", "low", "close"):
            p_val = str(p_bar.get(f))
            o_val = str(o_bar.get(f))
            dec_p = Decimal(p_val)
            dec_o = Decimal(o_val)

            if dec_p == dec_o:
                # Decimal equal: retain exact cache string representation
                candidate_row[f] = p_val
            else:
                # Numerical discrepancy!
                if not is_target_date or f != cfg["dates"][d]["field"]:
                    raise PreconditionError(
                        f"Unauthorized discrepancy in {key} on date {d} field '{f}': "
                        f"cache={p_val}, origin={o_val}"
                    )

        aligned_rows.append(candidate_row)

    return aligned_rows, planned_diffs, already_reconciled_count


def reconcile_board(
    key: str,
    origin_dir: Path,
    cache_dir: Path | None = None,
    *,
    apply: bool = False,
    reason: str = "verified_origin_align",
) -> dict[str, Any]:
    cfg = WHITELIST_TARGETS[key]
    c_dir = cache_dir or br.get_board_cache_dir()
    safe_key = key.replace("/", "_")
    cache_path = c_dir / f"{safe_key}.json"
    lock_path = c_dir / f"{safe_key}.lock"

    if not cache_path.exists():
        raise FileNotFoundError(f"Cache file {cache_path} does not exist")

    origin_p = origin_dir / cfg["origin_file"]
    origin_bars = parse_origin_klines(origin_p, cfg["expected_raw_sha256"])

    with br.cross_process_file_lock(lock_path):
        # 1. In-lock reload of existing store
        existing_store = br.load_board_cache(key, cache_dir=c_dir)
        aligned_rows, diffs, already_count = prepare_aligned_rows(
            key, existing_store, origin_bars, cfg["expected_raw_sha256"]
        )

        needs_reconcile = [d for d in diffs if d["status"] == "NEEDS_RECONCILE"]
        expected_revisions = len(needs_reconcile)

        result: dict[str, Any] = {
            "key": key,
            "mode": "apply" if apply else "dry_run",
            "cache_file": str(cache_path),
            "planned_diffs": diffs,
            "revisions_needed": expected_revisions,
            "already_reconciled": already_count,
            "total_bars": len(aligned_rows),
        }

        if not apply:
            result["status"] = "DRY_RUN_SUCCESS"
            result["revisions_count"] = 0
            result["message"] = f"Dry-run passed. {expected_revisions} revisions planned; {already_count} already satisfied."
            return result

        # 2. Apply mode: create timestamped pre-reconcile backup first
        ts = datetime.now(timezone.utc).strftime("%Y%m%d_%H%M%S")
        backup_path = cache_path.with_suffix(f".pre_reconcile_{ts}.bak.json")
        raw_bytes = cache_path.read_bytes()
        backup_path.write_bytes(raw_bytes)
        result["backup_file"] = str(backup_path)
        result["backup_sha256"] = hashlib.sha256(raw_bytes).hexdigest()

        # 3. Call standard reconcile_series_cache
        api_res = br.reconcile_series_cache(
            key,
            aligned_rows,
            provider=cfg["provider"],
            reason=reason,
            raw_hash=None,  # Allow per-row raw_hash so unrelated bars remain immutable
            cache_dir=c_dir,
        )

        result["api_response"] = api_res
        result["revisions_count"] = api_res.get("revisions_count", 0)
        result["revised_dates"] = api_res.get("revised_dates", [])

        if api_res.get("status") != "success":
            # Rollback
            shutil.copy2(backup_path, cache_path)
            raise RuntimeError(f"Reconcile API failed for {key}: {api_res}")

        if api_res.get("revisions_count") != expected_revisions:
            # Rollback
            shutil.copy2(backup_path, cache_path)
            raise RuntimeError(
                f"Reconcile API revision count mismatch for {key}: expected {expected_revisions}, got {api_res.get('revisions_count')}"
            )

        # 4. Post-check on-disk file
        post_store = br.load_board_cache(key, cache_dir=c_dir)
        for d_spec in cfg["dates"].values():
            d8 = d_spec["date_iso"].replace("-", "")
            fld = d_spec["field"]
            actual_val = post_store.get("bars", {}).get(d8, {}).get(fld)
            if actual_val != d_spec["expected_new"]:
                shutil.copy2(backup_path, cache_path)
                raise RuntimeError(
                    f"Post-verification failed on {key} {d8} {fld}: expected {d_spec['expected_new']}, found {actual_val}"
                )

        result["status"] = "APPLIED_SUCCESS"
        result["new_cache_sha256"] = hashlib.sha256(cache_path.read_bytes()).hexdigest()
        result["message"] = f"Successfully reconciled {expected_revisions} points. Disk cache verified."
        return result


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--apply", action="store_true", help="Commit revisions to disk (default: dry-run only)")
    parser.add_argument("--cache-dir", type=Path, default=None, help="Directory containing board daily cache JSON files")
    parser.add_argument("--origin-dir", type=str, default=None, help="Directory containing origin .body files")
    parser.add_argument("--reason", type=str, default="verified_origin_align", help="Audit reason recorded in revisions")
    parser.add_argument("--series", type=str, default=None, help="Optionally target a single series from the whitelist")
    parser.add_argument("--json", action="store_true", help="Print structured JSON output")
    args = parser.parse_args()

    try:
        origin_dir = resolve_origin_dir(args.origin_dir)
    except Exception as exc:
        print(f"ERROR: {exc}", file=sys.stderr)
        return 1

    target_keys = [args.series] if args.series else list(WHITELIST_TARGETS.keys())
    for k in target_keys:
        if k not in WHITELIST_TARGETS:
            print(f"ERROR: Series '{k}' is outside the authorized 3-series whitelist: {list(WHITELIST_TARGETS.keys())}", file=sys.stderr)
            return 1

    results = []
    has_error = False

    for key in target_keys:
        try:
            res = reconcile_board(
                key,
                origin_dir,
                cache_dir=args.cache_dir,
                apply=args.apply,
                reason=args.reason,
            )
            results.append(res)
        except Exception as exc:
            has_error = True
            results.append({
                "key": key,
                "status": "FAIL",
                "error": str(exc),
                "error_type": type(exc).__name__,
            })

    if args.json:
        print(json.dumps(results, ensure_ascii=False, indent=2))
    else:
        mode_str = "APPLY" if args.apply else "DRY-RUN (PREVIEW)"
        print(f"\n=======================================================")
        print(f"  StockDB 4-Point C19 Reconciliation Tool [{mode_str}]")
        print(f"=======================================================")
        print(f"Origin Directory: {origin_dir}")
        print(f"Target Series:    {len(target_keys)}")
        print("-------------------------------------------------------")
        for res in results:
            k = res["key"]
            st = res.get("status")
            if st in ("DRY_RUN_SUCCESS", "APPLIED_SUCCESS"):
                revs = res.get("revisions_needed", 0) if not args.apply else res.get("revisions_count", 0)
                already = res.get("already_reconciled", 0)
                print(f"[{st:16s}] {k}")
                print(f"  Revisions: {revs} | Already Reconciled: {already} | Total Bars: {res.get('total_bars')}")
                for d in res.get("planned_diffs", []):
                    dt = d["date_iso"]
                    f = d["field"]
                    pv = d["prior_value"]
                    tv = d["target_value"]
                    div = d["divergence"]
                    diff_st = d["status"]
                    print(f"    - {dt} {f:4s}: {pv:>8s} -> {tv:<8s} ({div:>5s}) [{diff_st}]")
            else:
                print(f"[FAILED          ] {k}: {res.get('error')}")
        print("-------------------------------------------------------")
        if has_error:
            print("RECONCILIATION RESULT: FAILED")
            return 1
        elif not args.apply:
            print("DRY-RUN PASSED. Run with --apply to commit revisions to disk.")
            return 0
        else:
            print("ALL REVISIONS APPLIED AND VERIFIED SUCCESSFULLY.")
            return 0


if __name__ == "__main__":
    sys.exit(main())
