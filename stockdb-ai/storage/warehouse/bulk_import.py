"""storage.warehouse.bulk_import — NAS 侧 Parquet + DuckDB 批量导入与只读核对引擎。

核心职责：
1. 生成无损 Parquet 批次（`raw_key` / `raw_value` 按 `BLOB` 原样保存，附加辅助可读列）。
2. NAS 本地防篡改与一致性校验：SHA-256、14 列类型与顺序、行数、`seq_no` 连续性、
   `rows_digest_sha256`、批次内严格键序无重复、跨批次不重叠。
3. 幂等与冲突隔离：同 `(snapshot_id, source_db, batch_id)` 同内容重传返回 `duplicate_skipped`；
   同 `batch_id` 内容不一致或 `source_fingerprint` 冲突返回 `BulkImportConflictError` (409)。
4. 原子落盘：临时 `.staging/...parquet.tmp` 校验通过后 `os.replace` 至不可变 `facts/leveldb_raw/`，
   并登记进 `warehouse.duckdb` 的 `bulk_import_jobs` / `bulk_import_batches`。
5. 分页读回与逐库全量比对（缺键、多键、错值、重键全部为 0 才通过）。
"""
from __future__ import annotations

import base64
import hashlib
import json
import os
import threading
import time
import uuid
from collections.abc import Callable, Iterable
from pathlib import Path
from typing import Any

import config
from storage.providers import msgpack_lite

from . import catalog, inventory, layout

_BULK_LOCK = threading.RLock()

LEVELDB_RAW_COLUMNS: tuple[tuple[str, str], ...] = (
    ("snapshot_id", "VARCHAR"),
    ("source_db", "VARCHAR"),
    ("batch_id", "VARCHAR"),
    ("seq_no", "BIGINT"),
    ("raw_key", "BLOB"),
    ("raw_value", "BLOB"),
    ("key_sha256", "VARCHAR"),
    ("value_sha256", "VARCHAR"),
    ("key_len", "INTEGER"),
    ("value_len", "INTEGER"),
    ("key_utf8", "VARCHAR"),
    ("key_namespace", "VARCHAR"),
    ("value_kind", "VARCHAR"),
    ("value_text", "VARCHAR"),
)

_EMPTY_LEVELDB_RAW_SQL = (
    "SELECT "
    "CAST(NULL AS VARCHAR) AS snapshot_id, "
    "CAST(NULL AS VARCHAR) AS source_db, "
    "CAST(NULL AS VARCHAR) AS batch_id, "
    "CAST(NULL AS BIGINT) AS seq_no, "
    "CAST(NULL AS BLOB) AS raw_key, "
    "CAST(NULL AS BLOB) AS raw_value, "
    "CAST(NULL AS VARCHAR) AS key_sha256, "
    "CAST(NULL AS VARCHAR) AS value_sha256, "
    "CAST(NULL AS INTEGER) AS key_len, "
    "CAST(NULL AS INTEGER) AS value_len, "
    "CAST(NULL AS VARCHAR) AS key_utf8, "
    "CAST(NULL AS VARCHAR) AS key_namespace, "
    "CAST(NULL AS VARCHAR) AS value_kind, "
    "CAST(NULL AS VARCHAR) AS value_text "
    "WHERE 1=0"
)

_KNOWN_PREFIXES = (
    "日k",
    "分钟k",
    "复权",
    "股票代码",
    "周k",
    "月k",
    "板块",
    "行业",
    "概念",
    "hk日k",
    "打板指标",
    "竞价快照",
    "打板序列",
    "清单",
    "自定义",
)


class BulkImportError(ValueError):
    """客户端参数、校验或文件格式错误（映射 HTTP 400）。"""


class BulkImportConflictError(RuntimeError):
    """幂等冲突、快照指纹冲突或状态冲突（映射 HTTP 409）。"""


def _now_iso() -> str:
    return time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())


def _is_printable_text(s: str) -> bool:
    for ch in s:
        o = ord(ch)
        if o < 32 and ch not in ("\t", "\n", "\r"):
            return False
    return True


def classify_kv_entry(raw_key: bytes, raw_value: bytes) -> tuple[str | None, str, str, str | None]:
    """生成辅助索引元数据；原始字节由 `raw_key`/`raw_value` 100% 原样承担。"""
    key_utf8: str | None = None
    key_namespace = "binary"
    if b"\x00" not in raw_key:
        try:
            decoded_key = raw_key.decode("utf-8")
        except UnicodeDecodeError:
            decoded_key = None
        if decoded_key is not None and _is_printable_text(decoded_key):
            key_utf8 = decoded_key
            matched_ns = None
            for pfx in _KNOWN_PREFIXES:
                if decoded_key.startswith(pfx):
                    matched_ns = pfx
                    break
            if matched_ns is not None:
                key_namespace = matched_ns
            else:
                sep_idx = -1
                for sep in (":", "|", "/", "_"):
                    idx = decoded_key.find(sep)
                    if idx > 0 and (sep_idx < 0 or idx < sep_idx):
                        sep_idx = idx
                if 0 < sep_idx <= 32:
                    key_namespace = decoded_key[:sep_idx]
                else:
                    key_namespace = "raw_utf8"
        elif decoded_key is not None:
            # 兼容含 `\x1f` 二级索引分隔符的 SSDB 键（如 `kidx:hk日k:k2:...`）
            type_key, _, _ = inventory.resolve_leveldb_type(raw_key)
            if type_key != "binary":
                key_namespace = type_key

    if raw_value == b"":
        return key_utf8, key_namespace, "empty", ""

    if b"\x00" not in raw_value:
        try:
            decoded_val = raw_value.decode("utf-8")
        except UnicodeDecodeError:
            decoded_val = None
        if decoded_val is not None and _is_printable_text(decoded_val):
            stripped = decoded_val.lstrip()
            if stripped.startswith(("{", "[")):
                try:
                    json.loads(stripped)
                    return key_utf8, key_namespace, "json", decoded_val[:4096]
                except ValueError:
                    pass
            return key_utf8, key_namespace, "utf8", decoded_val[:4096]

    b0 = raw_value[0]
    if 0x80 <= b0 <= 0x9F or b0 in (0xDC, 0xDD, 0xDE, 0xDF):
        try:
            obj = msgpack_lite.unpack(raw_value)
            preview = json.dumps(obj, ensure_ascii=False, default=str)[:4096]
            return key_utf8, key_namespace, "msgpack", preview
        except Exception:
            pass

    return key_utf8, key_namespace, "binary", None


def compute_rows_digest(rows_meta: Iterable[tuple[int, str, str]]) -> str:
    """基于 `(seq_no, key_sha256, value_sha256)` 序列计算规范摘要。"""
    h = hashlib.sha256()
    for seq_no, k_sha, v_sha in rows_meta:
        h.update(f"{seq_no}:{k_sha}:{v_sha}\n".encode("ascii"))
    return h.hexdigest()


