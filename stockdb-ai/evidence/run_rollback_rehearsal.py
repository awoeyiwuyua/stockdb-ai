#!/usr/bin/env python3
"""Standalone executable script for v1/v2 migration and rollback rehearsal (C22).
Performs end-to-end verification of:
1. Creation of v1 cache fixture
2. Dry-run preview of migration
3. Actual application of v1 -> v2 migration with automatic .v1.bak.json creation
4. Verification of v2 structure and backup integrity
5. Rollback from v2 -> v1 using .v1.bak.json
6. Exact bit-for-bit and semantic comparison of original vs restored cache
Outputs detailed execution log and JSON evidence with exact hashes.
"""
from __future__ import annotations

import argparse
import hashlib
import json
import os
import shutil
import sys
import tempfile
from pathlib import Path

# Setup import path
REPO_ROOT = Path(__file__).resolve().parents[2]
SERVICE = REPO_ROOT / "stockdb-ai"
if str(SERVICE) not in sys.path:
    sys.path.insert(0, str(SERVICE))
if str(SERVICE / "interfaces/mcp") not in sys.path:
    sys.path.insert(0, str(SERVICE / "interfaces/mcp"))

import interfaces.mcp.board_reliability as br


def sha256_file(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def run_rehearsal(work_dir: Path) -> dict:
    work_dir.mkdir(parents=True, exist_ok=True)
    fixture_file = work_dir / "CN_board_eastmoney_BK1408.json"

    # Step 1: Create v1 fixture
    v1_fixture = {
        "instrument_id": "CN/board/eastmoney/BK1408",
        "cache_key": "CN/board/eastmoney/BK1408|1d|none|points|v1",
        "bars": {
            "20261102": {
                "open": "100.5",
                "high": "105.2",
                "low": "99.8",
                "close": "104.0",
                "bar_state": "final"
            },
            "20261103": {
                "open": "104.0",
                "high": "106.0",
                "low": "103.5",
                "close": "105.5",
                "bar_state": "final"
            },
            "20261104": {
                "open": "105.5",
                "high": "108.0",
                "low": "105.0",
                "close": "107.2",
                "bar_state": "final"
            }
        }
    }
    fixture_json = json.dumps(v1_fixture, ensure_ascii=False, indent=2)
    fixture_file.write_text(fixture_json, encoding="utf-8")
    original_sha256 = sha256_file(fixture_file)
    print(f"[Step 1] Created original v1 fixture at {fixture_file}")
    print(f"         SHA256: {original_sha256}")

    # Step 2: Preview migration (dry-run)
    print("\n[Step 2] Running preview (dry-run)...")
    preview_res = br.migrate_v1_to_v2_preview(work_dir)
    print(f"         Preview result: scanned={preview_res.get('scanned')}, eligible={preview_res.get('v1_eligible')}")
    assert preview_res.get("v1_eligible") == 1, "Expected 1 eligible v1 file"

    # Step 3: Apply migration (v1 -> v2)
    print("\n[Step 3] Applying v1 -> v2 migration...")
    apply_res = br.migrate_v1_to_v2_apply(work_dir)
    print(f"         Apply result: converted={apply_res.get('converted')}, failed={apply_res.get('failed')}")
    assert apply_res.get("converted") == 1 and apply_res.get("failed") == 0

    v2_sha256 = sha256_file(fixture_file)
    bak_file = fixture_file.with_suffix(".v1.bak.json")
    assert bak_file.exists(), f"Backup file {bak_file} must exist"
    bak_sha256 = sha256_file(bak_file)
    print(f"         Migrated v2 SHA256: {v2_sha256}")
    print(f"         Backup v1.bak SHA256: {bak_sha256}")
    assert bak_sha256 == original_sha256, "Backup file SHA256 must match original exactly"

    # Verify v2 structure
    v2_content = json.loads(fixture_file.read_text(encoding="utf-8"))
    assert v2_content.get("schema_version") == "board-cache-v2"
    assert "revisions" in v2_content
    print("         v2 schema_version and revisions validated.")

    # Step 4: Rollback (v2 -> v1)
    print("\n[Step 4] Rolling back v2 -> v1...")
    rollback_res = br.rollback_v2_to_v1(work_dir)
    print(f"         Rollback result: restored={rollback_res.get('restored')}, failed={rollback_res.get('failed')}")
    assert rollback_res.get("restored") == 1 and rollback_res.get("failed") == 0

    restored_sha256 = sha256_file(fixture_file)
    print(f"         Restored v1 SHA256: {restored_sha256}")
    assert restored_sha256 == original_sha256, "Restored file SHA256 must match original exactly!"

    # Step 5: Semantic bars comparison
    restored_content = json.loads(fixture_file.read_text(encoding="utf-8"))
    assert restored_content.get("bars") == v1_fixture["bars"], "Bars dictionary must be 100% identical!"
    assert "schema_version" not in restored_content, "v1 restored file must not have schema_version"
    print("\n[Step 5] Exact comparison passed: 100% identical bars and bit-for-bit SHA256 match!")

    evidence = {
        "rehearsal_type": "isolated_directory_v1_v2_migration_and_rollback",
        "status": "PASS",
        "work_dir": str(work_dir),
        "target_file": fixture_file.name,
        "hashes": {
            "original_v1_sha256": original_sha256,
            "backup_v1_bak_sha256": bak_sha256,
            "migrated_v2_sha256": v2_sha256,
            "restored_v1_sha256": restored_sha256
        },
        "verification": {
            "backup_matches_original": bool(bak_sha256 == original_sha256),
            "restored_matches_original_hash": bool(restored_sha256 == original_sha256),
            "restored_bars_equal": bool(restored_content.get("bars") == v1_fixture["bars"])
        },
        "preview_result": preview_res,
        "migration_result": apply_res,
        "rollback_result": rollback_res,
        "commands": [
            "python interfaces/mcp/board_maintenance.py migrate-v1-to-v2",
            "python interfaces/mcp/board_maintenance.py migrate-v1-to-v2 --apply",
            "python interfaces/mcp/board_maintenance.py rollback-v2-to-v1"
        ]
    }
    return evidence


def main():
    parser = argparse.ArgumentParser(description="Run migration and rollback rehearsal")
    parser.add_argument("--work-dir", type=Path, default=None, help="Directory to run rehearsal in")
    parser.add_argument("--output-json", type=Path, default=None, help="Path to write JSON evidence")
    args = parser.parse_args()

    if args.work_dir:
        evidence = run_rehearsal(args.work_dir)
    else:
        with tempfile.TemporaryDirectory() as td:
            evidence = run_rehearsal(Path(td))

    if args.output_json:
        args.output_json.parent.mkdir(parents=True, exist_ok=True)
        args.output_json.write_text(json.dumps(evidence, ensure_ascii=False, indent=2), encoding="utf-8")
        print(f"\nWrote evidence JSON to {args.output_json}")

    return 0


if __name__ == "__main__":
    sys.exit(main())
