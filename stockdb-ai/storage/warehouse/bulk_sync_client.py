"""storage.warehouse.bulk_sync_client — Windows 侧全量读取 LevelDB 快照、断点续传至 NAS 并从 DuckDB 读回逐库验收。

关键保障：
1. Windows 与 NAS 不并发打开同一 `warehouse.duckdb`：Windows 仅生成并上传 Parquet 批次，
   所有落盘、DuckDB 入仓与读回均由 NAS HTTP 接口完成。
2. 先确认、后推进进度（Write-Ahead Checkpoint）：每批先写入 `pending_batch`，待 NAS 返回
   `committed`/`duplicate_skipped` 且摘要一致后才推进 `last_confirmed_key_b64` 与 `next_seq`。
3. 断点/断网续传：重启后先拉取 NAS `/status`，若 `pending_batch` 已在 NAS 落盘且摘要匹配则自动晋升；
   同批重传完全幂等；跳过已确认前缀（利用 SSTable 索引定位）从下一键继续。
4. 终局验收：从 NAS DuckDB 读回（`v_leveldb_raw`）与本地冻结 LevelDB 快照逐条比对，
   缺键、多键、错值、重键全部为 0 才判通过。
"""
from __future__ import annotations

import base64
import hashlib
import json
import os
import tempfile
import time
from collections.abc import Callable, Sequence
from pathlib import Path
from typing import Any, Protocol

import requests

from . import bulk_import, daily_backfill, leveldb_snapshot


class BulkImportTransport(Protocol):
    def get_status(self, snapshot_id: str, source_db: str) -> dict[str, Any]: ...
    def upload_batch(self, meta: dict[str, Any], parquet_bytes: bytes) -> dict[str, Any]: ...
    def complete_import(
        self,
        snapshot_id: str,
        source_db: str,
        *,
        expected_batches: int,
        expected_total_rows: int,
        source_fingerprint: str,
    ) -> dict[str, Any]: ...
    def readback_page(
        self, snapshot_id: str, source_db: str, *, after_seq: int, limit: int
    ) -> tuple[bytes, dict[str, Any]]: ...
    def verify_import(
        self,
        snapshot_id: str,
        source_db: str,
        *,
        expected_total_rows: int,
        source_fingerprint: str,
    ) -> dict[str, Any]: ...
    def upload_daily_backfill_batch(
        self,
        meta: dict[str, Any],
        parquet_bytes: bytes,
        *,
        overwrite_existing: bool = False,
    ) -> dict[str, Any]: ...
    def readback_daily(self, dates: list[str]) -> tuple[bytes, dict[str, Any]]: ...


class TransportError(RuntimeError):
    """HTTP / 传输层错误。"""