def ensure_leveldb_views(con, root: Path) -> None:
    """在当前 DuckDB 连接上确保 bulk_import 元表与 `v_leveldb_*` 视图存在。"""
    con.execute(catalog._BULK_IMPORT_JOBS_SCHEMA)
    con.execute(catalog._BULK_IMPORT_BATCHES_SCHEMA)
    raw_dir = layout.leveldb_raw_dir(root)
    has_files = raw_dir.is_dir() and any(raw_dir.rglob("*.parquet"))
    if has_files:
        glob_pattern = str(raw_dir / "*" / "*" / "*.parquet").replace("\\", "/")
        con.execute(
            f"CREATE OR REPLACE VIEW v_leveldb_raw AS "
            f"SELECT snapshot_id, source_db, batch_id, seq_no, raw_key, raw_value, "
            f"key_sha256, value_sha256, key_len, value_len, key_utf8, key_namespace, "
            f"value_kind, value_text "
            f"FROM read_parquet('{glob_pattern}')"
        )
    else:
        con.execute(f"CREATE OR REPLACE VIEW v_leveldb_raw AS {_EMPTY_LEVELDB_RAW_SQL}")

    con.execute(
        "CREATE OR REPLACE VIEW v_leveldb_snapshots AS "
        "SELECT snapshot_id, source_db, status, source_fingerprint, expected_batches, "
        "committed_batches, committed_rows, committed_bytes, last_seq_end, "
        "created_at, updated_at, completed_at, verified_at, verify_summary "
        "FROM bulk_import_jobs"
    )
    con.execute(
        "CREATE OR REPLACE VIEW v_leveldb_latest AS "
        "WITH ranked AS ("
        "    SELECT snapshot_id, source_db, completed_at, updated_at, "
        "           ROW_NUMBER() OVER ("
        "               PARTITION BY source_db "
        "               ORDER BY COALESCE(verified_at, completed_at, updated_at) DESC, snapshot_id DESC"
        "           ) AS rn "
        "    FROM bulk_import_jobs "
        "    WHERE status IN ('completed', 'verified')"
        ") "
        "SELECT r.snapshot_id, r.source_db, r.batch_id, r.seq_no, r.raw_key, r.raw_value, "
        "       r.key_sha256, r.value_sha256, r.key_len, r.value_len, r.key_utf8, "
        "       r.key_namespace, r.value_kind, r.value_text "
        "FROM v_leveldb_raw r "
        "JOIN ranked k ON r.snapshot_id = k.snapshot_id AND r.source_db = k.source_db "
        "WHERE k.rn = 1"
    )


def write_leveldb_batch_parquet(
    out_path: Path,
    *,
    snapshot_id: str,
    source_db: str,
    batch_id: str,
    start_seq: int,
    items: list[tuple[bytes, bytes]],
) -> dict[str, Any]:
    """将一批 `(raw_key, raw_value)` 原子写成符合契约的 Zstd Parquet 文件并返回校验元数据。"""
    import duckdb
    import pandas as pd

    layout.validate_identifier(snapshot_id, "snapshot_id")
    layout.validate_identifier(source_db, "source_db")
    layout.validate_identifier(batch_id, "batch_id")
    if start_seq < 0:
        raise BulkImportError(f"start_seq 不能为负: {start_seq}")
    if not items:
        raise BulkImportError("不能写入空 Parquet 批次（空库应直接调用 complete expected_batches=0）")

    seq_nos: list[int] = []
    raw_keys: list[bytes] = []
    raw_values: list[bytes] = []
    key_shas: list[str] = []
    val_shas: list[str] = []
    key_lens: list[int] = []
    val_lens: list[int] = []
    key_utf8s: list[str | None] = []
    key_namespaces: list[str] = []
    val_kinds: list[str] = []
    val_texts: list[str | None] = []
    digest_rows: list[tuple[int, str, str]] = []

    prev_key: bytes | None = None
    for idx, (rk, rv) in enumerate(items):
        if not isinstance(rk, (bytes, bytearray)) or not isinstance(rv, (bytes, bytearray)):
            raise BulkImportError("raw_key 和 raw_value 必须为 bytes")
        kb = bytes(rk)
        vb = bytes(rv)
        if prev_key is not None and kb <= prev_key:
            raise BulkImportError("批次内部键未按严格字典序递增或存在重复键")
        prev_key = kb
        seq_no = start_seq + idx
        k_sha = hashlib.sha256(kb).hexdigest()
        v_sha = hashlib.sha256(vb).hexdigest()
        k_u8, k_ns, v_kind, v_txt = classify_kv_entry(kb, vb)
        seq_nos.append(seq_no)
        raw_keys.append(kb)
        raw_values.append(vb)
        key_shas.append(k_sha)
        val_shas.append(v_sha)
        key_lens.append(len(kb))
        val_lens.append(len(vb))
        key_utf8s.append(k_u8)
        key_namespaces.append(k_ns)
        val_kinds.append(v_kind)
        val_texts.append(v_txt)
        digest_rows.append((seq_no, k_sha, v_sha))

    n = len(items)
    df = pd.DataFrame(
        {
            "snapshot_id": pd.Series([snapshot_id] * n, dtype="object"),
            "source_db": pd.Series([source_db] * n, dtype="object"),
            "batch_id": pd.Series([batch_id] * n, dtype="object"),
            "seq_no": pd.Series(seq_nos, dtype="int64"),
            "raw_key": pd.Series(raw_keys, dtype="object"),
            "raw_value": pd.Series(raw_values, dtype="object"),
            "key_sha256": pd.Series(key_shas, dtype="object"),
            "value_sha256": pd.Series(val_shas, dtype="object"),
            "key_len": pd.Series(key_lens, dtype="int32"),
            "value_len": pd.Series(val_lens, dtype="int32"),
            "key_utf8": pd.Series(key_utf8s, dtype="object"),
            "key_namespace": pd.Series(key_namespaces, dtype="object"),
            "value_kind": pd.Series(val_kinds, dtype="object"),
            "value_text": pd.Series(val_texts, dtype="object"),
        }
    )

    out_path = Path(out_path)
    out_path.parent.mkdir(parents=True, exist_ok=True)
    tmp_path = out_path.with_name(f".{out_path.name}.{uuid.uuid4().hex[:8]}.tmp")
    con = duckdb.connect()
    try:
        con.register("_bulk_batch_df", df)
        sql_path = str(tmp_path).replace("\\", "/").replace("'", "''")
        con.execute(
            f"COPY ("
            f"  SELECT "
            f"    CAST(snapshot_id AS VARCHAR) AS snapshot_id, "
            f"    CAST(source_db AS VARCHAR) AS source_db, "
            f"    CAST(batch_id AS VARCHAR) AS batch_id, "
            f"    CAST(seq_no AS BIGINT) AS seq_no, "
            f"    CAST(raw_key AS BLOB) AS raw_key, "
            f"    CAST(raw_value AS BLOB) AS raw_value, "
            f"    CAST(key_sha256 AS VARCHAR) AS key_sha256, "
            f"    CAST(value_sha256 AS VARCHAR) AS value_sha256, "
            f"    CAST(key_len AS INTEGER) AS key_len, "
            f"    CAST(value_len AS INTEGER) AS value_len, "
            f"    CAST(key_utf8 AS VARCHAR) AS key_utf8, "
            f"    CAST(key_namespace AS VARCHAR) AS key_namespace, "
            f"    CAST(value_kind AS VARCHAR) AS value_kind, "
            f"    CAST(value_text AS VARCHAR) AS value_text "
            f"  FROM _bulk_batch_df"
            f") TO '{sql_path}' (FORMAT PARQUET, COMPRESSION ZSTD)"
        )
        os.replace(tmp_path, out_path)
    except Exception:
        tmp_path.unlink(missing_ok=True)
        raise
    finally:
        con.close()

    parquet_bytes = out_path.read_bytes()
    return {
        "snapshot_id": snapshot_id,
        "source_db": source_db,
        "batch_id": batch_id,
        "seq_start": start_seq,
        "seq_end": start_seq + n - 1,
        "row_count": n,
        "file_size": len(parquet_bytes),
        "parquet_sha256": hashlib.sha256(parquet_bytes).hexdigest(),
        "rows_digest_sha256": compute_rows_digest(digest_rows),
        "first_key_sha256": key_shas[0],
        "last_key_sha256": key_shas[-1],
        "first_key": raw_keys[0],
        "last_key": raw_keys[-1],
    }


