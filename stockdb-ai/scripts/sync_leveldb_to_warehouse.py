#!/usr/bin/env python3
"""Windows 侧 LevelDB 快照全量/按范围同步至 NAS StockDB 分析数仓（Parquet + DuckDB）命令行工具。

示例：
  # 全量导入
  python scripts/sync_leveldb_to_warehouse.py \\
      --snapshot-root D:/leveldb_snapshots/20261112 \\
      --snapshot-id snap_20261112 \\
      --nas-url http://192.168.31.146:8081 \\
      --token "$STOCKDB_WEBUI_TOKEN" \\
      --checkpoint-dir D:/leveldb_snapshots/checkpoints

  # 仅导入近两年（含无日期元信息键）并排除二级索引
  python scripts/sync_leveldb_to_warehouse.py \\
      --snapshot-root D:/leveldb_snapshots/20261112 \\
      --snapshot-id snap_2y_20261112 \\
      --since-date 20241112 \\
      --exclude-prefix kidx: \\
      --nas-url http://192.168.31.146:8081 \\
      --token "$STOCKDB_WEBUI_TOKEN"
"""
from __future__ import annotations

import argparse
import json
import os
import sys
from pathlib import Path

_REPO_AI_DIR = Path(__file__).resolve().parents[1]
if str(_REPO_AI_DIR) not in sys.path:
    sys.path.insert(0, str(_REPO_AI_DIR))

from core.board_metrics import is_supported_a_share_code
from storage.warehouse import bulk_sync_client, leveldb_snapshot

DEFAULT_EXCLUDE_PREFIXES: tuple[str, ...] = ("k资金流:", "资金流:")


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        description="全量或按范围遍历 LevelDB 快照并分批上传 Parquet 到 NAS StockDB 数仓，支持断点续传与 DuckDB 读回核对。"
    )
    parser.add_argument("--snapshot-root", required=True, help="LevelDB 源库目录，或包含多个源库子目录的快照根目录")
    parser.add_argument("--snapshot-id", required=True, help="本次冻结快照标识（如 snap_20261112_1530）")
    parser.add_argument("--nas-url", required=True, help="NAS StockDB HTTP 根地址（如 http://192.168.31.146:8081）")
    parser.add_argument("--token", default=os.environ.get("STOCKDB_WEBUI_TOKEN", ""), help="X-StockDB-Token 认证凭据")
    parser.add_argument("--checkpoint-dir", default="./bulk_sync_checkpoints", help="本地断点进度目录")
    parser.add_argument("--db", action="append", dest="dbs", help="仅同步指定源库名（可重复指定；默认全部）")
    parser.add_argument("--since-date", default=None, help="仅包含键内日期 >= YYYYMMDD 的记录（如 20241112）")
    parser.add_argument("--until-date", default=None, help="仅包含键内日期 <= YYYYMMDD 的记录")
    parser.add_argument("--include-prefix", action="append", default=[], help="仅包含指定 UTF-8 键前缀（可重复）")
    parser.add_argument("--exclude-prefix", action="append", default=[], help="排除指定 UTF-8 键前缀（可重复，如 kidx:；默认已排除 k资金流:/资金流:）")
    parser.add_argument(
        "--include-capital-flow",
        action="store_true",
        help="取消默认排除 k资金流:/资金流:，允许将资金流原始键一并导入数仓",
    )
    parser.add_argument("--exclude-undated", action="store_true", help="配合日期范围使用：排除不带日期的键")
    parser.add_argument(
        "--standard-daily-mode",
        choices=("off", "exclude", "sync-and-exclude"),
        default="off",
        help="日线处理模式：off=进 leveldb_raw（兼容旧行为）；exclude=原库排除 k日k；sync-and-exclude=过滤 A 股回填标准日线并从原库排除 k日k",
    )
    parser.add_argument("--max-rows", type=int, default=None, help="每库最多导入条数上限（适合先小规模试导）")
    parser.add_argument("--batch-size", type=int, default=50000, help="每批最大键值条数（默认 50000）")
    parser.add_argument("--max-batch-bytes", type=int, default=16 * 1024 * 1024, help="每批未压缩键值总字节上限（默认 16MiB）")
    parser.add_argument("--readback-page-size", type=int, default=50000, help="DuckDB 读回对账每页行数（默认 50000）")
    parser.add_argument("--timeout", type=float, default=60.0, help="单次 HTTP 请求超时秒数")
    parser.add_argument("--no-verify-checksums", action="store_true", help="跳过 LevelDB 底层 CRC32C 校验")
    parser.add_argument("--skip-readback", action="store_true", help="跳过最终 DuckDB 读回逐条比对（不建议）")
    args = parser.parse_args(argv)

    exclude_prefix_list = list(args.exclude_prefix or [])
    explicit_cap_flow = bool(args.include_capital_flow) or any(
        str(p).startswith(("k资金流", "资金流")) for p in (args.include_prefix or [])
    )
    if not explicit_cap_flow:
        for pfx in DEFAULT_EXCLUDE_PREFIXES:
            if pfx not in exclude_prefix_list:
                exclude_prefix_list.append(pfx)

    key_filter = leveldb_snapshot.KeyScopeFilter(
        since_date=args.since_date,
        until_date=args.until_date,
        include_prefixes=tuple(p.encode("utf-8") for p in args.include_prefix),
        exclude_prefixes=tuple(p.encode("utf-8") for p in exclude_prefix_list),
        include_undated=not args.exclude_undated,
        max_rows=args.max_rows,
    )
    if key_filter.is_unfiltered():
        key_filter = None

    transport = bulk_sync_client.HttpBulkImportTransport(
        args.nas_url,
        token=args.token,
        timeout=args.timeout,
    )
    report = bulk_sync_client.sync_all_snapshots_to_nas(
        args.snapshot_root,
        snapshot_id=args.snapshot_id,
        transport=transport,
        checkpoint_dir=args.checkpoint_dir,
        db_names=args.dbs,
        batch_size=args.batch_size,
        max_batch_bytes=args.max_batch_bytes,
        readback_page_size=args.readback_page_size,
        verify_checksums=not args.no_verify_checksums,
        verify_readback=not args.skip_readback,
        key_filter=key_filter,
        daily_mode=args.standard_daily_mode,
        daily_code_predicate=is_supported_a_share_code,
    )
    print(json.dumps(report, ensure_ascii=False, indent=2))
    return 0 if report.get("passed") else 2


if __name__ == "__main__":
    raise SystemExit(main())
