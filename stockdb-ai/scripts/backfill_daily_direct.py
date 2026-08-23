#!/usr/bin/env python3
"""全历史日K直连回填（维护脚本，0.10.7；一次性/可重复执行，幂等）。

通道对比（实测）：
  - 按日全市场快照：6300 交易日 × ~19s ≈ 20-30 小时（webui 调度/运维口通道）
  - 按码全历史 vals：`/?cmd=vals&t=日k:<code>:*` 一请求取整段（600000 全历史
    6331 根 0.15s）× 5179 码 ≈ 数分钟——本脚本通道，直连本地引擎 127.0.0.1:7899

两阶段（0.10.7 修订：纯内存转置——任何 code 形态的中转都不落盘）：
  1) 按码拉取全历史 → 内存 DuckDB 表（复用 sink 归一化：全字段原样镜像，NaN→NULL）
  2) 逐日 sink.write_daily → facts/daily/year=YYYY/market=xx/date=YYYYMMDD.parquet
     （分区布局/原子写/幂等跳过与主线完全一致）

行为：已有分区跳过；完成后写 codes 表、推进 watermark。运行前停 webui（避免双写者）。

用法（仓库根）：
  cd stockdb-ai && DATA_DIR=../data NO_PROXY=127.0.0.1,localhost uv run \
      python scripts/backfill_daily_direct.py [--workers 8]
"""
from __future__ import annotations

import json
import sys
import time
from concurrent.futures import ThreadPoolExecutor, as_completed
from pathlib import Path
from urllib.parse import quote

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE.parent))  # stockdb-ai/

import config  # noqa: E402
from storage.providers import free_stockdb  # noqa: E402
from storage.warehouse import catalog, sink  # noqa: E402

STAGING_DIRNAME = ".staging-backfill"
_COLUMN_NAMES = [n for n, _ in sink._DAILY_COLUMNS]  # 维护脚本复用 sink 内部列定义


def stock_codes() -> list[str]:
    raw = free_stockdb.fetch("/?cmd=get&t=" + quote("股票代码"), timeout=30, block=True)
    data = json.loads(raw)
    codes: list[str] = []
    for group in (data or {}).values():
        if isinstance(group, list):
            codes.extend(str(c) for c in group)
    return sorted(set(codes))


def pull_one(code: str):
    """拉单码全历史 → 归一化元组行（纯内存，不落任何中转文件）。"""
    raw = free_stockdb.fetch(
        f"/?cmd=vals&t={quote(f'日k:{code}:*')}", timeout=120, block=True)
    bars = [b for b in json.loads(raw) if isinstance(b, dict)]  # 引擎响应混零星 null
    if not bars:
        return code, None, []
    rows, _sanitized = sink._normalize_rows(bars, sink._DAILY_COLUMNS)
    return code, (bars[-1] or {}).get("name"), rows