def _validate_staging_parquet(
    staging_path: Path,
    *,
    snapshot_id: str,
    source_db: str,
    batch_id: str,
    expected_row_count: int | None,
    expected_seq_start: int | None,
    expected_seq_end: int | None,
    expected_rows_digest: str | None,
) -> dict[str, Any]:
    import duckdb

    sql_path = str(staging_path).replace("\\", "/")
    con = duckdb.connect()
    try:
        try:
            desc_rows = con.execute("DESCRIBE SELECT * FROM read_parquet(?)", [sql_path]).fetchall()
        except Exception as exc:
            raise BulkImportError(f"Parquet 文件损坏或不可读: {exc}") from exc

        actual_cols = [(str(r[0]), str(r[1])) for r in desc_rows]
        if actual_cols != list(LEVELDB_RAW_COLUMNS):
            raise BulkImportError(
                f"Parquet schema 不匹配: expected={list(LEVELDB_RAW_COLUMNS)}, actual={actual_cols}"
            )

        stats = con.execute(
            "SELECT "
            "  COUNT(*) AS row_count, "
            "  COUNT(DISTINCT raw_key) AS distinct_keys, "
            "  COUNT(DISTINCT seq_no) AS distinct_seqs, "
            "  MIN(seq_no) AS seq_start, "
            "  MAX(seq_no) AS seq_end, "
            "  COUNT(*) FILTER (WHERE snapshot_id IS DISTINCT FROM ? OR source_db IS DISTINCT FROM ? OR batch_id IS DISTINCT FROM ?) AS wrong_ids, "
            "  COUNT(*) FILTER (WHERE raw_key IS NULL OR raw_value IS NULL OR seq_no IS NULL OR key_sha256 IS NULL OR value_sha256 IS NULL) AS null_req, "
            "  COUNT(*) FILTER (WHERE sha256(raw_key) != key_sha256 OR sha256(raw_value) != value_sha256 OR octet_length(raw_key) != key_len OR octet_length(raw_value) != value_len) AS bad_hashes "
            "FROM read_parquet(?)",
            [snapshot_id, source_db, batch_id, sql_path],
        ).fetchone()

        row_count, distinct_keys, distinct_seqs, seq_start, seq_end, wrong_ids, null_req, bad_hashes = stats
        if row_count <= 0:
            raise BulkImportError("Parquet 批次不能为空（0 行）")
        if wrong_ids:
            raise BulkImportError(f"Parquet 行内 snapshot_id/source_db/batch_id 与请求不一致: {wrong_ids} 行")
        if null_req:
            raise BulkImportError(f"Parquet 必填列存在 NULL: {null_req} 行")
        if bad_hashes:
            raise BulkImportError(f"Parquet 行内 SHA-256 或长度校验失败: {bad_hashes} 行")
        if distinct_keys != row_count:
            raise BulkImportError(f"Parquet 批次内存在重复键: row_count={row_count}, distinct_keys={distinct_keys}")
        if seq_start < 0 or distinct_seqs != row_count or (seq_end - seq_start + 1) != row_count:
            raise BulkImportError(
                f"Parquet 批次内 seq_no 非稠密连续: seq_start={seq_start}, seq_end={seq_end}, row_count={row_count}"
            )
        if expected_row_count is not None and row_count != expected_row_count:
            raise BulkImportError(f"行数不匹配: expected={expected_row_count}, actual={row_count}")
        if expected_seq_start is not None and seq_start != expected_seq_start:
            raise BulkImportError(f"seq_start 不匹配: expected={expected_seq_start}, actual={seq_start}")
        if expected_seq_end is not None and seq_end != expected_seq_end:
            raise BulkImportError(f"seq_end 不匹配: expected={expected_seq_end}, actual={seq_end}")

        rows = con.execute(
            "SELECT seq_no, key_sha256, value_sha256, raw_key, key_len + value_len AS raw_bytes "
            "FROM read_parquet(?) ORDER BY seq_no ASC",
            [sql_path],
        ).fetchall()
    finally:
        con.close()

    prev_key: bytes | None = None
    digest_items: list[tuple[int, str, str]] = []
    type_input_rows: list[tuple[bytes, int]] = []
    first_key_sha = ""
    last_key_sha = ""
    for seq_no, k_sha, v_sha, raw_k, rbytes in rows:
        rk = bytes(raw_k)
        if prev_key is not None and rk <= prev_key:
            raise BulkImportError("Parquet 行键未按 seq_no 对应严格字典序递增")
        prev_key = rk
        if not first_key_sha:
            first_key_sha = str(k_sha)
        last_key_sha = str(k_sha)
        digest_items.append((int(seq_no), str(k_sha), str(v_sha)))
        type_input_rows.append((rk, int(rbytes)))

    actual_digest = compute_rows_digest(digest_items)
    if expected_rows_digest is not None and actual_digest != expected_rows_digest:
        raise BulkImportError(
            f"rows_digest_sha256 校验失败: expected={expected_rows_digest}, actual={actual_digest}"
        )

    return {
        "row_count": int(row_count),
        "seq_start": int(seq_start),
        "seq_end": int(seq_end),
        "rows_digest_sha256": actual_digest,
        "first_key_sha256": first_key_sha,
        "last_key_sha256": last_key_sha,
        "type_stats": inventory.accumulate_batch_type_stats(type_input_rows),
    }


