#!/usr/bin/env python3
"""board_maintenance — 板块可靠性运维与迁移 CLI 工具。

支持命令：
  - status: 查看各提供者闸门、补采 worker、快照与持久化健康状态
  - backfill: 手动触发一轮缺口增量补采（支持 --force 穿透冷却，支持指定 --series）
  - migrate-v1-to-v2: v1 扁平缓存至 v2 审计链与校验格式迁移（默认 preview，带 --apply 执行）
  - rollback-v2-to-v1: 将 v2 缓存安全回滚至迁移前备份的 v1 格式
  - cleanup-stale-tmp: 清理因进程异常崩溃留存的过期 .tmp 临时文件
"""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path
from typing import Any

# 确保 interfaces/mcp 可直接导入
_THIS_DIR = Path(__file__).resolve().parent
_AI_DIR = _THIS_DIR.parent.parent
if str(_AI_DIR) not in sys.path:
    sys.path.insert(0, str(_AI_DIR))
if str(_THIS_DIR) not in sys.path:
    sys.path.insert(0, str(_THIS_DIR))

import interfaces.mcp.board_reliability as br


def cmd_status(args: argparse.Namespace) -> int:
    ext = br.build_board_reliability_extension()
    if args.json:
        print(json.dumps(ext, ensure_ascii=False, indent=2))
    else:
        print("=== StockDB Board Reliability Status ===")
        print(f"Target Trade Date (Shanghai): {ext.get('target_trade_date')}")
        clock = ext.get("shanghai_clock", {})
        print(f"Shanghai Clock: {clock.get('shanghai_time')} (T={clock.get('target_t')}, is_today={clock.get('is_today')}, frozen={clock.get('frozen_after_1500')})")
        print("\n--- Source Gate Health ---")
        for src, status in ext.get("source_health", {}).items():
            cd = f"in_cooldown({status.get('cooldown_remaining_s')}s)" if status.get("in_cooldown") else "healthy"
            print(f"  {src:12s}: {cd:<20s} streak={status.get('backoff_streak')} ok={status.get('requests_ok')}/{status.get('requests_total')}")
        print("\n--- Persistence Health ---")
        for k, v in ext.get("persistence_health", {}).items():
            print(f"  {k:24s}: {v}")
        print("\n--- Worker State ---")
        worker = ext.get("worker_state", {})
        print(f"  enabled={worker.get('enabled')} running={worker.get('running')} active_task={worker.get('active_task')}")
        if worker.get("last_summary"):
            print(f"  last_cycle_summary: {json.dumps(worker.get('last_summary'), ensure_ascii=False)}")
    return 0


def cmd_backfill(args: argparse.Namespace) -> int:
    print(f"Triggering board backfill cycle (force={args.force}, series={args.series})...", file=sys.stderr)
    if args.series:
        # 指定单序列补采
        import interfaces.mcp.market_v3_research as r
        key = args.series
        if key in r.BOARD_SERIES:
            meta = r.BOARD_SERIES[key]
            rows, ci = r.board_daily_cached(key, meta["secid"], "20240101", br.compute_target_trade_date_shanghai())
        elif key in r.THS_BOARD_SERIES:
            meta = r.THS_BOARD_SERIES[key]
            rows, ci = r.ths_board_daily_cached(key, meta["code"], "20240101", br.compute_target_trade_date_shanghai())
        else:
            print(f"Unknown series key: {key}", file=sys.stderr)
            return 1
        summary = {"series": key, "rows_count": len(rows), "cache_info": ci}
    else:
        summary = br.WORKER.run_once(force=args.force)

    if args.json:
        print(json.dumps(summary, ensure_ascii=False, indent=2))
    else:
        print("Backfill completed:")
        print(json.dumps(summary, ensure_ascii=False, indent=2))
    return 0


def cmd_migrate_v1_to_v2(args: argparse.Namespace) -> int:
    cache_dir = Path(args.cache_dir) if args.cache_dir else None
    if args.apply:
        print("Applying migration from v1 to v2 format...", file=sys.stderr)
        res = br.migrate_v1_to_v2_apply(cache_dir)
    else:
        print("Previewing migration from v1 to v2 format (dry-run)...", file=sys.stderr)
        res = br.migrate_v1_to_v2_preview(cache_dir)

    if args.json:
        print(json.dumps(res, ensure_ascii=False, indent=2))
    else:
        print(f"Scanned files: {res.get('scanned')}")
        print(f"Eligible for upgrade: {res.get('v1_eligible')}")
        print(f"Already v2: {res.get('v2_already')}")
        if args.apply:
            print(f"Successfully converted: {res.get('converted')}")
            print(f"Failed conversions: {res.get('failed')}")
            if res.get("backup_files"):
                print("Backups created:")
                for b in res["backup_files"]:
                    print(f"  {b}")
        else:
            print("(Dry-run completed. Pass --apply to perform actual migration)")
    return 0 if not res.get("failed") else 1