class HttpBulkImportTransport:
    """面向 NAS StockDB WebUI API 的带重试 HTTP 传输器。"""

    def __init__(
        self,
        base_url: str,
        *,
        token: str = "",
        timeout: float = 60.0,
        max_retries: int = 3,
        retry_backoff_sec: float = 0.5,
        session: requests.Session | None = None,
    ) -> None:
        self.base_url = base_url.rstrip("/")
        self.token = token
        self.timeout = timeout
        self.max_retries = max(0, int(max_retries))
        self.retry_backoff_sec = retry_backoff_sec
        self.session = session or requests.Session()

    def _headers(self, extra: dict[str, str] | None = None) -> dict[str, str]:
        hdrs: dict[str, str] = {}
        if self.token:
            hdrs["X-StockDB-Token"] = self.token
        if extra:
            hdrs.update(extra)
        return hdrs

    def _request(self, method: str, path: str, **kwargs) -> requests.Response:
        url = f"{self.base_url}{path}"
        last_exc: Exception | None = None
        for attempt in range(self.max_retries + 1):
            try:
                resp = self.session.request(method, url, timeout=self.timeout, **kwargs)
                if resp.status_code in (502, 503, 504) and attempt < self.max_retries:
                    time.sleep(self.retry_backoff_sec * (2**attempt))
                    continue
                return resp
            except requests.RequestException as exc:
                last_exc = exc
                if attempt < self.max_retries:
                    time.sleep(self.retry_backoff_sec * (2**attempt))
                    continue
                raise TransportError(f"请求失败 {method} {url}: {exc}") from exc
        raise TransportError(f"请求失败 {method} {url}: {last_exc}")

    @staticmethod
    def _decode_json(resp: requests.Response) -> dict[str, Any]:
        try:
            data = resp.json()
        except ValueError:
            data = {"ok": False, "error": resp.text}
        if resp.status_code == 409:
            raise bulk_import.BulkImportConflictError(data.get("error") or resp.text)
        if resp.status_code == 400:
            raise bulk_import.BulkImportError(data.get("error") or resp.text)
        if not resp.ok or not data.get("ok", False):
            raise TransportError(f"HTTP {resp.status_code}: {data}")
        return data

    def get_status(self, snapshot_id: str, source_db: str) -> dict[str, Any]:
        resp = self._request(
            "GET",
            "/api/warehouse/bulk-import/status",
            params={"snapshot_id": snapshot_id, "source_db": source_db},
            headers=self._headers(),
        )
        return self._decode_json(resp)

    def upload_batch(self, meta: dict[str, Any], parquet_bytes: bytes) -> dict[str, Any]:
        headers = self._headers(
            {
                "Content-Type": "application/octet-stream",
                "X-StockDB-Snapshot-Id": str(meta["snapshot_id"]),
                "X-StockDB-Source-Db": str(meta["source_db"]),
                "X-StockDB-Batch-Id": str(meta["batch_id"]),
                "X-StockDB-Batch-Index": str(meta["batch_index"]),
                "X-StockDB-Parquet-Sha256": str(meta["parquet_sha256"]),
                "X-StockDB-Row-Count": str(meta["row_count"]),
                "X-StockDB-Seq-Start": str(meta["seq_start"]),
                "X-StockDB-Seq-End": str(meta["seq_end"]),
                "X-StockDB-Rows-Digest": str(meta["rows_digest_sha256"]),
                "X-StockDB-Source-Fingerprint": str(meta.get("source_fingerprint") or ""),
            }
        )
        resp = self._request(
            "POST",
            "/api/warehouse/bulk-import/batch",
            params={
                "snapshot_id": meta["snapshot_id"],
                "source_db": meta["source_db"],
                "batch_id": meta["batch_id"],
                "batch_index": str(meta["batch_index"]),
            },
            headers=headers,
            data=parquet_bytes,
        )
        return self._decode_json(resp)

    def complete_import(
        self,
        snapshot_id: str,
        source_db: str,
        *,
        expected_batches: int,
        expected_total_rows: int,
        source_fingerprint: str,
    ) -> dict[str, Any]:
        resp = self._request(
            "POST",
            "/api/warehouse/bulk-import/complete",
            headers=self._headers({"Content-Type": "application/json"}),
            json={
                "snapshot_id": snapshot_id,
                "source_db": source_db,
                "expected_batches": expected_batches,
                "expected_total_rows": expected_total_rows,
                "source_fingerprint": source_fingerprint,
            },
        )
        return self._decode_json(resp)

    def readback_page(
        self, snapshot_id: str, source_db: str, *, after_seq: int, limit: int
    ) -> tuple[bytes, dict[str, Any]]:
        resp = self._request(
            "GET",
            "/api/warehouse/bulk-import/readback",
            params={
                "snapshot_id": snapshot_id,
                "source_db": source_db,
                "after_seq": str(after_seq),
                "limit": str(limit),
                "format": "parquet",
            },
            headers=self._headers(),
        )
        if not resp.ok:
            raise TransportError(f"HTTP {resp.status_code}: {resp.text}")
        meta = {
            "has_more": resp.headers.get("X-StockDB-Has-More", "false").lower() == "true",
            "next_after_seq": int(resp.headers.get("X-StockDB-Next-After-Seq", str(after_seq))),
            "returned_rows": int(resp.headers.get("X-StockDB-Returned-Rows", "0")),
        }
        return resp.content, meta

    def verify_import(
        self,
        snapshot_id: str,
        source_db: str,
        *,
        expected_total_rows: int,
        source_fingerprint: str,
    ) -> dict[str, Any]:
        resp = self._request(
            "POST",
            "/api/warehouse/bulk-import/verify",
            headers=self._headers({"Content-Type": "application/json"}),
            json={
                "snapshot_id": snapshot_id,
                "source_db": source_db,
                "expected_total_rows": expected_total_rows,
                "source_fingerprint": source_fingerprint,
            },
        )
        return self._decode_json(resp)

    def upload_daily_backfill_batch(
        self,
        meta: dict[str, Any],
        parquet_bytes: bytes,
        *,
        overwrite_existing: bool = False,
    ) -> dict[str, Any]:
        headers = self._headers(
            {
                "Content-Type": "application/octet-stream",
                "X-StockDB-Snapshot-Id": str(meta["snapshot_id"]),
                "X-StockDB-Source-Db": str(meta["source_db"]),
                "X-StockDB-Batch-Id": str(meta["batch_id"]),
                "X-StockDB-Parquet-Sha256": str(meta["parquet_sha256"]),
                "X-StockDB-Row-Count": str(meta["row_count"]),
                "X-StockDB-Rows-Digest": str(meta["rows_digest_sha256"]),
                "X-StockDB-Overwrite-Existing": "true" if overwrite_existing else "false",
            }
        )
        resp = self._request(
            "POST",
            "/api/warehouse/daily-backfill/batch",
            params={
                "snapshot_id": meta["snapshot_id"],
                "source_db": meta["source_db"],
                "batch_id": meta["batch_id"],
            },
            headers=headers,
            data=parquet_bytes,
        )
        return self._decode_json(resp)

    def readback_daily(self, dates: list[str]) -> tuple[bytes, dict[str, Any]]:
        resp = self._request(
            "GET",
            "/api/warehouse/daily-backfill/readback",
            params={"dates": ",".join(dates)},
            headers=self._headers(),
        )
        if not resp.ok:
            raise TransportError(f"HTTP {resp.status_code}: {resp.text}")
        meta = {
            "returned_rows": int(resp.headers.get("X-StockDB-Returned-Rows", "0")),
            "parquet_sha256": resp.headers.get("X-StockDB-Parquet-Sha256", ""),
        }
        return resp.content, meta