def ingest_parquet_batch(
    root: Path,
    payload: bytes,
    *,
    snapshot_id: str,
    source_db: str,
    batch_id: str,
    batch_index: int,
    parquet_sha256: str | None = None,
    expected_row_count: int | None = None,
    expected_seq_start: int | None = None,
    expected_seq_end: int | None = None,
    expected_rows_digest: str | None = None,
    source_fingerprint: str | None = None,
    refresh_views_cb: Callable[[], None] | None = None,
) -> dict[str, Any]:
    """NAS 侧接收、校验并原子入仓单个 Parquet 批次。"""
    root = Path(root)
    layout.validate_identifier(snapshot_id, "snapshot_id")
    layout.validate_identifier(source_db, "source_db")
    layout.validate_identifier(batch_id, "batch_id")
    if batch_index < 0:
        raise BulkImportError(f"batch_index 不能为负: {batch_index}")
    if not payload:
        raise BulkImportError("上传请求体为空")

    actual_parquet_sha256 = hashlib.sha256(payload).hexdigest()
    if parquet_sha256 is not None and actual_parquet_sha256 != parquet_sha256.lower():
        raise BulkImportError(
            f"parquet_sha256 校验失败: expected={parquet_sha256}, actual={actual_parquet_sha256}"
        )

    with _BULK_LOCK:
        con = catalog._connect(root)
        try:
            job_row = con.execute(
                "SELECT status, source_fingerprint, committed_batches, committed_rows, "
                "committed_bytes, last_seq_end "
                "FROM bulk_import_jobs WHERE snapshot_id = ? AND source_db = ?",
                [snapshot_id, source_db],
            ).fetchone()

            if job_row is not None:
                job_status, existing_fp, _, _, _, _ = job_row
                if (
                    source_fingerprint
                    and existing_fp
                    and existing_fp != source_fingerprint
                ):
                    raise BulkImportConflictError(
                        f"source_fingerprint 冲突: existing={existing_fp}, incoming={source_fingerprint}"
                    )

            existing_batch = con.execute(
                "SELECT batch_index, seq_start, seq_end, row_count, file_size, "
                "parquet_sha256, rows_digest_sha256, first_key_sha256, last_key_sha256, "
                "parquet_path, committed_at "
                "FROM bulk_import_batches "
                "WHERE snapshot_id = ? AND source_db = ? AND batch_id = ?",
                [snapshot_id, source_db, batch_id],
            ).fetchone()

            if existing_batch is not None:
                (
                    ex_idx,
                    ex_s0,
                    ex_s1,
                    ex_rows,
                    ex_size,
                    ex_psha,
                    ex_dsha,
                    ex_fk,
                    ex_lk,
                    ex_ppath,
                    ex_at,
                ) = existing_batch
                same = (
                    ex_psha == actual_parquet_sha256
                    and ex_idx == batch_index
                    and (expected_rows_digest is None or ex_dsha == expected_rows_digest)
                    and (expected_row_count is None or ex_rows == expected_row_count)
                    and (expected_seq_start is None or ex_s0 == expected_seq_start)
                    and (expected_seq_end is None or ex_s1 == expected_seq_end)
                    and Path(ex_ppath).is_file()
                )
                if not same:
                    raise BulkImportConflictError(
                        f"批次 {batch_id} 已存在但内容或元数据不一致，禁止覆盖不可变分区"
                    )
                return {
                    "ok": True,
                    "status": "duplicate_skipped",
                    "snapshot_id": snapshot_id,
                    "source_db": source_db,
                    "batch_id": batch_id,
                    "batch_index": int(ex_idx),
                    "seq_start": int(ex_s0),
                    "seq_end": int(ex_s1),
                    "row_count": int(ex_rows),
                    "file_size": int(ex_size),
                    "parquet_sha256": ex_psha,
                    "rows_digest_sha256": ex_dsha,
                    "first_key_sha256": ex_fk,
                    "last_key_sha256": ex_lk,
                    "parquet_path": ex_ppath,
                    "committed_at": ex_at,
                }

            if job_row is not None and job_row[0] in ("completed", "verified"):
                raise BulkImportConflictError(
                    f"导入任务 {snapshot_id}/{source_db} 状态已为 {job_row[0]}，不能追加新批次"
                )

            existing_all = con.execute(
                "SELECT batch_id, batch_index, seq_start, seq_end "
                "FROM bulk_import_batches WHERE snapshot_id = ? AND source_db = ?",
                [snapshot_id, source_db],
            ).fetchall()
        finally:
            con.close()

        staging_path = layout.leveldb_staging_path(
            root, snapshot_id, source_db, batch_id, token=uuid.uuid4().hex[:12]
        )
        staging_path.parent.mkdir(parents=True, exist_ok=True)
        try:
            staging_path.write_bytes(payload)
            meta = _validate_staging_parquet(
                staging_path,
                snapshot_id=snapshot_id,
                source_db=source_db,
                batch_id=batch_id,
                expected_row_count=expected_row_count,
                expected_seq_start=expected_seq_start,
                expected_seq_end=expected_seq_end,
                expected_rows_digest=expected_rows_digest,
            )

            seq_start = meta["seq_start"]
            seq_end = meta["seq_end"]
            for other_bid, other_idx, other_s0, other_s1 in existing_all:
                if other_idx == batch_index:
                    raise BulkImportConflictError(
                        f"batch_index={batch_index} 已被批次 {other_bid} 占用"
                    )
                if not (seq_end < other_s0 or seq_start > other_s1):
                    raise BulkImportConflictError(
                        f"seq_no 区间 [{seq_start}, {seq_end}] 与已提交批次 {other_bid} "
                        f"[{other_s0}, {other_s1}] 重叠"
                    )

            dest = layout.leveldb_batch_partition(root, snapshot_id, source_db, batch_id)
            dest.parent.mkdir(parents=True, exist_ok=True)
            if dest.exists():
                raise BulkImportConflictError(f"目标分区文件已存在且未登记: {dest}")
            os.replace(staging_path, dest)
        except Exception:
            staging_path.unlink(missing_ok=True)
            raise

        now = _now_iso()
        file_size = len(payload)
        dest_str = str(dest)
        con = catalog._connect(root)
        try:
            con.execute("BEGIN")
            con.execute(
                "INSERT INTO bulk_import_batches "
                "(snapshot_id, source_db, batch_id, batch_index, seq_start, seq_end, "
                "row_count, file_size, parquet_sha256, rows_digest_sha256, "
                "first_key_sha256, last_key_sha256, parquet_path, committed_at) "
                "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
                [
                    snapshot_id,
                    source_db,
                    batch_id,
                    batch_index,
                    seq_start,
                    seq_end,
                    meta["row_count"],
                    file_size,
                    actual_parquet_sha256,
                    meta["rows_digest_sha256"],
                    meta["first_key_sha256"],
                    meta["last_key_sha256"],
                    dest_str,
                    now,
                ],
            )
            agg = con.execute(
                "SELECT COUNT(*), COALESCE(SUM(row_count), 0), COALESCE(SUM(file_size), 0), "
                "COALESCE(MAX(seq_end), -1) "
                "FROM bulk_import_batches WHERE snapshot_id = ? AND source_db = ?",
                [snapshot_id, source_db],
            ).fetchone()
            committed_batches, committed_rows, committed_bytes, max_seq_end = agg
            con.execute(
                "INSERT INTO bulk_import_jobs "
                "(snapshot_id, source_db, status, source_fingerprint, expected_batches, "
                "committed_batches, committed_rows, committed_bytes, last_seq_end, "
                "created_at, updated_at) "
                "VALUES (?, ?, 'uploading', ?, NULL, ?, ?, ?, ?, ?, ?) "
                "ON CONFLICT (snapshot_id, source_db) DO UPDATE SET "
                "source_fingerprint = COALESCE(bulk_import_jobs.source_fingerprint, excluded.source_fingerprint), "
                "committed_batches = excluded.committed_batches, "
                "committed_rows = excluded.committed_rows, "
                "committed_bytes = excluded.committed_bytes, "
                "last_seq_end = excluded.last_seq_end, "
                "updated_at = excluded.updated_at",
                [
                    snapshot_id,
                    source_db,
                    source_fingerprint,
                    int(committed_batches),
                    int(committed_rows),
                    int(committed_bytes),
                    int(max_seq_end),
                    now,
                    now,
                ],
            )
            inventory.upsert_bulk_type_stats(
                con, snapshot_id, source_db, meta["type_stats"], now
            )
            inventory.record_inventory_event(
                root,
                "bulk_batch",
                snapshot_id=snapshot_id,
                source_db=source_db,
                event_id_override=f"import:{snapshot_id}:{source_db}:progress",
                summary_prefix=f"导入中 {source_db}：",
                con=con,
            )
            con.execute("COMMIT")
        except Exception:
            try:
                con.execute("ROLLBACK")
            except Exception:
                pass
            dest.unlink(missing_ok=True)
            raise
        finally:
            con.close()

        if refresh_views_cb is not None:
            refresh_views_cb()

        return {
            "ok": True,
            "status": "committed",
            "snapshot_id": snapshot_id,
            "source_db": source_db,
            "batch_id": batch_id,
            "batch_index": int(batch_index),
            "seq_start": seq_start,
            "seq_end": seq_end,
            "row_count": meta["row_count"],
            "file_size": file_size,
            "parquet_sha256": actual_parquet_sha256,
            "rows_digest_sha256": meta["rows_digest_sha256"],
            "first_key_sha256": meta["first_key_sha256"],
            "last_key_sha256": meta["last_key_sha256"],
            "parquet_path": dest_str,
            "committed_at": now,
            "committed_batches": int(committed_batches),
            "committed_rows": int(committed_rows),
            "last_seq_end": int(max_seq_end),
        }