def main() -> None:
    workers = 8
    if "--workers" in sys.argv:
        workers = int(sys.argv[sys.argv.index("--workers") + 1])
    root = Path(config.WAREHOUSE_DIR)

    codes = stock_codes()
    print(f"[1/3] 引擎股票表 {len(codes)} 只，按码全历史拉取 → 内存表（workers={workers}）…")

    import duckdb
    con = duckdb.connect()  # 纯内存：转置中转不落盘（0.10.7 修订——任何 code 形态不进仓库目录）
    con.execute("CREATE TABLE t (" + ", ".join(f'"{n}" {t}' for n, t in sink._DAILY_COLUMNS) + ")")

    t0 = time.time()
    names: dict[str, str | None] = {}
    failed = 0
    with ThreadPoolExecutor(max_workers=workers) as pool:
        futs = {pool.submit(pull_one, c): c for c in codes}
        for i, fut in enumerate(as_completed(futs), 1):
            code = futs[fut]
            try:
                _, name, rows = fut.result()
                names[code] = name
                if rows:
                    ph = ", ".join("?" for _ in rows[0])
                    con.executemany(f"INSERT INTO t VALUES ({ph})", rows)
            except Exception as exc:  # noqa: BLE001 - 单码失败不拖垮整批
                failed += 1
                print(f"  ⚠️ {code} 拉取失败: {exc}", flush=True)
            if i % 500 == 0:
                print(f"  进度 {i}/{len(codes)}（{time.time()-t0:.0f}s）", flush=True)
    total = con.execute("SELECT count(*) FROM t").fetchone()[0]
    print(f"[1/3] 完成：{time.time()-t0:.0f}s，内存 {total} 行，失败 {failed} 只")

    dates = [r[0] for r in con.execute("SELECT DISTINCT date FROM t ORDER BY 1").fetchall()]
    print(f"[2/3] 转置 {len(dates)} 个交易日 → 按日分区（幂等跳过已有）…")

    t0 = time.time()
    written = skipped = 0
    col_names = [n for n, _ in sink._DAILY_COLUMNS]
    for i, d in enumerate(dates, 1):
        day = d.strftime("%Y%m%d")
        rows = con.execute("SELECT * FROM t WHERE date = ?", [d]).fetchall()
        day_rows = [dict(zip(col_names, r)) for r in rows]
        result = sink.write_daily(root, day, day_rows)
        if result["status"] == "written":
            written += 1
        elif result["status"] == "skipped":
            skipped += 1
        if i % 1000 == 0:
            print(f"  进度 {i}/{len(dates)}（{time.time()-t0:.0f}s）", flush=True)
    print(f"[2/3] 完成：written {written} 日 / skipped {skipped} 日")

    print("[3/3] codes 表 + watermark …")
    sink.write_codes(root, [{"code": c, "name": n} for c, n in names.items() if n is not None])
    catalog.set_watermark(root, "daily", dates[-1].strftime("%Y%m%d"))
    con.close()

    print("== 回填汇总 ==")
    print(f"  交易日 {len(dates)}（{dates[0]} ~ {dates[-1]}）；总行数 {total}")
    print(f"  分区新增 {written} / 跳过 {skipped}；watermark → {catalog.get_watermark(root, 'daily')}")


    workers = 8
    if "--workers" in sys.argv:
        workers = int(sys.argv[sys.argv.index("--workers") + 1])
    root = Path(config.WAREHOUSE_DIR)
    staging = root / STAGING_DIRNAME
    staging.mkdir(parents=True, exist_ok=True)

    codes = stock_codes()
    print(f"[1/3] 引擎股票表 {len(codes)} 只，按码全历史拉取（workers={workers}）…")

    t0 = time.time()
    names: dict[str, str | None] = {}
    failed: list[str] = []
    with ThreadPoolExecutor(max_workers=workers) as pool:
        futs = {pool.submit(pull_one, c): c for c in codes}
        for i, fut in enumerate(as_completed(futs), 1):
            code = futs[fut]
            try:
                _, n, name = fut.result()
                names[code] = name
                if n == 0 and name is None:
                    failed.append(code)  # 无历史（新股未上市满日/无数据）不算失败，仅记录
            except Exception as exc:  # noqa: BLE001 - 单码失败不拖垮整批
                failed.append(code)
                print(f"  ⚠️ {code} 拉取失败: {exc}", flush=True)
            if i % 500 == 0:
                print(f"  进度 {i}/{len(codes)}（{time.time()-t0:.0f}s）", flush=True)
    print(f"[1/3] 完成：{time.time()-t0:.0f}s，无数据/失败 {len(failed)} 只")

    import duckdb
    con = duckdb.connect()
    glob = f"'{staging.as_posix()}/code=*.parquet'"
    dates = [r[0] for r in con.execute(
        f"SELECT DISTINCT date FROM read_parquet({glob}) ORDER BY 1").fetchall()]
    print(f"[2/3] 转置 {len(dates)} 个交易日 → 分区文件（幂等跳过已有）…")

    t0 = time.time()
    written = skipped = 0
    total_rows = 0
    for i, d in enumerate(dates, 1):
        day = d.strftime("%Y%m%d")
        rows = con.execute(
            f"SELECT * FROM read_parquet({glob}) WHERE date = ?", [d]).fetchall()
        day_rows = [dict(zip(_COLUMN_NAMES, r)) for r in rows]
        result = sink.write_daily(root, day, day_rows)
        if result["status"] == "written":
            written += 1
        elif result["status"] == "skipped":
            skipped += 1
        total_rows += len(day_rows)
        if i % 500 == 0:
            print(f"  进度 {i}/{len(dates)}（{time.time()-t0:.0f}s）", flush=True)
    print(f"[2/3] 完成：written {written} 日 / skipped {skipped} 日 / 总行数 {total_rows}")

    print("[3/3] codes 表 + watermark + staging 清理…")
    sink.write_codes(root, [{"code": c, "name": n} for c, n in names.items() if n is not None])
    catalog.set_watermark(root, "daily", dates[-1].strftime("%Y%m%d"))
    con.close()
    shutil.rmtree(staging, ignore_errors=True)

    print("== 回填汇总 ==")
    print(f"  交易日: {len(dates)}（{dates[0]} ~ {dates[-1]}）")
    print(f"  分区新增 {written} / 已存在跳过 {skipped}；总行数 {total_rows}")
    print(f"  watermark → {catalog.get_watermark(root, 'daily')}")
    print(f"  无数据标的 {len(failed)} 只（新股/无历史，正常）")


if __name__ == "__main__":
    main()