class LocalBulkImportTransport:
    """直连本地 warehouse_root 的传输实现（用于单机/集成测试，不经 HTTP）。"""

    def __init__(
        self,
        warehouse_root: Path,
        refresh_views_cb=None,
        *,
        daily_code_predicate: Callable[[str], bool] | None = None,
        is_trading_day_fn=None,
    ) -> None:
        self.warehouse_root = Path(warehouse_root)
        self.refresh_views_cb = refresh_views_cb
        self.daily_code_predicate = daily_code_predicate
        self.is_trading_day_fn = is_trading_day_fn

    def get_status(self, snapshot_id: str, source_db: str) -> dict[str, Any]:
        return bulk_import.get_bulk_import_status(
            self.warehouse_root, snapshot_id=snapshot_id, source_db=source_db
        )

    def upload_batch(self, meta: dict[str, Any], parquet_bytes: bytes) -> dict[str, Any]:
        return bulk_import.ingest_parquet_batch(
            self.warehouse_root,
            parquet_bytes,
            snapshot_id=meta["snapshot_id"],
            source_db=meta["source_db"],
            batch_id=meta["batch_id"],
            batch_index=int(meta["batch_index"]),
            parquet_sha256=meta["parquet_sha256"],
            expected_row_count=int(meta["row_count"]),
            expected_seq_start=int(meta["seq_start"]),
            expected_seq_end=int(meta["seq_end"]),
            expected_rows_digest=meta["rows_digest_sha256"],
            source_fingerprint=meta.get("source_fingerprint"),
            refresh_views_cb=self.refresh_views_cb,
        )

    def complete_import(
        self,
        snapshot_id: str,
        source_db: str,
        *,
        expected_batches: int,
        expected_total_rows: int,
        source_fingerprint: str,
    ) -> dict[str, Any]:
        return bulk_import.complete_bulk_import(
            self.warehouse_root,
            snapshot_id=snapshot_id,
            source_db=source_db,
            expected_batches=expected_batches,
            expected_total_rows=expected_total_rows,
            source_fingerprint=source_fingerprint,
            refresh_views_cb=self.refresh_views_cb,
        )

    def readback_page(
        self, snapshot_id: str, source_db: str, *, after_seq: int, limit: int
    ) -> tuple[bytes, dict[str, Any]]:
        return bulk_import.readback_bulk_import(
            self.warehouse_root,
            snapshot_id=snapshot_id,
            source_db=source_db,
            after_seq=after_seq,
            limit=limit,
            output_format="parquet",
        )

    def verify_import(
        self,
        snapshot_id: str,
        source_db: str,
        *,
        expected_total_rows: int,
        source_fingerprint: str,
    ) -> dict[str, Any]:
        return bulk_import.verify_bulk_import(
            self.warehouse_root,
            snapshot_id=snapshot_id,
            source_db=source_db,
            expected_total_rows=expected_total_rows,
            source_fingerprint=source_fingerprint,
        )

    def upload_daily_backfill_batch(
        self,
        meta: dict[str, Any],
        parquet_bytes: bytes,
        *,
        overwrite_existing: bool = False,
    ) -> dict[str, Any]:
        return daily_backfill.ingest_daily_backfill_parquet_batch(
            self.warehouse_root,
            parquet_bytes,
            snapshot_id=meta["snapshot_id"],
            source_db=meta["source_db"],
            batch_id=meta["batch_id"],
            overwrite_existing=overwrite_existing,
            parquet_sha256=meta["parquet_sha256"],
            expected_row_count=int(meta["row_count"]),
            expected_rows_digest=meta["rows_digest_sha256"],
            code_predicate=self.daily_code_predicate,
            is_trading_day_fn=self.is_trading_day_fn,
            refresh_views_cb=self.refresh_views_cb,
        )

    def readback_daily(self, dates: list[str]) -> tuple[bytes, dict[str, Any]]:
        return daily_backfill.readback_daily_parquet(
            self.warehouse_root,
            dates=dates,
        )