def get_bulk_import_status(
    root: Path,
    *,
    snapshot_id: str | None = None,
    source_db: str | None = None,
) -> dict[str, Any]:
    """查询 NAS 侧批量导入任务及已提交批次进度。"""
    root = Path(root)
    if snapshot_id is not None:
        layout.validate_identifier(snapshot_id, "snapshot_id")
    if source_db is not None:
        layout.validate_identifier(source_db, "source_db")

    with _BULK_LOCK:
        con = catalog._connect(root)
        try:
            where = []
            params: list[Any] = []
            if snapshot_id is not None:
                where.append("snapshot_id = ?")
                params.append(snapshot_id)
            if source_db is not None:
                where.append("source_db = ?")
                params.append(source_db)
            clause = ("WHERE " + " AND ".join(where)) if where else ""
            jobs_rows = con.execute(
                f"SELECT snapshot_id, source_db, status, source_fingerprint, expected_batches, "
                f"committed_batches, committed_rows, committed_bytes, last_seq_end, "
                f"created_at, updated_at, completed_at, verified_at, verify_summary "
                f"FROM bulk_import_jobs {clause} "
                f"ORDER BY updated_at DESC, snapshot_id ASC, source_db ASC",
                params,
            ).fetchall()

            jobs = []
            for r in jobs_rows:
                jobs.append(
                    {
                        "snapshot_id": r[0],
                        "source_db": r[1],
                        "status": r[2],
                        "source_fingerprint": r[3],
                        "expected_batches": int(r[4]) if r[4] is not None else None,
                        "committed_batches": int(r[5]),
                        "committed_rows": int(r[6]),
                        "committed_bytes": int(r[7]),
                        "last_seq_end": int(r[8]),
                        "created_at": r[9],
                        "updated_at": r[10],
                        "completed_at": r[11],
                        "verified_at": r[12],
                        "verify_summary": json.loads(r[13]) if r[13] else None,
                    }
                )

            batches = []
            if snapshot_id is not None and source_db is not None:
                batch_rows = con.execute(
                    "SELECT batch_id, batch_index, seq_start, seq_end, row_count, file_size, "
                    "parquet_sha256, rows_digest_sha256, first_key_sha256, last_key_sha256, "
                    "parquet_path, committed_at "
                    "FROM bulk_import_batches "
                    "WHERE snapshot_id = ? AND source_db = ? "
                    "ORDER BY batch_index ASC",
                    [snapshot_id, source_db],
                ).fetchall()
                for b in batch_rows:
                    batches.append(
                        {
                            "batch_id": b[0],
                            "batch_index": int(b[1]),
                            "seq_start": int(b[2]),
                            "seq_end": int(b[3]),
                            "row_count": int(b[4]),
                            "file_size": int(b[5]),
                            "parquet_sha256": b[6],
                            "rows_digest_sha256": b[7],
                            "first_key_sha256": b[8],
                            "last_key_sha256": b[9],
                            "parquet_path": b[10],
                            "committed_at": b[11],
                        }
                    )
        finally:
            con.close()

    result: dict[str, Any] = {
        "ok": True,
        "snapshot_id": snapshot_id,
        "source_db": source_db,
        "jobs": jobs,
    }
    if snapshot_id is not None and source_db is not None:
        result["job"] = jobs[0] if jobs else None
        result["batches"] = batches
        result["committed_batch_ids"] = [b["batch_id"] for b in batches]
    return result