def cmd_rollback_v2_to_v1(args: argparse.Namespace) -> int:
    cache_dir = Path(args.cache_dir) if args.cache_dir else None
    print("Rolling back v2 format to v1 format using .v1.bak.json...", file=sys.stderr)
    res = br.rollback_v2_to_v1(cache_dir)
    if args.json:
        print(json.dumps(res, ensure_ascii=False, indent=2))
    else:
        print(f"Restored files: {res.get('restored')}")
        print(f"Failed restorations: {res.get('failed')}")
        if res.get("restored_files"):
            for f in res["restored_files"]:
                print(f"  {f}")
    return 0 if not res.get("failed") else 1


def cmd_cleanup_stale_tmp(args: argparse.Namespace) -> int:
    d = Path(args.dir) if args.dir else br.get_board_cache_dir()
    cleaned = br.cleanup_stale_tmp_files(d, max_age_seconds=args.max_age_seconds)
    print(f"Cleaned {cleaned} stale .tmp files in {d}")
    return 0


def _parse_origin_dir_rows(origin_dir: Path, key: str, start8: str, end8: str) -> list[dict[str, Any]]:
    import hashlib
    import re
    code = key.split("/")[-1]
    is_ths = "/ths/" in key
    rows = []
    if is_ths:
        rows_by_date = {}
        for year in ("2024", "2025", "2026"):
            f = origin_dir / f"{code}-{year}.body"
            if not f.exists():
                continue
            text = f.read_text(encoding="utf-8", errors="ignore")
            raw_hash = hashlib.sha256(text.encode("utf-8")).hexdigest()
            m = re.search(r'"data":\s*"([^"]+)"', text)
            data_str = m.group(1) if m else text
            for item in data_str.split(";"):
                parts = item.strip().split(",")
                if len(parts) >= 5 and len(parts[0]) == 8 and parts[0].isdigit():
                    d = parts[0]
                    if start8 <= d <= end8:
                        rows_by_date[d] = {
                            "date8": d,
                            "open": parts[1],
                            "high": parts[2],
                            "low": parts[3],
                            "close": parts[4],
                            "bar_state": "final",
                            "source_evidence": "yearly_archive",
                            "archive_status": "archived",
                            "raw_hash": raw_hash,
                        }
        # 检查 today.body
        today_f = None
        for candidate in (origin_dir / f"{code}-today.body", origin_dir.parent / f"{code}-today.body"):
            if candidate.exists():
                today_f = candidate
                break
        if today_f and today_f.exists():
            today_text = today_f.read_text(encoding="utf-8", errors="ignore")
            today_hash = hashlib.sha256(today_text.encode("utf-8")).hexdigest()
            m = re.search(r"\{.*\}", today_text)
            if m:
                try:
                    obj = json.loads(m.group(0))
                    val = obj.get(f"bk_{code}") or {}
                    d_today = str(val.get("1") or "")
                    if d_today and (start8 <= d_today <= end8) and val.get("7") and val.get("11"):
                        # 如果年度包中没有该日期，则采用 today.body 补充，并标记 pending_yearly_archive
                        if d_today not in rows_by_date:
                            rows_by_date[d_today] = {
                                "date8": d_today,
                                "open": str(val["7"]),
                                "high": str(val.get("8") or val["7"]),
                                "low": str(val.get("9") or val["7"]),
                                "close": str(val["11"]),
                                "bar_state": "final" if int(val.get("open") or 0) != 1 else "preliminary",
                                "source_evidence": "today_js",
                                "archive_status": "pending_yearly_archive",
                                "raw_hash": today_hash,
                            }
                except Exception:
                    pass
        for d in sorted(rows_by_date.keys()):
            rows.append(rows_by_date[d])
    else:
        f = origin_dir / f"{code}-history.body"
        if f.exists():
            text = f.read_text(encoding="utf-8")
            raw_hash = hashlib.sha256(text.encode("utf-8")).hexdigest()
            data = json.loads(text)
            klines = (data.get("data") or {}).get("klines") or []
            for line in klines:
                parts = str(line).split(",")
                if len(parts) >= 5:
                    d = parts[0].replace("-", "")
                    if start8 <= d <= end8:
                        rows.append({
                            "date8": d,
                            "open": parts[1],
                            "close": parts[2],
                            "high": parts[3],
                            "low": parts[4],
                            "bar_state": "final",
                            "source_evidence": "eastmoney_kline_api",
                            "archive_status": "archived",
                            "raw_hash": raw_hash,
                        })
    return rows