def _save_json_atomic(path: Path, data: dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_name(f".{path.name}.{os.getpid()}.tmp")
    tmp.write_text(json.dumps(data, ensure_ascii=False, indent=2), encoding="utf-8")
    os.replace(tmp, path)


def _load_json(path: Path) -> dict[str, Any] | None:
    if not path.is_file():
        return None
    return json.loads(path.read_text(encoding="utf-8"))


def verify_snapshot_from_nas(
    snapshot_dir: str | Path,
    *,
    snapshot_id: str,
    source_db: str,
    transport: BulkImportTransport,
    page_size: int = 50000,
    verify_checksums: bool = True,
    key_filter: leveldb_snapshot.KeyScopeFilter | None = None,
) -> dict[str, Any]:
    """通过 NAS DuckDB 读回接口流式拉取全量条目，与本地 LevelDB 冻结快照逐条比对。"""
    snap = leveldb_snapshot.open_leveldb_snapshot(snapshot_dir, verify_checksums=verify_checksums)

    def _fetch(after_seq: int) -> tuple[bytes, dict[str, Any]]:
        return transport.readback_page(snapshot_id, source_db, after_seq=after_seq, limit=page_size)

    res = bulk_import.compare_sorted_kv_streams(
        snap.iter_entries(key_filter=key_filter),
        bulk_import.iter_duckdb_parquet_stream(_fetch),
    )
    res["snapshot_id"] = snapshot_id
    res["source_db"] = source_db
    res["source_fingerprint"] = snap.fingerprint()
    if key_filter is not None and not key_filter.is_unfiltered():
        res["scope_filter"] = key_filter.to_dict()
    return res


def _effective_fingerprint(
    raw_fp: str, key_filter: leveldb_snapshot.KeyScopeFilter | None
) -> str:
    if key_filter is None or key_filter.is_unfiltered():
        return raw_fp
    return hashlib.sha256(f"{raw_fp}|{key_filter.scope_digest()}".encode("ascii")).hexdigest()


def sync_snapshot_to_nas(
    snapshot_dir: str | Path,
    *,
    snapshot_id: str,
    source_db: str | None = None,
    transport: BulkImportTransport,
    checkpoint_path: str | Path,
    staging_dir: str | Path | None = None,
    batch_size: int = 50000,
    max_batch_bytes: int = 16 * 1024 * 1024,
    readback_page_size: int = 50000,
    verify_checksums: bool = True,
    verify_readback: bool = True,
    keep_staging: bool = False,
    key_filter: leveldb_snapshot.KeyScopeFilter | None = None,
) -> dict[str, Any]:
    """全量或按范围读取一个 LevelDB 快照目录、分批导出 Parquet 并断点续传至 NAS，最后从 DuckDB 读回核对。"""
    snap_path = Path(snapshot_dir)
    src_name = source_db or snap_path.name
    snap = leveldb_snapshot.open_leveldb_snapshot(snap_path, verify_checksums=verify_checksums)
    fp = _effective_fingerprint(snap.fingerprint(), key_filter)

    ckpt_path = Path(checkpoint_path)
    state = _load_json(ckpt_path)
    if state is None:
        state = {
            "snapshot_id": snapshot_id,
            "source_db": src_name,
            "source_dir": str(snap_path),
            "source_fingerprint": fp,
            "scope_filter": key_filter.to_dict() if key_filter is not None else None,
            "status": "uploading",
            "next_batch_index": 0,
            "next_seq": 0,
            "last_confirmed_key_b64": None,
            "committed_batches": [],
            "pending_batch": None,
        }
    else:
        if state.get("snapshot_id") != snapshot_id or state.get("source_db") != src_name:
            raise bulk_import.BulkImportConflictError(
                f"本地 checkpoint 与当前任务不匹配: {ckpt_path}"
            )
        if state.get("source_fingerprint") != fp:
            raise bulk_import.BulkImportConflictError(
                f"源快照指纹已变化，禁止跨快照混传: old={state.get('source_fingerprint')}, now={fp}"
            )

    # 先对账 NAS 端已确认进度（自动恢复“NAS 已落盘但 ACK 丢失”的 pending_batch）
    remote_status = transport.get_status(snapshot_id, src_name)
    remote_job = remote_status.get("job")
    if remote_job and remote_job.get("source_fingerprint") and remote_job["source_fingerprint"] != fp:
        raise bulk_import.BulkImportConflictError(
            f"NAS 端 source_fingerprint 不一致: remote={remote_job['source_fingerprint']}, local={fp}"
        )
    remote_batches = {b["batch_id"]: b for b in remote_status.get("batches", [])}

    pending = state.get("pending_batch")
    if pending is not None:
        rb = remote_batches.get(pending["batch_id"])
        if rb is not None and (
            rb["parquet_sha256"] == pending["parquet_sha256"]
            and rb["rows_digest_sha256"] == pending["rows_digest_sha256"]
        ):
            state["last_confirmed_key_b64"] = pending["last_key_b64"]
            state["next_batch_index"] = int(pending["batch_index"]) + 1
            state["next_seq"] = int(pending["seq_end"]) + 1
            state["committed_batches"].append(
                {
                    "batch_id": pending["batch_id"],
                    "batch_index": int(pending["batch_index"]),
                    "seq_start": int(pending["seq_start"]),
                    "seq_end": int(pending["seq_end"]),
                    "row_count": int(pending["row_count"]),
                    "parquet_sha256": pending["parquet_sha256"],
                    "rows_digest_sha256": pending["rows_digest_sha256"],
                }
            )
            state["pending_batch"] = None
            _save_json_atomic(ckpt_path, state)
        elif rb is not None:
            raise bulk_import.BulkImportConflictError(
                f"pending_batch={pending['batch_id']} 与 NAS 已提交摘要冲突"
            )
        else:
            state["pending_batch"] = None
            _save_json_atomic(ckpt_path, state)

    if len(state["committed_batches"]) > len(remote_batches):
        raise bulk_import.BulkImportConflictError(
            f"本地确认批次数 ({len(state['committed_batches'])}) 超出 NAS 已存 ({len(remote_batches)})"
        )

    start_after_key = (
        base64.b64decode(state["last_confirmed_key_b64"])
        if state.get("last_confirmed_key_b64") is not None
        else None
    )

    stage_root = Path(staging_dir) if staging_dir else (ckpt_path.parent / "staging" / snapshot_id / src_name)
    stage_root.mkdir(parents=True, exist_ok=True)

    uploaded_this_run = 0
    duplicate_skipped_this_run = 0

    remaining_rows = (
        max(0, int(key_filter.max_rows) - int(state["next_seq"]))
        if (key_filter is not None and key_filter.max_rows is not None)
        else None
    )

    for batch_idx, seq_start, seq_end, items in snap.iter_batches(
        batch_size=batch_size,
        max_batch_bytes=max_batch_bytes,
        start_after_key=start_after_key,
        start_seq=int(state["next_seq"]),
        start_batch_index=int(state["next_batch_index"]),
        key_filter=key_filter,
        max_rows_override=remaining_rows,
    ):
        batch_id = f"b{batch_idx:06d}"
        parquet_path = stage_root / f"batch_{batch_id}.parquet"
        meta = bulk_import.write_leveldb_batch_parquet(
            parquet_path,
            snapshot_id=snapshot_id,
            source_db=src_name,
            batch_id=batch_id,
            start_seq=seq_start,
            items=items,
        )
        meta["batch_index"] = batch_idx
        meta["source_fingerprint"] = fp
        last_key_b64 = base64.b64encode(meta["last_key"]).decode("ascii")

        # Write-ahead：先记录 pending_batch，再上传；仅在收到 NAS 确认后才推进游标
        state["pending_batch"] = {
            "batch_id": batch_id,
            "batch_index": batch_idx,
            "seq_start": meta["seq_start"],
            "seq_end": meta["seq_end"],
            "row_count": meta["row_count"],
            "parquet_sha256": meta["parquet_sha256"],
            "rows_digest_sha256": meta["rows_digest_sha256"],
            "last_key_b64": last_key_b64,
        }
        _save_json_atomic(ckpt_path, state)

        parquet_bytes = parquet_path.read_bytes()
        ack = transport.upload_batch(meta, parquet_bytes)
        if ack.get("parquet_sha256") != meta["parquet_sha256"] or ack.get("rows_digest_sha256") != meta["rows_digest_sha256"]:
            raise TransportError(f"NAS 返回确认摘要不一致: batch={batch_id}, ack={ack}")

        if ack.get("status") == "duplicate_skipped":
            duplicate_skipped_this_run += 1
        else:
            uploaded_this_run += 1

        state["last_confirmed_key_b64"] = last_key_b64
        state["next_batch_index"] = batch_idx + 1
        state["next_seq"] = meta["seq_end"] + 1
        state["committed_batches"].append(
            {
                "batch_id": batch_id,
                "batch_index": batch_idx,
                "seq_start": meta["seq_start"],
                "seq_end": meta["seq_end"],
                "row_count": meta["row_count"],
                "parquet_sha256": meta["parquet_sha256"],
                "rows_digest_sha256": meta["rows_digest_sha256"],
            }
        )
        state["pending_batch"] = None
        _save_json_atomic(ckpt_path, state)

        if not keep_staging:
            parquet_path.unlink(missing_ok=True)

    complete_res = transport.complete_import(
        snapshot_id,
        src_name,
        expected_batches=int(state["next_batch_index"]),
        expected_total_rows=int(state["next_seq"]),
        source_fingerprint=fp,
    )
    state["status"] = "completed"
    _save_json_atomic(ckpt_path, state)

    nas_verify = transport.verify_import(
        snapshot_id,
        src_name,
        expected_total_rows=int(state["next_seq"]),
        source_fingerprint=fp,
    )

    readback_verify: dict[str, Any] | None = None
    passed = bool(nas_verify.get("passed"))
    if verify_readback:
        readback_verify = verify_snapshot_from_nas(
            snap_path,
            snapshot_id=snapshot_id,
            source_db=src_name,
            transport=transport,
            page_size=readback_page_size,
            verify_checksums=verify_checksums,
            key_filter=key_filter,
        )
        passed = passed and bool(readback_verify.get("passed"))

    if passed:
        state["status"] = "verified"
    state["verify_result"] = {
        "nas_verify": nas_verify,
        "readback_verify": readback_verify,
        "passed": passed,
    }
    _save_json_atomic(ckpt_path, state)

    return {
        "ok": True,
        "passed": passed,
        "snapshot_id": snapshot_id,
        "source_db": src_name,
        "source_fingerprint": fp,
        "total_batches": int(state["next_batch_index"]),
        "total_rows": int(state["next_seq"]),
        "uploaded_this_run": uploaded_this_run,
        "duplicate_skipped_this_run": duplicate_skipped_this_run,
        "complete": complete_res,
        "nas_verify": nas_verify,
        "readback_verify": readback_verify,
        "checkpoint_path": str(ckpt_path),
    }


def _with_exclude_prefix(
    key_filter: leveldb_snapshot.KeyScopeFilter | None,
    prefix: bytes,
) -> leveldb_snapshot.KeyScopeFilter:
    if key_filter is None:
        return leveldb_snapshot.KeyScopeFilter(exclude_prefixes=(prefix,))
    excl = tuple(dict.fromkeys([*key_filter.exclude_prefixes, prefix]))
    return leveldb_snapshot.KeyScopeFilter(
        since_date=key_filter.since_date,
        until_date=key_filter.until_date,
        include_prefixes=key_filter.include_prefixes,
        exclude_prefixes=excl,
        include_undated=key_filter.include_undated,
        max_rows=key_filter.max_rows,
    )


def sync_kdaily_standard_to_nas(
    snapshot_dirs: Sequence[Path],
    *,
    snapshot_id: str,
    transport: BulkImportTransport,
    staging_dir: str | Path,
    checkpoint_path: str | Path,
    key_filter: leveldb_snapshot.KeyScopeFilter | None = None,
    code_predicate: Callable[[str], bool] | None = None,
    overwrite_existing: bool = False,
    verify_checksums: bool = True,
    verify_readback: bool = True,
) -> dict[str, Any]:
    """按完整自然月扫描并上传 `k日k:` 至 NAS 标准 `facts/daily`（仅保留目标股票，且逐字段读回核对）。"""
    ckpt_path = Path(checkpoint_path)
    state = _load_json(ckpt_path) or {
        "snapshot_id": snapshot_id,
        "completed_months": {},
    }
    completed: dict[str, Any] = state.setdefault("completed_months", {})

    stage_root = Path(staging_dir) / snapshot_id / "_daily_backfill"
    month_batches, scan_stats = daily_backfill.iter_leveldb_kdaily_month_batches(
        snapshot_dirs,
        staging_dir=stage_root,
        snapshot_id=snapshot_id,
        key_filter=key_filter,
        code_predicate=code_predicate,
        verify_checksums=verify_checksums,
    )

    written_dates_all: list[str] = []
    skipped_dates_all: list[str] = []
    written_rows_total = 0
    verified_rows_total = 0
    months_processed: list[str] = []

    for ym, pq_path, meta, month_rows in month_batches:
        try:
            done_rec = completed.get(ym)
            if done_rec and done_rec.get("parquet_sha256") == meta["parquet_sha256"] and not overwrite_existing:
                continue

            pq_bytes = pq_path.read_bytes()
            ack = transport.upload_daily_backfill_batch(
                meta,
                pq_bytes,
                overwrite_existing=overwrite_existing,
            )
            w_dates = list(ack.get("written_dates") or [])
            s_dates = list(ack.get("skipped_dates") or [])
            written_dates_all.extend(w_dates)
            skipped_dates_all.extend(s_dates)
            written_rows_total += int(ack.get("written_rows") or 0)

            if verify_readback and w_dates:
                rb_tuples: list[tuple[Any, ...]] = []
                for idx in range(0, len(w_dates), 30):
                    chunk_dates = w_dates[idx : idx + 30]
                    rb_bytes, _ = transport.readback_daily(chunk_dates)
                    rb_tuples.extend(
                        daily_backfill.parse_daily_parquet_bytes(
                            rb_bytes, staging_dir=stage_root / "verify"
                        )
                    )
                w_set = set(w_dates)
                expected_rows = [r for r in month_rows if str(r.get("date")) in w_set]
                cmp_res = daily_backfill.compare_daily_rows(expected_rows, rb_tuples)
                if not cmp_res["passed"]:
                    raise TransportError(f"标准日线回填 readback 核对失败 (ym={ym}): {cmp_res}")
                verified_rows_total += int(cmp_res["actual_rows"])

            completed[ym] = {
                "parquet_sha256": meta["parquet_sha256"],
                "written_dates": w_dates,
                "skipped_dates": s_dates,
                "written_rows": int(ack.get("written_rows") or 0),
            }
            _save_json_atomic(ckpt_path, state)
            months_processed.append(ym)
        finally:
            pq_path.unlink(missing_ok=True)

    return {
        "ok": True,
        "passed": True,
        "snapshot_id": snapshot_id,
        "scanned_kdaily_rows": scan_stats["scanned_kdaily_rows"],
        "filtered_non_stock_rows": scan_stats["filtered_non_stock_rows"],
        "month_batches": scan_stats["month_batches"],
        "months_processed": months_processed,
        "written_dates": sorted(set(written_dates_all)),
        "skipped_dates": sorted(set(skipped_dates_all)),
        "written_rows": written_rows_total,
        "verified_rows": verified_rows_total,
    }


def sync_all_snapshots_to_nas(
    snapshot_root: str | Path,
    *,
    snapshot_id: str,
    transport: BulkImportTransport,
    checkpoint_dir: str | Path,
    db_names: list[str] | None = None,
    batch_size: int = 50000,
    max_batch_bytes: int = 16 * 1024 * 1024,
    readback_page_size: int = 50000,
    verify_checksums: bool = True,
    verify_readback: bool = True,
    key_filter: leveldb_snapshot.KeyScopeFilter | None = None,
    daily_mode: str = "off",
    daily_code_predicate: Callable[[str], bool] | None = None,
    overwrite_daily_existing: bool = False,
) -> dict[str, Any]:
    """遍历快照根目录下全部或指定 LevelDB 库并逐库完成同步与 DuckDB 读回验收。

    `daily_mode`:
      - `"off"`: 保持原有原始库全量导入（含 `k日k`）；
      - `"exclude"`: 跳过 `k日k:`（借助 SST 区间整文件跳读）；
      - `"sync-and-exclude"`: 先跳过 `k日k:` 同步原库（确保 `k复权`/`k分钟k` 等入仓），
        再将 `k日k:` 过滤股票后合并回填进唯一标准 `facts/daily` 并逐字段读回核对。
    """
    if daily_mode not in ("off", "exclude", "sync-and-exclude"):
        raise ValueError(f"非法 daily_mode: {daily_mode!r}")

    db_dirs = leveldb_snapshot.list_snapshot_dirs(snapshot_root)
    if db_names:
        wanted = set(db_names)
        db_dirs = [d for d in db_dirs if d.name in wanted]
        found = {d.name for d in db_dirs}
        missing = sorted(wanted - found)
        if missing:
            raise leveldb_snapshot.LevelDBFormatError(f"指定的源库不存在: {missing}")

    ckpt_root = Path(checkpoint_dir)
    ckpt_root.mkdir(parents=True, exist_ok=True)

    raw_key_filter = (
        _with_exclude_prefix(key_filter, b"k\xe6\x97\xa5k:")
        if daily_mode in ("exclude", "sync-and-exclude")
        else key_filter
    )

    per_db: list[dict[str, Any]] = []
    all_passed = True
    for db_dir in db_dirs:
        res = sync_snapshot_to_nas(
            db_dir,
            snapshot_id=snapshot_id,
            source_db=db_dir.name,
            transport=transport,
            checkpoint_path=ckpt_root / f"{snapshot_id}__{db_dir.name}.checkpoint.json",
            batch_size=batch_size,
            max_batch_bytes=max_batch_bytes,
            readback_page_size=readback_page_size,
            verify_checksums=verify_checksums,
            verify_readback=verify_readback,
            key_filter=raw_key_filter,
        )
        per_db.append(res)
        if not res["passed"]:
            all_passed = False

    daily_backfill_report: dict[str, Any] | None = None
    if daily_mode == "sync-and-exclude" and db_dirs:
        daily_backfill_report = sync_kdaily_standard_to_nas(
            db_dirs,
            snapshot_id=snapshot_id,
            transport=transport,
            staging_dir=ckpt_root / "staging",
            checkpoint_path=ckpt_root / f"{snapshot_id}__daily_backfill.checkpoint.json",
            key_filter=key_filter,
            code_predicate=daily_code_predicate,
            overwrite_existing=overwrite_daily_existing,
            verify_checksums=verify_checksums,
            verify_readback=verify_readback,
        )
        if not daily_backfill_report.get("passed"):
            all_passed = False

    return {
        "ok": True,
        "passed": all_passed,
        "snapshot_id": snapshot_id,
        "daily_mode": daily_mode,
        "databases": [d.name for d in db_dirs],
        "results": per_db,
        "daily_backfill": daily_backfill_report,
    }