def complete_bulk_import(
    root: Path,
    *,
    snapshot_id: str,
    source_db: str,
    expected_batches: int,
    expected_total_rows: int | None = None,
    source_fingerprint: str | None = None,
    refresh_views_cb: Callable[[], None] | None = None,
) -> dict[str, Any]:
    """封口 `(snapshot_id, source_db)` 批量导入：校验无缺批、序号连续、跨批零重复键。"""
    root = Path(root)
    layout.validate_identifier(snapshot_id, "snapshot_id")
    layout.validate_identifier(source_db, "source_db")
    if expected_batches < 0:
        raise BulkImportError(f"expected_batches 不能为负: {expected_batches}")

    with _BULK_LOCK:
        con = catalog._connect(root)
        try:
            job_row = con.execute(
                "SELECT status, source_fingerprint FROM bulk_import_jobs "
                "WHERE snapshot_id = ? AND source_db = ?",
                [snapshot_id, source_db],
            ).fetchone()
            if (
                job_row is not None
                and source_fingerprint
                and job_row[1]
                and job_row[1] != source_fingerprint
            ):
                raise BulkImportConflictError(
                    f"source_fingerprint 冲突: existing={job_row[1]}, incoming={source_fingerprint}"
                )

            batches = con.execute(
                "SELECT batch_id, batch_index, seq_start, seq_end, row_count, file_size, parquet_path "
                "FROM bulk_import_batches WHERE snapshot_id = ? AND source_db = ? "
                "ORDER BY batch_index ASC",
                [snapshot_id, source_db],
            ).fetchall()

            if len(batches) != expected_batches:
                raise BulkImportConflictError(
                    f"批次数不完整: expected={expected_batches}, committed={len(batches)}"
                )

            expected_seq = 0
            total_rows = 0
            total_bytes = 0
            for idx, (bid, bidx, s0, s1, rcnt, fsize, ppath) in enumerate(batches):
                if bidx != idx:
                    raise BulkImportConflictError(f"批次索引缺口: expected={idx}, actual={bidx} ({bid})")
                if s0 != expected_seq or s1 != s0 + rcnt - 1:
                    raise BulkImportConflictError(
                        f"批次 {bid} seq_no 不连续: expected_start={expected_seq}, got=[{s0}, {s1}]"
                    )
                if not Path(ppath).is_file():
                    raise BulkImportConflictError(f"批次文件丢失: {ppath}")
                expected_seq = s1 + 1
                total_rows += int(rcnt)
                total_bytes += int(fsize)

            if expected_total_rows is not None and total_rows != expected_total_rows:
                raise BulkImportConflictError(
                    f"总行数不匹配: expected={expected_total_rows}, committed={total_rows}"
                )

            ensure_leveldb_views(con, root)
            if total_rows > 0:
                dup_check = con.execute(
                    "SELECT COUNT(*), COUNT(DISTINCT raw_key) "
                    "FROM v_leveldb_raw WHERE snapshot_id = ? AND source_db = ?",
                    [snapshot_id, source_db],
                ).fetchone()
                actual_rows, distinct_keys = int(dup_check[0]), int(dup_check[1])
                if actual_rows != total_rows:
                    raise BulkImportConflictError(
                        f"DuckDB 视图行数异常: view={actual_rows}, catalog={total_rows}"
                    )
                if distinct_keys != actual_rows:
                    raise BulkImportConflictError(
                        f"跨批次存在重复键: total={actual_rows}, distinct={distinct_keys}"
                    )

            now = _now_iso()
            next_status = "verified" if (job_row and job_row[0] == "verified") else "completed"
            con.execute(
                "INSERT INTO bulk_import_jobs "
                "(snapshot_id, source_db, status, source_fingerprint, expected_batches, "
                "committed_batches, committed_rows, committed_bytes, last_seq_end, "
                "created_at, updated_at, completed_at) "
                "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?) "
                "ON CONFLICT (snapshot_id, source_db) DO UPDATE SET "
                "status = excluded.status, "
                "source_fingerprint = COALESCE(bulk_import_jobs.source_fingerprint, excluded.source_fingerprint), "
                "expected_batches = excluded.expected_batches, "
                "committed_batches = excluded.committed_batches, "
                "committed_rows = excluded.committed_rows, "
                "committed_bytes = excluded.committed_bytes, "
                "last_seq_end = excluded.last_seq_end, "
                "updated_at = excluded.updated_at, "
                "completed_at = COALESCE(bulk_import_jobs.completed_at, excluded.completed_at)",
                [
                    snapshot_id,
                    source_db,
                    next_status,
                    source_fingerprint,
                    expected_batches,
                    len(batches),
                    total_rows,
                    total_bytes,
                    expected_seq - 1,
                    now,
                    now,
                    now,
                ],
            )
            inventory.record_inventory_event(
                root,
                "bulk_complete",
                snapshot_id=snapshot_id,
                source_db=source_db,
                summary_prefix=f"导入完成 {source_db}：",
                con=con,
            )
        finally:
            con.close()

        if refresh_views_cb is not None:
            refresh_views_cb()

    return {
        "ok": True,
        "status": next_status,
        "snapshot_id": snapshot_id,
        "source_db": source_db,
        "expected_batches": expected_batches,
        "committed_batches": len(batches),
        "committed_rows": total_rows,
        "committed_bytes": total_bytes,
        "last_seq_end": expected_seq - 1,
    }