def cmd_reconcile(args: argparse.Namespace) -> int:
    import interfaces.mcp.market_v3_research as r
    target_keys = (
        [args.series] if args.series
        else [
            "CN/board/eastmoney/BK1408",
            "CN/board/eastmoney/BK1134",
            "CN/board/eastmoney/BK1138",
            "CN/board/eastmoney/BK1101",
            "CN/board/eastmoney/BK1037",
            "CN/board/eastmoney/BK0728",
            "CN/board/ths/885809",
            "CN/board/ths/886069",
            "CN/board/ths/885957",
            "CN/board/ths/886044",
            "CN/board/ths/886009",
            "CN/board/ths/881124",
            "CN/board/ths/881181",
        ]
    )
    start8 = args.start or "20241014"
    end8 = args.end or br.compute_target_trade_date_shanghai()
    results = []
    c_dir = Path(args.cache_dir) if args.cache_dir else None
    origin_dir = Path(args.origin_dir) if getattr(args, "origin_dir", None) else None

    for key in target_keys:
        is_ths = "/ths/" in key
        code = key.split("/")[-1]
        try:
            rows = []
            reason = "maintenance_reconcile"
            if origin_dir and origin_dir.exists():
                rows = _parse_origin_dir_rows(origin_dir, key, start8, end8)
                if rows:
                    reason = "verified_origin_align"
            if not rows:
                if is_ths:
                    rows = r.fetch_ths_board_daily(code, start8, end8)
                    provider = "ths"
                else:
                    secid = r.BOARD_SERIES[key]["secid"]
                    rows = r.eastmoney_board_daily(secid, start8, end8)
                    provider = "eastmoney"
            else:
                provider = "ths" if is_ths else "eastmoney"

            res = br.reconcile_series_cache(key, rows, provider=provider, reason=reason, cache_dir=c_dir)
            results.append(res)
        except Exception as exc:
            results.append({"key": key, "error": str(exc), "status": "FAIL"})

    if args.json:
        print(json.dumps(results, ensure_ascii=False, indent=2))
    else:
        for res in results:
            print(f"{res.get('key')}: revisions={res.get('revisions_count', 0)}, revised_dates={res.get('revised_dates', [])}, error={res.get('error')}")
    return 0


def main() -> None:
    parser = argparse.ArgumentParser(description="StockDB Board Reliability Maintenance CLI")
    parser.add_argument("--json", action="store_true", help="输出 JSON 格式")
    subparsers = parser.add_subparsers(dest="command", required=True)

    # status
    sub = subparsers.add_parser("status", help="查看可靠性与健康状态")
    sub.add_argument("--json", action="store_true", help="输出 JSON 格式")
    sub.set_defaults(func=cmd_status)

    # backfill
    sub = subparsers.add_parser("backfill", help="手动执行缺口补采")
    sub.add_argument("--json", action="store_true", help="输出 JSON 格式")
    sub.add_argument("--force", action="store_true", help="强制穿透提供者冷却期")
    sub.add_argument("--series", type=str, default=None, help="指定单序列规范键（如 CN/board/eastmoney/BK1408）")
    sub.set_defaults(func=cmd_backfill)

    # reconcile
    sub = subparsers.add_parser("reconcile", help="历史修订巡检与收盘确认对齐（更新错误行并记入修订链）")
    sub.add_argument("--json", action="store_true", help="输出 JSON 格式")
    sub.add_argument("--series", type=str, default=None, help="指定单序列规范键（默认全量 13 板块）")
    sub.add_argument("--start", type=str, default="20241014", help="起始日期（YYYYMMDD）")
    sub.add_argument("--end", type=str, default=None, help="结束日期（YYYYMMDD，默认当前收盘目标日）")
    sub.add_argument("--cache-dir", type=str, default=None, help="指定缓存目录（默认系统配置目录）")
    sub.add_argument("--origin-dir", type=str, default=None, help="指定原来源文件目录进行对齐（如 consumer origin-full）")
    sub.set_defaults(func=cmd_reconcile)

    # migrate-v1-to-v2
    sub = subparsers.add_parser("migrate-v1-to-v2", help="缓存升级迁移：v1 -> v2")
    sub.add_argument("--json", action="store_true", help="输出 JSON 格式")
    sub.add_argument("--apply", action="store_true", help="执行实际写入（默认仅预览 dry-run）")
    sub.add_argument("--cache-dir", type=str, default=None, help="指定缓存目录（默认系统配置目录）")
    sub.set_defaults(func=cmd_migrate_v1_to_v2)

    # rollback-v2-to-v1
    sub = subparsers.add_parser("rollback-v2-to-v1", help="缓存回滚：v2 -> v1")
    sub.add_argument("--json", action="store_true", help="输出 JSON 格式")
    sub.add_argument("--cache-dir", type=str, default=None, help="指定缓存目录（默认系统配置目录）")
    sub.set_defaults(func=cmd_rollback_v2_to_v1)

    # cleanup-stale-tmp
    sub = subparsers.add_parser("cleanup-stale-tmp", help="清理未落盘的陈旧 .tmp 文件")
    sub.add_argument("--dir", type=str, default=None, help="清理目录")
    sub.add_argument("--max-age-seconds", type=float, default=3600.0, help="清理阈值秒数（默认 3600）")
    sub.set_defaults(func=cmd_cleanup_stale_tmp)

    parsed = parser.parse_args()
    sys.exit(parsed.func(parsed))


if __name__ == "__main__":
    main()