def readback_bulk_import(
    root: Path,
    *,
    snapshot_id: str,
    source_db: str,
    after_seq: int = -1,
    limit: int | None = None,
    output_format: str = "parquet",
) -> tuple[bytes, dict[str, Any]]:
    """从 NAS DuckDB `v_leveldb_raw` 按键游标分页读回供对账（支持 parquet / json）。"""
    root = Path(root)
    layout.validate_identifier(snapshot_id, "snapshot_id")
    layout.validate_identifier(source_db, "source_db")
    max_rows = max(1, int(getattr(config, "WAREHOUSE_BULK_READBACK_MAX_ROWS", 100000)))
    effective_limit = min(max_rows, int(limit)) if limit is not None else max_rows
    if effective_limit <= 0:
        raise BulkImportError(f"limit 必须大于 0: {limit}")
    if output_format not in ("parquet", "json"):
        raise BulkImportError(f"不支持的读回格式: {output_format}")

    with _BULK_LOCK:
        con = catalog._connect(root)
        try:
            ensure_leveldb_views(con, root)
            rows = con.execute(
                "SELECT seq_no, raw_key, raw_value, key_sha256, value_sha256 "
                "FROM v_leveldb_raw "
                "WHERE snapshot_id = ? AND source_db = ? AND seq_no > ? "
                "ORDER BY seq_no ASC "
                "LIMIT ?",
                [snapshot_id, source_db, after_seq, effective_limit + 1],
            ).fetchall()

            has_more = len(rows) > effective_limit
            page_rows = rows[:effective_limit]
            returned_rows = len(page_rows)
            next_after_seq = int(page_rows[-1][0]) if page_rows else int(after_seq)

            if output_format == "json":
                encoded_rows = [
                    {
                        "seq_no": int(r[0]),
                        "raw_key_b64": base64.b64encode(bytes(r[1])).decode("ascii"),
                        "raw_value_b64": base64.b64encode(bytes(r[2])).decode("ascii"),
                        "key_sha256": r[3],
                        "value_sha256": r[4],
                    }
                    for r in page_rows
                ]
                body = json.dumps(
                    {
                        "ok": True,
                        "snapshot_id": snapshot_id,
                        "source_db": source_db,
                        "after_seq": int(after_seq),
                        "next_after_seq": next_after_seq,
                        "returned_rows": returned_rows,
                        "has_more": has_more,
                        "rows": encoded_rows,
                    },
                    ensure_ascii=False,
                ).encode("utf-8")
            else:
                tmp_path = (
                    root
                    / ".staging"
                    / "readback"
                    / f"rb_{snapshot_id}_{source_db}_{uuid.uuid4().hex[:10]}.parquet.tmp"
                )
                tmp_path.parent.mkdir(parents=True, exist_ok=True)
                try:
                    sql_path = str(tmp_path).replace("\\", "/").replace("'", "''")
                    sid_lit = snapshot_id.replace("'", "''")
                    src_lit = source_db.replace("'", "''")
                    con.execute(
                        f"COPY ("
                        f"  SELECT seq_no, raw_key, raw_value, key_sha256, value_sha256 "
                        f"  FROM v_leveldb_raw "
                        f"  WHERE snapshot_id = '{sid_lit}' AND source_db = '{src_lit}' "
                        f"    AND seq_no > {int(after_seq)} "
                        f"  ORDER BY seq_no ASC LIMIT {effective_limit}"
                        f") TO '{sql_path}' (FORMAT PARQUET, COMPRESSION ZSTD)"
                    )
                    body = tmp_path.read_bytes()
                finally:
                    tmp_path.unlink(missing_ok=True)
        finally:
            con.close()

    meta = {
        "snapshot_id": snapshot_id,
        "source_db": source_db,
        "after_seq": int(after_seq),
        "next_after_seq": next_after_seq,
        "returned_rows": returned_rows,
        "has_more": has_more,
        "format": output_format,
    }
    return body, meta


def verify_bulk_import(
    root: Path,
    *,
    snapshot_id: str,
    source_db: str,
    expected_total_rows: int | None = None,
    source_fingerprint: str | None = None,
    update_status: bool = True,
) -> dict[str, Any]:
    """核对 NAS 仓内已落盘批次与 DuckDB `v_leveldb_raw` 自身完整性。"""
    root = Path(root)
    layout.validate_identifier(snapshot_id, "snapshot_id")
    layout.validate_identifier(source_db, "source_db")

    with _BULK_LOCK:
        con = catalog._connect(root)
        try:
            job_row = con.execute(
                "SELECT status, source_fingerprint, committed_batches, committed_rows "
                "FROM bulk_import_jobs WHERE snapshot_id = ? AND source_db = ?",
                [snapshot_id, source_db],
            ).fetchone()
            if job_row is None:
                raise BulkImportError(f"未找到导入任务: {snapshot_id}/{source_db}")

            _, job_fp, committed_batches, committed_rows = job_row
            if source_fingerprint and job_fp and job_fp != source_fingerprint:
                raise BulkImportConflictError(
                    f"source_fingerprint 不一致: warehouse={job_fp}, expected={source_fingerprint}"
                )

            batches = con.execute(
                "SELECT batch_id, file_size, parquet_sha256, parquet_path, row_count, seq_start, seq_end "
                "FROM bulk_import_batches WHERE snapshot_id = ? AND source_db = ? ORDER BY batch_index ASC",
                [snapshot_id, source_db],
            ).fetchall()

            missing_files: list[str] = []
            corrupt_files: list[str] = []
            for bid, fsize, psha, ppath, _, _, _ in batches:
                p = Path(ppath)
                if not p.is_file():
                    missing_files.append(str(bid))
                    continue
                raw = p.read_bytes()
                if len(raw) != int(fsize) or hashlib.sha256(raw).hexdigest() != psha:
                    corrupt_files.append(str(bid))

            ensure_leveldb_views(con, root)
            stats = con.execute(
                "SELECT "
                "  COUNT(*) AS actual_rows, "
                "  COUNT(DISTINCT raw_key) AS distinct_keys, "
                "  COALESCE(MIN(seq_no), -1) AS min_seq, "
                "  COALESCE(MAX(seq_no), -1) AS max_seq, "
                "  COUNT(*) FILTER (WHERE sha256(raw_key) != key_sha256 OR sha256(raw_value) != value_sha256) AS hash_mismatches "
                "FROM v_leveldb_raw WHERE snapshot_id = ? AND source_db = ?",
                [snapshot_id, source_db],
            ).fetchone()

            actual_rows = int(stats[0])
            distinct_keys = int(stats[1])
            min_seq = int(stats[2])
            max_seq = int(stats[3])
            hash_mismatches = int(stats[4])
            duplicate_keys = actual_rows - distinct_keys

            seq_contiguous = (
                (actual_rows == 0 and min_seq == -1 and max_seq == -1)
                or (actual_rows > 0 and min_seq == 0 and max_seq == actual_rows - 1)
            )
            rows_match_catalog = actual_rows == int(committed_rows)
            rows_match_expected = expected_total_rows is None or actual_rows == int(expected_total_rows)

            passed = (
                not missing_files
                and not corrupt_files
                and duplicate_keys == 0
                and hash_mismatches == 0
                and seq_contiguous
                and rows_match_catalog
                and rows_match_expected
            )

            summary = {
                "ok": True,
                "passed": passed,
                "snapshot_id": snapshot_id,
                "source_db": source_db,
                "committed_batches": int(committed_batches),
                "catalog_rows": int(committed_rows),
                "warehouse_rows": actual_rows,
                "distinct_keys": distinct_keys,
                "duplicate_keys": duplicate_keys,
                "hash_mismatches": hash_mismatches,
                "min_seq": min_seq,
                "max_seq": max_seq,
                "seq_contiguous": seq_contiguous,
                "missing_files": missing_files,
                "corrupt_files": corrupt_files,
            }
            if expected_total_rows is not None:
                summary["expected_total_rows"] = int(expected_total_rows)

            if update_status and passed:
                now = _now_iso()
                con.execute(
                    "UPDATE bulk_import_jobs "
                    "SET status = 'verified', verified_at = ?, updated_at = ?, verify_summary = ? "
                    "WHERE snapshot_id = ? AND source_db = ?",
                    [now, now, json.dumps(summary, ensure_ascii=False), snapshot_id, source_db],
                )
                inventory.record_inventory_event(
                    root,
                    "bulk_verify",
                    snapshot_id=snapshot_id,
                    source_db=source_db,
                    summary_prefix=f"核验通过 {source_db}：",
                    con=con,
                )
        finally:
            con.close()

    return summary


def iter_duckdb_parquet_stream(
    fetch_page_fn: Callable[[int], tuple[bytes, dict[str, Any]]],
) -> Iterable[tuple[bytes, bytes]]:
    """把分页 `/readback?format=parquet` 结果流式解码为严格按 `seq_no` 递增的 `(raw_key, raw_value)`。"""
    import duckdb

    after_seq = -1
    con = duckdb.connect()
    try:
        while True:
            payload, meta = fetch_page_fn(after_seq)
            if payload:
                tmp_fd_path = Path(os.path.join(
                    os.environ.get("TEMP", os.environ.get("TMP", ".")),
                    f"stockdb_rb_{uuid.uuid4().hex}.parquet",
                ))
                try:
                    tmp_fd_path.write_bytes(payload)
                    sql_path = str(tmp_fd_path).replace("\\", "/").replace("'", "''")
                    rows = con.execute(
                        f"SELECT seq_no, raw_key, raw_value FROM read_parquet('{sql_path}') ORDER BY seq_no ASC"
                    ).fetchall()
                finally:
                    tmp_fd_path.unlink(missing_ok=True)
                for _, rk, rv in rows:
                    yield bytes(rk), bytes(rv)
            has_more = bool(meta.get("has_more"))
            next_after = int(meta.get("next_after_seq", after_seq))
            if not has_more or next_after <= after_seq:
                break
            after_seq = next_after
    finally:
        con.close()


def compare_sorted_kv_streams(
    source_iter: Iterable[tuple[bytes, bytes]],
    warehouse_iter: Iterable[tuple[bytes, bytes]],
    *,
    sample_limit: int = 10,
) -> dict[str, Any]:
    """流式双指针比对两条按字典序排列的 `(raw_key, raw_value)` 流，验收缺键/多键/错值/重键。"""
    src_it = iter(source_iter)
    wh_it = iter(warehouse_iter)

    sentinel = object()
    s_cur = next(src_it, sentinel)
    w_cur = next(wh_it, sentinel)

    source_rows = 0
    warehouse_rows = 0
    missing_keys = 0
    extra_keys = 0
    mismatched_values = 0
    duplicate_keys = 0
    order_errors = 0

    samples_missing: list[str] = []
    samples_extra: list[str] = []
    samples_mismatch: list[str] = []

    prev_s_key: bytes | None = None
    prev_w_key: bytes | None = None

    while s_cur is not sentinel or w_cur is not sentinel:
        if s_cur is not sentinel:
            sk, sv = s_cur
            if prev_s_key is not None and sk <= prev_s_key:
                order_errors += 1
        if w_cur is not sentinel:
            wk, wv = w_cur
            if prev_w_key is not None and wk == prev_w_key:
                duplicate_keys += 1
                warehouse_rows += 1
                w_cur = next(wh_it, sentinel)
                continue
            if prev_w_key is not None and wk < prev_w_key:
                order_errors += 1

        if s_cur is sentinel:
            wk, _ = w_cur
            prev_w_key = wk
            warehouse_rows += 1
            extra_keys += 1
            if len(samples_extra) < sample_limit:
                samples_extra.append(wk.hex())
            w_cur = next(wh_it, sentinel)
        elif w_cur is sentinel:
            sk, _ = s_cur
            prev_s_key = sk
            source_rows += 1
            missing_keys += 1
            if len(samples_missing) < sample_limit:
                samples_missing.append(sk.hex())
            s_cur = next(src_it, sentinel)
        else:
            sk, sv = s_cur
            wk, wv = w_cur
            if sk == wk:
                prev_s_key = sk
                prev_w_key = wk
                source_rows += 1
                warehouse_rows += 1
                if sv != wv:
                    mismatched_values += 1
                    if len(samples_mismatch) < sample_limit:
                        samples_mismatch.append(sk.hex())
                s_cur = next(src_it, sentinel)
                w_cur = next(wh_it, sentinel)
            elif sk < wk:
                prev_s_key = sk
                source_rows += 1
                missing_keys += 1
                if len(samples_missing) < sample_limit:
                    samples_missing.append(sk.hex())
                s_cur = next(src_it, sentinel)
            else:
                prev_w_key = wk
                warehouse_rows += 1
                extra_keys += 1
                if len(samples_extra) < sample_limit:
                    samples_extra.append(wk.hex())
                w_cur = next(wh_it, sentinel)

    passed = (
        missing_keys == 0
        and extra_keys == 0
        and mismatched_values == 0
        and duplicate_keys == 0
        and order_errors == 0
        and source_rows == warehouse_rows
    )
    return {
        "passed": passed,
        "source_rows": source_rows,
        "warehouse_rows": warehouse_rows,
        "missing_keys": missing_keys,
        "extra_keys": extra_keys,
        "mismatched_values": mismatched_values,
        "duplicate_keys": duplicate_keys,
        "order_errors": order_errors,
        "sample_missing_keys_hex": samples_missing,
        "sample_extra_keys_hex": samples_extra,
        "sample_mismatched_keys_hex": samples_mismatch,
    }


def verify_duckdb_against_snapshot(
    root: Path,
    *,
    snapshot_id: str,
    source_db: str,
    source_entries: Iterable[tuple[bytes, bytes]],
    page_size: int | None = None,
) -> dict[str, Any]:
    """直接读取本地 NAS `warehouse.duckdb` (`v_leveldb_raw`) 与源快照条目流做逐条比对。"""
    def _fetch(after_seq: int) -> tuple[bytes, dict[str, Any]]:
        return readback_bulk_import(
            root,
            snapshot_id=snapshot_id,
            source_db=source_db,
            after_seq=after_seq,
            limit=page_size,
            output_format="parquet",
        )

    cmp_res = compare_sorted_kv_streams(
        source_entries,
        iter_duckdb_parquet_stream(_fetch),
    )
    cmp_res["snapshot_id"] = snapshot_id
    cmp_res["source_db"] = source_db
    return cmp_res
