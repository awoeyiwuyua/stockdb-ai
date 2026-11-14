"""LevelDB 全量快照读取、Parquet 分批上传、断点续传及 DuckDB 读回核对端到端测试。"""
from __future__ import annotations

import json
import os
import shutil
import struct
import tempfile
import threading
import unittest
from http.server import ThreadingHTTPServer
from pathlib import Path

try:
    from compression import zstd as _zstd
except ImportError:  # pragma: no cover
    _zstd = None

import config
from core.board_metrics import is_supported_a_share_code
from storage.providers import msgpack_lite
from storage.warehouse import (
    bulk_import,
    bulk_sync_client,
    catalog as wh_catalog,
    daily_backfill,
    engine as wh_engine,
    leveldb_snapshot,
)
from storage.warehouse.leveldb_snapshot import crc32c, mask_crc


def _pack_msgpack_test(obj: object) -> bytes:
    """最小测试用 MsgPack 编码器（与 msgpack_lite.unpack 互逆）。"""
    if obj is None:
        return b"\xc0"
    if isinstance(obj, bool):
        return b"\xc3" if obj else b"\xc2"
    if isinstance(obj, int):
        if 0 <= obj <= 127:
            return bytes([obj])
        if -32 <= obj < 0:
            return bytes([obj & 0xFF])
        return b"\xd3" + struct.pack(">q", obj)
    if isinstance(obj, float):
        return b"\xcb" + struct.pack(">d", obj)
    if isinstance(obj, str):
        raw = obj.encode("utf-8")
        ln = len(raw)
        if ln <= 31:
            return bytes([0xA0 | ln]) + raw
        if ln <= 0xFF:
            return b"\xd9" + bytes([ln]) + raw
        return b"\xda" + struct.pack(">H", ln) + raw
    if isinstance(obj, (list, tuple)):
        n = len(obj)
        head = bytes([0x90 | n]) if n <= 15 else b"\xdc" + struct.pack(">H", n)
        return head + b"".join(_pack_msgpack_test(x) for x in obj)
    if isinstance(obj, dict):
        n = len(obj)
        head = bytes([0x80 | n]) if n <= 15 else b"\xde" + struct.pack(">H", n)
        body = b"".join(_pack_msgpack_test(k) + _pack_msgpack_test(v) for k, v in obj.items())
        return head + body
    raise TypeError(f"unsupported type in _pack_msgpack_test: {type(obj)}")


# ---------------------------------------------------------------------------
# 测试用标准 Google LevelDB 二进制夹具生成器（支持 RAW / Snappy / Zstd + WAL + MANIFEST）
# ---------------------------------------------------------------------------

def _encode_varint(n: int) -> bytes:
    out = bytearray()
    while True:
        b = n & 0x7F
        n >>= 7
        if n:
            out.append(b | 0x80)
        else:
            out.append(b)
            return bytes(out)


def _encode_slice(data: bytes) -> bytes:
    return _encode_varint(len(data)) + data


def _snappy_compress_test(src: bytes) -> bytes:
    """生成合法 Snappy 流（含 literal 与 back-reference copy），覆盖纯 Python 解压器全部路径。"""
    out = bytearray(_encode_varint(len(src)))
    # 如果包含较长重复段（测试用）可用 literal + copy；通用构造支持混合 literal 与 4+ 匹配
    pos = 0
    n = len(src)
    lit_start = 0

    def _flush_lit(end: int) -> None:
        chunk = src[lit_start:end]
        ln = len(chunk)
        if ln == 0:
            return
        if ln <= 60:
            out.append((ln - 1) << 2)
        else:
            extra = (ln - 1).to_bytes((ln - 1).bit_length() + 7 >> 3, "little")
            out.append((59 + len(extra)) << 2)
            out.extend(extra)
        out.extend(chunk)

    while pos < n:
        # 简单回溯窗口找最长匹配（仅 >= 4 字节，offset < 2048 测试 copy-1 与 copy-2）
        best_len = 0
        best_off = 0
        search_start = max(0, pos - 4096)
        if pos - search_start >= 4 and n - pos >= 4:
            target4 = src[pos : pos + 4]
            idx = src.rfind(target4, search_start, pos)
            if idx >= 0:
                off = pos - idx
                mlen = 4
                while pos + mlen < n and mlen < 64 and src[idx + (mlen % off)] == src[pos + mlen]:
                    mlen += 1
                best_len = mlen
                best_off = off
        if best_len >= 4:
            _flush_lit(pos)
            if best_off < 2048 and 4 <= best_len <= 11:
                out.append(1 | ((best_len - 4) << 2) | ((best_off >> 3) & 0xE0))
                out.append(best_off & 0xFF)
            else:
                out.append(2 | ((best_len - 1) << 2))
                out.extend(best_off.to_bytes(2, "little"))
            pos += best_len
            lit_start = pos
        else:
            pos += 1
    _flush_lit(n)
    return bytes(out)


def _encode_kv_block(entries: list[tuple[bytes, bytes]], restart_interval: int = 3) -> bytes:
    buf = bytearray()
    restarts: list[int] = []
    prev_key = b""
    for idx, (k, v) in enumerate(entries):
        if idx % restart_interval == 0:
            restarts.append(len(buf))
            shared = 0
        else:
            shared = 0
            limit = min(len(prev_key), len(k))
            while shared < limit and prev_key[shared] == k[shared]:
                shared += 1
        non_shared = len(k) - shared
        buf.extend(_encode_varint(shared))
        buf.extend(_encode_varint(non_shared))
        buf.extend(_encode_varint(len(v)))
        buf.extend(k[shared:])
        buf.extend(v)
        prev_key = k
    for r in restarts:
        buf.extend(struct.pack("<I", r))
    buf.extend(struct.pack("<I", len(restarts)))
    return bytes(buf)


def _wrap_block_trailer(uncompressed: bytes, comp_type: int) -> bytes:
    if comp_type == 0:
        payload = uncompressed
    elif comp_type == 1:
        payload = _snappy_compress_test(uncompressed)
    elif comp_type == 2:
        assert _zstd is not None
        payload = _zstd.compress(uncompressed)
    else:
        raise ValueError(comp_type)
    crc = mask_crc(crc32c(payload + bytes((comp_type,))))
    return payload + bytes((comp_type,)) + struct.pack("<I", crc)


def _write_sstable(
    path: Path,
    internal_entries: list[tuple[bytes, int, int, bytes]],
    *,
    block_max_entries: int = 4,
    compression_types: tuple[int, ...] = (0,),
) -> tuple[bytes, bytes, int]:
    """写入标准 `.ldb` SSTable（按 InternalKey 排序）。返回 `(smallest_ikey, largest_ikey, file_size)`。"""
    # InternalKey sort order: (user_key ASC, tag DESC)
    sorted_entries = sorted(internal_entries, key=lambda x: (x[0], -((x[1] << 8) | x[2])))
    ikey_pairs = [
        (uk + struct.pack("<Q", (seq << 8) | vtype), val)
        for uk, seq, vtype, val in sorted_entries
    ]
    raw_file = bytearray()
    index_entries: list[tuple[bytes, bytes]] = []

    blk_idx = 0
    for i in range(0, max(1, len(ikey_pairs)), block_max_entries):
        chunk = ikey_pairs[i : i + block_max_entries]
        if not chunk:
            break
        block_bytes = _encode_kv_block(chunk, restart_interval=2)
        comp = compression_types[blk_idx % len(compression_types)]
        wrapped = _wrap_block_trailer(block_bytes, comp)
        offset = len(raw_file)
        size = len(wrapped) - 5
        raw_file.extend(wrapped)
        handle = _encode_varint(offset) + _encode_varint(size)
        index_entries.append((chunk[-1][0], handle))
        blk_idx += 1

    # metaindex block (empty)
    meta_bytes = _wrap_block_trailer(_encode_kv_block([]), 0) if False else _wrap_block_trailer(struct.pack("<II", 0, 1), 0)
    meta_offset = len(raw_file)
    meta_size = len(meta_bytes) - 5
    raw_file.extend(meta_bytes)

    # index block
    idx_raw = _encode_kv_block(index_entries, restart_interval=2)
    idx_wrapped = _wrap_block_trailer(idx_raw, 0)
    idx_offset = len(raw_file)
    idx_size = len(idx_wrapped) - 5
    raw_file.extend(idx_wrapped)

    # 48-byte footer
    handles = (
        _encode_varint(meta_offset)
        + _encode_varint(meta_size)
        + _encode_varint(idx_offset)
        + _encode_varint(idx_size)
    )
    footer = handles.ljust(40, b"\x00") + struct.pack("<Q", 0xDB4775248B80FB57)
    raw_file.extend(footer)
    path.write_bytes(bytes(raw_file))
    return ikey_pairs[0][0], ikey_pairs[-1][0], len(raw_file)


def _encode_log_file(records: list[bytes]) -> bytes:
    """将逻辑记录列表按 LevelDB 32KiB 块日志格式编码（含跨块分片 FIRST/MIDDLE/LAST）。"""
    out = bytearray()
    pos_in_block = 0
    block_size = 32768
    header_size = 7

    for rec in records:
        offset = 0
        first = True
        while True:
            remain = block_size - pos_in_block
            if remain < header_size:
                out.extend(b"\x00" * remain)
                pos_in_block = 0
                remain = block_size
            avail = remain - header_size
            chunk_len = min(len(rec) - offset, avail)
            end = (offset + chunk_len) == len(rec)
            if first and end:
                rtype = 1
            elif first:
                rtype = 2
            elif end:
                rtype = 4
            else:
                rtype = 3
            chunk = rec[offset : offset + chunk_len]
            crc = mask_crc(crc32c(bytes((rtype,)) + chunk))
            out.extend(struct.pack("<IHB", crc, chunk_len, rtype))
            out.extend(chunk)
            pos_in_block = (pos_in_block + header_size + chunk_len) % block_size
            offset += chunk_len
            first = False
            if end:
                break
    return bytes(out)


def _encode_write_batch(start_seq: int, ops: list[tuple[int, bytes, bytes]]) -> bytes:
    buf = bytearray(struct.pack("<QI", start_seq, len(ops)))
    for vtype, k, v in ops:
        buf.append(vtype)
        buf.extend(_encode_slice(k))
        if vtype == 1:
            buf.extend(_encode_slice(v))
    return bytes(buf)


def _build_synthetic_leveldb(
    db_dir: Path,
    *,
    seed_prefix: bytes = b"",
    include_wal_fragments: bool = True,
    empty: bool = False,
) -> dict[bytes, bytes]:
    """在 `db_dir` 构造真实多层级 LevelDB 快照并返回理论存活字典。"""
    db_dir.mkdir(parents=True, exist_ok=True)
    if empty:
        edit = (
            _encode_varint(1) + _encode_slice(b"leveldb.BytewiseComparator")
            + _encode_varint(2) + _encode_varint(1)
            + _encode_varint(3) + _encode_varint(2)
            + _encode_varint(4) + _encode_varint(0)
        )
        (db_dir / "MANIFEST-000001").write_bytes(_encode_log_file([edit]))
        (db_dir / "CURRENT").write_text("MANIFEST-000001\n", encoding="utf-8")
        return {}

    truth: dict[bytes, bytes] = {}
    seq = 1

    # L1 两个非重叠文件（分别测试 Snappy、Zstd、RAW）
    l1_a: list[tuple[bytes, int, int, bytes]] = []
    l1_a.append((b"", seq, 1, b"empty-key-v1"))
    truth[b""] = b"empty-key-v1"
    seq += 1

    for i in range(15):
        k = seed_prefix + f"a_{i:04d}".encode("ascii")
        # 带重复模式的 value 以确保测试 Snappy copy-1/copy-2 与 Zstd
        v = (f"val_{i:04d}_" * 8).encode("utf-8")
        l1_a.append((k, seq, 1, v))
        truth[k] = v
        seq += 1

    l1_b: list[tuple[bytes, int, int, bytes]] = []
    for i in range(15):
        k = seed_prefix + f"m_{i:04d}".encode("ascii")
        v = json.dumps({"idx": i, "tag": "json_row"}).encode("utf-8")
        l1_b.append((k, seq, 1, v))
        truth[k] = v
        seq += 1

    # 添加未知格式键 & MsgPack & 纯二进制键（含 \x00 \xff）
    k_msg = seed_prefix + "日k:sh600519:20261112".encode("utf-8")
    # 手工构造 MsgPack map {"code": "sh600519", "close": 1520.5}
    v_msg = (
        b"\x82"
        + b"\xa4code\xa8sh600519"
        + b"\xa5close\xcb" + struct.pack(">d", 1520.5)
    )
    assert msgpack_lite.unpack(v_msg) == {"code": "sh600519", "close": 1520.5}
    l1_b.append((k_msg, seq, 1, v_msg))
    truth[k_msg] = v_msg
    seq += 1

    k_unk = seed_prefix + b"\x00\xffunknown\x80\x81key"
    v_unk = b"\xff\x00\xfe\x01binary_blob_" + bytes(range(32))
    l1_a.append((k_unk, seq, 1, v_unk))
    truth[k_unk] = v_unk
    seq += 1

    # L0 重叠文件：覆盖部分 a_0003..0006，删除 a_0001、m_0002、b""(保留后再加回来或测试空值)
    l0_entries: list[tuple[bytes, int, int, bytes]] = []
    k_ov = seed_prefix + b"a_0003"
    v_ov = b"overwritten-in-L0"
    l0_entries.append((k_ov, seq, 1, v_ov))
    truth[k_ov] = v_ov
    seq += 1

    k_del1 = seed_prefix + b"a_0001"
    l0_entries.append((k_del1, seq, 0, b""))
    truth.pop(k_del1, None)
    seq += 1

    k_del2 = seed_prefix + b"m_0002"
    l0_entries.append((k_del2, seq, 0, b""))
    truth.pop(k_del2, None)
    seq += 1

    # 老旧已删除表 file_number=2（不应出现在存活版本）
    s2_min, s2_max, s2_sz = _write_sstable(
        db_dir / "000002.ldb",
        [(seed_prefix + b"stale_deleted_file_key", 1, 1, b"should-not-appear")],
        compression_types=(0,),
    )
    comps = (0, 1, 2) if _zstd is not None else (0, 1)
    s3_min, s3_max, s3_sz = _write_sstable(db_dir / "000003.ldb", l1_a, block_max_entries=3, compression_types=comps)
    s4_min, s4_max, s4_sz = _write_sstable(db_dir / "000004.sst", l1_b, block_max_entries=4, compression_types=comps)
    s5_min, s5_max, s5_sz = _write_sstable(db_dir / "000005.ldb", l0_entries, block_max_entries=2, compression_types=(1,))

    # WAL 日志（包含大 payload 强制触发跨 32KiB 分片，以及覆盖/删除/空值）
    wal_ops: list[tuple[int, bytes, bytes]] = []
    wal_start = seq

    k_wal_ov = seed_prefix + b"a_0003"
    v_wal_ov = b"overwritten-again-in-WAL"
    wal_ops.append((1, k_wal_ov, v_wal_ov))
    truth[k_wal_ov] = v_wal_ov
    seq += 1

    k_wal_empty_val = seed_prefix + b"z_empty_val"
    wal_ops.append((1, k_wal_empty_val, b""))
    truth[k_wal_empty_val] = b""
    seq += 1

    k_wal_del = seed_prefix + b"m_0005"
    wal_ops.append((0, k_wal_del, b""))
    truth.pop(k_wal_del, None)
    seq += 1

    if include_wal_fragments:
        k_big = seed_prefix + b"z_big_fragmented"
        v_big = (b"FRAG_PAYLOAD_" * 4000) + b"\x00\xffEND"  # ~52KB > 32KB block
        wal_ops.append((1, k_big, v_big))
        truth[k_big] = v_big
        seq += 1

    wb = _encode_write_batch(wal_start, wal_ops)
    # 写入老 log 000001.log（应被 log_number=6 忽略！）和活跃 log 000006.log
    (db_dir / "000001.log").write_bytes(
        _encode_log_file([_encode_write_batch(1, [(1, seed_prefix + b"old_wal_ignored", b"x")])])
    )
    (db_dir / "000006.log").write_bytes(_encode_log_file([wb]))

    edit1 = (
        _encode_varint(1) + _encode_slice(b"leveldb.BytewiseComparator")
        + _encode_varint(7) + _encode_varint(1) + _encode_varint(2) + _encode_varint(s2_sz)
        + _encode_slice(s2_min) + _encode_slice(s2_max)
    )
    edit2 = (
        _encode_varint(6) + _encode_varint(1) + _encode_varint(2)
        + _encode_varint(7) + _encode_varint(1) + _encode_varint(3) + _encode_varint(s3_sz)
        + _encode_slice(s3_min) + _encode_slice(s3_max)
        + _encode_varint(7) + _encode_varint(1) + _encode_varint(4) + _encode_varint(s4_sz)
        + _encode_slice(s4_min) + _encode_slice(s4_max)
        + _encode_varint(7) + _encode_varint(0) + _encode_varint(5) + _encode_varint(s5_sz)
        + _encode_slice(s5_min) + _encode_slice(s5_max)
        + _encode_varint(2) + _encode_varint(6)
        + _encode_varint(9) + _encode_varint(0)
        + _encode_varint(3) + _encode_varint(10)
        + _encode_varint(4) + _encode_varint(seq - 1)
    )
    (db_dir / "MANIFEST-000005").write_bytes(_encode_log_file([edit1, edit2]))
    (db_dir / "CURRENT").write_text("MANIFEST-000005\n", encoding="utf-8")
    return truth


class BulkImportTests(unittest.TestCase):
    def setUp(self) -> None:
        self.tmp = Path(tempfile.mkdtemp(prefix="stockdb_bulk_test_"))
        self.wh_root = self.tmp / "warehouse"
        self.snap_root = self.tmp / "leveldb_snapshots"
        self.ckpt_dir = self.tmp / "checkpoints"

    def tearDown(self) -> None:
        wh_engine.reset_engine()
        shutil.rmtree(self.tmp, ignore_errors=True)

    def test_leveldb_snapshot_traversal_seek_and_crc_check(self):
        db_dir = self.snap_root / "mydb"
        truth = _build_synthetic_leveldb(db_dir)

        snap = leveldb_snapshot.open_leveldb_snapshot(db_dir)
        actual_list = list(snap.iter_entries())
        expected_sorted = sorted(truth.items(), key=lambda kv: kv[0])
        self.assertEqual(actual_list, expected_sorted)
        self.assertIn(b"", dict(actual_list))
        self.assertEqual(dict(actual_list)[b""], b"empty-key-v1")
        self.assertNotIn(b"a_0001", dict(actual_list))
        self.assertNotIn(b"m_0002", dict(actual_list))
        self.assertNotIn(b"m_0005", dict(actual_list))
        self.assertNotIn(b"stale_deleted_file_key", dict(actual_list))
        self.assertNotIn(b"old_wal_ignored", dict(actual_list))
        self.assertEqual(dict(actual_list)[b"a_0003"], b"overwritten-again-in-WAL")

        # 断点续读 start_after_key=b"" 必须仅跳过空键 b""
        after_empty = list(snap.iter_entries(start_after_key=b""))
        self.assertEqual(after_empty, expected_sorted[1:])

        # 跨文件、跨 block、跨 WAL start_after_key
        mid_key = expected_sorted[len(expected_sorted) // 2][0]
        after_mid = list(snap.iter_entries(start_after_key=mid_key))
        expected_after_mid = [kv for kv in expected_sorted if kv[0] > mid_key]
        self.assertEqual(after_mid, expected_after_mid)

        # 未向快照目录写 LOCK 等任何文件
        self.assertFalse((db_dir / "LOCK").exists())

        # 破坏 WAL 1 字节必须触发 CRC32C 异常
        wal_path = db_dir / "000006.log"
        bad_wal = bytearray(wal_path.read_bytes())
        bad_wal[20] ^= 0xFF
        wal_path.write_bytes(bytes(bad_wal))
        with self.assertRaises(leveldb_snapshot.LevelDBFormatError):
            list(leveldb_snapshot.open_leveldb_snapshot(db_dir).iter_entries())

    def test_empty_leveldb_sync_and_verify(self):
        db_dir = self.snap_root / "empty_db"
        _build_synthetic_leveldb(db_dir, empty=True)
        transport = bulk_sync_client.LocalBulkImportTransport(self.wh_root)
        res = bulk_sync_client.sync_snapshot_to_nas(
            db_dir,
            snapshot_id="snap_empty",
            transport=transport,
            checkpoint_path=self.ckpt_dir / "empty.json",
        )
        self.assertTrue(res["passed"])
        self.assertEqual(res["total_batches"], 0)
        self.assertEqual(res["total_rows"], 0)
        self.assertEqual(res["readback_verify"]["missing_keys"], 0)
        self.assertEqual(res["readback_verify"]["extra_keys"], 0)
        self.assertEqual(res["readback_verify"]["mismatched_values"], 0)

    def test_ingest_validation_idempotency_and_conflicts(self):
        items_0 = [(b"k1", b"v1"), (b"k2", b"v2")]
        items_1 = [(b"k3", b"v3"), (b"k4", b"v4")]
        p0 = self.tmp / "b0.parquet"
        p1 = self.tmp / "b1.parquet"
        meta0 = bulk_import.write_leveldb_batch_parquet(
            p0, snapshot_id="snap1", source_db="data", batch_id="b000000", start_seq=0, items=items_0
        )
        meta1 = bulk_import.write_leveldb_batch_parquet(
            p1, snapshot_id="snap1", source_db="data", batch_id="b000001", start_seq=2, items=items_1
        )
        bytes0 = p0.read_bytes()

        # SHA256 不匹配拒收
        with self.assertRaises(bulk_import.BulkImportError):
            bulk_import.ingest_parquet_batch(
                self.wh_root,
                bytes0,
                snapshot_id="snap1",
                source_db="data",
                batch_id="b000000",
                batch_index=0,
                parquet_sha256="0" * 64,
            )

        # 首次提交
        r0 = bulk_import.ingest_parquet_batch(
            self.wh_root,
            bytes0,
            snapshot_id="snap1",
            source_db="data",
            batch_id="b000000",
            batch_index=0,
            parquet_sha256=meta0["parquet_sha256"],
            expected_rows_digest=meta0["rows_digest_sha256"],
            source_fingerprint="fp-aaa",
        )
        self.assertEqual(r0["status"], "committed")

        # 同内容幂等重传
        r0_dup = bulk_import.ingest_parquet_batch(
            self.wh_root,
            bytes0,
            snapshot_id="snap1",
            source_db="data",
            batch_id="b000000",
            batch_index=0,
            parquet_sha256=meta0["parquet_sha256"],
            expected_rows_digest=meta0["rows_digest_sha256"],
            source_fingerprint="fp-aaa",
        )
        self.assertEqual(r0_dup["status"], "duplicate_skipped")

        # 同 batch_id 内容篡改 -> 409 冲突
        p0_alt = self.tmp / "b0_alt.parquet"
        bulk_import.write_leveldb_batch_parquet(
            p0_alt, snapshot_id="snap1", source_db="data", batch_id="b000000",
            start_seq=0, items=[(b"k1", b"tampered"), (b"k2", b"v2")],
        )
        with self.assertRaises(bulk_import.BulkImportConflictError):
            bulk_import.ingest_parquet_batch(
                self.wh_root,
                p0_alt.read_bytes(),
                snapshot_id="snap1",
                source_db="data",
                batch_id="b000000",
                batch_index=0,
                source_fingerprint="fp-aaa",
            )

        # 不同 batch_id 相同 seq 区间重叠 -> 409 冲突
        p_overlap = self.tmp / "b_ov.parquet"
        bulk_import.write_leveldb_batch_parquet(
            p_overlap, snapshot_id="snap1", source_db="data", batch_id="b_overlap",
            start_seq=1, items=[(b"k2x", b"vx")],
        )
        with self.assertRaises(bulk_import.BulkImportConflictError):
            bulk_import.ingest_parquet_batch(
                self.wh_root,
                p_overlap.read_bytes(),
                snapshot_id="snap1",
                source_db="data",
                batch_id="b_overlap",
                batch_index=1,
                source_fingerprint="fp-aaa",
            )

        # 缺批封口拒收
        with self.assertRaises(bulk_import.BulkImportConflictError):
            bulk_import.complete_bulk_import(
                self.wh_root, snapshot_id="snap1", source_db="data", expected_batches=2
            )

        # 上传第 2 批后成功封口
        bulk_import.ingest_parquet_batch(
            self.wh_root,
            p1.read_bytes(),
            snapshot_id="snap1",
            source_db="data",
            batch_id="b000001",
            batch_index=1,
            parquet_sha256=meta1["parquet_sha256"],
            expected_rows_digest=meta1["rows_digest_sha256"],
            source_fingerprint="fp-aaa",
        )
        comp = bulk_import.complete_bulk_import(
            self.wh_root, snapshot_id="snap1", source_db="data", expected_batches=2, expected_total_rows=4
        )
        self.assertEqual(comp["committed_rows"], 4)

    def test_resumable_sync_after_network_drop_and_ack_loss(self):
        db_dir = self.snap_root / "data"
        truth = _build_synthetic_leveldb(db_dir, include_wal_fragments=True)

        class FlakyTransport(bulk_sync_client.LocalBulkImportTransport):
            def __init__(self, root: Path) -> None:
                super().__init__(root)
                self.calls = 0
                self.mode = "drop_on_3rd"  # 1: ok, 2: ok, 3: pre-upload drop -> 4: ack-loss

            def upload_batch(self, meta, parquet_bytes):
                self.calls += 1
                if self.mode == "drop_on_3rd" and self.calls == 3:
                    raise bulk_sync_client.TransportError("模拟网络中断（NAS 未收到）")
                res = super().upload_batch(meta, parquet_bytes)
                if self.mode == "ack_loss_once":
                    self.mode = "normal"
                    raise bulk_sync_client.TransportError("模拟回包丢失（NAS 已落盘）")
                return res

        transport = FlakyTransport(self.wh_root)
        ckpt_file = self.ckpt_dir / "resumable.json"

        # 第一次：前两批成功，第三批上传前中断
        with self.assertRaises(bulk_sync_client.TransportError):
            bulk_sync_client.sync_snapshot_to_nas(
                db_dir,
                snapshot_id="snap_resume",
                transport=transport,
                checkpoint_path=ckpt_file,
                batch_size=5,
            )
        ckpt_state = json.loads(ckpt_file.read_text(encoding="utf-8"))
        self.assertEqual(ckpt_state["next_batch_index"], 2)
        self.assertEqual(ckpt_state["next_seq"], 10)

        # 第二次：第三批 NAS 已落盘但回包丢失（leaving pending_batch）
        transport.mode = "ack_loss_once"
        with self.assertRaises(bulk_sync_client.TransportError):
            bulk_sync_client.sync_snapshot_to_nas(
                db_dir,
                snapshot_id="snap_resume",
                transport=transport,
                checkpoint_path=ckpt_file,
                batch_size=5,
            )
        ckpt_state2 = json.loads(ckpt_file.read_text(encoding="utf-8"))
        self.assertIsNotNone(ckpt_state2["pending_batch"])
        self.assertEqual(ckpt_state2["pending_batch"]["batch_id"], "b000002")

        # 第三次：恢复后应自动晋升 b000002 并从 b000003 继续传完，最终零缺零多零错
        res = bulk_sync_client.sync_snapshot_to_nas(
            db_dir,
            snapshot_id="snap_resume",
            transport=transport,
            checkpoint_path=ckpt_file,
            batch_size=5,
            readback_page_size=7,
        )
        self.assertTrue(res["passed"])
        self.assertEqual(res["total_rows"], len(truth))
        rb = res["readback_verify"]
        self.assertEqual(rb["missing_keys"], 0)
        self.assertEqual(rb["extra_keys"], 0)
        self.assertEqual(rb["mismatched_values"], 0)
        self.assertEqual(rb["duplicate_keys"], 0)
        self.assertEqual(rb["order_errors"], 0)

        # 第四次重跑完全幂等（0 上传）
        res2 = bulk_sync_client.sync_snapshot_to_nas(
            db_dir,
            snapshot_id="snap_resume",
            transport=transport,
            checkpoint_path=ckpt_file,
            batch_size=5,
        )
        self.assertTrue(res2["passed"])
        self.assertEqual(res2["uploaded_this_run"], 0)

    def test_http_e2e_multi_db_sync_views_and_mismatch_detection(self):
        # 构造三个库：data、data1、mydb
        t_data = _build_synthetic_leveldb(self.snap_root / "data", seed_prefix=b"D0_")
        t_data1 = _build_synthetic_leveldb(self.snap_root / "data1", seed_prefix=b"D1_", include_wal_fragments=False)
        t_mydb = _build_synthetic_leveldb(self.snap_root / "mydb", seed_prefix=b"MY_", include_wal_fragments=False)

        import app as stockdb_app
        from interfaces.web.handlers import Handler
        import services.warehouse_tasks as wh_tasks

        orig_root = config.WAREHOUSE_DIR
        orig_token = config.WEBUI_TOKEN
        orig_enabled = config.WAREHOUSE_ENABLED
        orig_bulk_mod = wh_tasks.bulk_import
        orig_wh_root_fn = wh_tasks.warehouse_root
        orig_avail = wh_tasks.availability
        orig_refresh = wh_tasks.refresh_views

        wh_engine.reset_engine()
        config.WAREHOUSE_DIR = str(self.wh_root)
        config.WEBUI_TOKEN = "test-secret-token"
        config.WAREHOUSE_ENABLED = True
        wh_tasks.bulk_import = bulk_import
        wh_tasks.warehouse_root = lambda: Path(config.WAREHOUSE_DIR)
        wh_tasks.availability = lambda: (True, "")
        wh_tasks.refresh_views = lambda: wh_engine.get_engine().refresh_views()
        # 确保 app 导出符号使用最新配置
        _ = stockdb_app

        server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
        port = server.server_address[1]
        thread = threading.Thread(target=server.serve_forever, daemon=True)
        thread.start()
        try:
            base_url = f"http://127.0.0.1:{port}"

            # 未带认证令牌必须被 401 拒绝
            unauth_transport = bulk_sync_client.HttpBulkImportTransport(base_url, token="wrong-token")
            with self.assertRaises(bulk_sync_client.TransportError):
                unauth_transport.get_status("snap_e2e", "data")

            # 带正确令牌全量同步 3 个源库 + 分页 DuckDB 读回逐库比对
            transport = bulk_sync_client.HttpBulkImportTransport(base_url, token="test-secret-token")
            report = bulk_sync_client.sync_all_snapshots_to_nas(
                self.snap_root,
                snapshot_id="snap_e2e",
                transport=transport,
                checkpoint_dir=self.ckpt_dir,
                batch_size=8,
                readback_page_size=6,
            )
            self.assertTrue(report["passed"])
            self.assertEqual(report["databases"], ["data", "data1", "mydb"])
            truth_by_db = {"data": t_data, "data1": t_data1, "mydb": t_mydb}
            for item in report["results"]:
                db_name = item["source_db"]
                rb = item["readback_verify"]
                self.assertTrue(rb["passed"])
                self.assertEqual(rb["source_rows"], len(truth_by_db[db_name]))
                self.assertEqual(rb["warehouse_rows"], len(truth_by_db[db_name]))
                self.assertEqual(rb["missing_keys"], 0)
                self.assertEqual(rb["extra_keys"], 0)
                self.assertEqual(rb["mismatched_values"], 0)
                self.assertEqual(rb["duplicate_keys"], 0)

            # 通过 DuckDB WarehouseEngine.run_sql 直接查询 v_leveldb_raw / v_leveldb_snapshots / v_leveldb_latest
            eng = wh_engine.get_engine()
            eng.refresh_views()
            cnt_res = eng.run_sql(
                "SELECT source_db, COUNT(*) AS cnt FROM v_leveldb_latest GROUP BY source_db ORDER BY source_db"
            )
            self.assertEqual(
                cnt_res["rows"],
                [["data", len(t_data)], ["data1", len(t_data1)], ["mydb", len(t_mydb)]],
            )
            blob_res = eng.run_sql(
                "SELECT raw_key, raw_value, value_kind FROM v_leveldb_raw "
                "WHERE source_db = 'data' AND raw_key IS NOT NULL LIMIT 3"
            )
            self.assertEqual(len(blob_res["rows"]), 3)
            self.assertTrue(str(blob_res["rows"][0][0]).startswith("0x"))
            json.dumps(blob_res)  # 确保可 JSON 序列化（含 0x hex BLOB）

            # 负面测试：验证流式比对器能够精确检出缺键、多键、错值
            src_sorted = sorted(t_mydb.items())
            bad_missing = src_sorted[:-1]
            r_miss = bulk_import.compare_sorted_kv_streams(src_sorted, bad_missing)
            self.assertFalse(r_miss["passed"])
            self.assertEqual(r_miss["missing_keys"], 1)

            bad_extra = src_sorted + [(b"zzzz_extra", b"1")]
            r_extra = bulk_import.compare_sorted_kv_streams(src_sorted, bad_extra)
            self.assertFalse(r_extra["passed"])
            self.assertEqual(r_extra["extra_keys"], 1)

            k0, v0 = src_sorted[1]
            bad_val = list(src_sorted)
            bad_val[1] = (k0, v0 + b"!")
            r_val = bulk_import.compare_sorted_kv_streams(src_sorted, bad_val)
            self.assertFalse(r_val["passed"])
            self.assertEqual(r_val["mismatched_values"], 1)

            # CLI 脚本主入口端到端验证（全量 + 按近两年范围过滤）
            from scripts import sync_leveldb_to_warehouse as cli_sync
            cli_ckpt = self.tmp / "cli_ckpts"
            exit_code = cli_sync.main([
                "--snapshot-root", str(self.snap_root / "mydb"),
                "--snapshot-id", "snap_cli_e2e",
                "--nas-url", base_url,
                "--token", "test-secret-token",
                "--checkpoint-dir", str(cli_ckpt),
                "--batch-size", "10",
                "--readback-page-size", "10",
            ])
            self.assertEqual(exit_code, 0)

            exit_code_scoped = cli_sync.main([
                "--snapshot-root", str(self.snap_root / "mydb"),
                "--snapshot-id", "snap_cli_2y",
                "--nas-url", base_url,
                "--token", "test-secret-token",
                "--checkpoint-dir", str(cli_ckpt),
                "--since-date", "20241112",
                "--exclude-prefix", "MY_a_",
                "--max-rows", "15",
                "--batch-size", "6",
                "--readback-page-size", "7",
            ])
            self.assertEqual(exit_code_scoped, 0)
        finally:
            server.shutdown()
            server.server_close()
            wh_engine.reset_engine()
            config.WAREHOUSE_DIR = orig_root
            config.WEBUI_TOKEN = orig_token
            config.WAREHOUSE_ENABLED = orig_enabled
            wh_tasks.bulk_import = orig_bulk_mod
            wh_tasks.warehouse_root = orig_wh_root_fn
            wh_tasks.availability = orig_avail
            wh_tasks.refresh_views = orig_refresh

    def test_scoped_filter_since_date_prefix_max_rows_and_resume(self):
        db_dir = self.snap_root / "scoped_db"
        db_dir.mkdir(parents=True, exist_ok=True)
        items = [
            ("kidx:分钟k:k1:000001\x1f分钟k:000001:20250102093000".encode("utf-8"), b"idx-new"),
            ("k分钟k:000001:20230501093000".encode("utf-8"), b"min-old-2023"),
            ("k分钟k:000001:20241111150000".encode("utf-8"), b"min-old-before"),
            ("k分钟k:000001:20241112093000".encode("utf-8"), b"min-new-boundary"),
            ("k日k:000001:20240105".encode("utf-8"), b"day-old-2024"),
            ("k日k:000001:20250318".encode("utf-8"), b"day-new-2025"),
            ("k日k:000001:20261112".encode("utf-8"), b"day-new-2026"),
            ("k股票代码:000001".encode("utf-8"), b"meta-undated"),
            (b"\x00\xffbin_undated", b"\x01\x02\x03"),
        ]
        entries = [(k, idx + 1, 1, v) for idx, (k, v) in enumerate(items)]
        smin, smax, ssz = _write_sstable(db_dir / "000003.ldb", entries, block_max_entries=3, compression_types=(1,))
        edit = (
            _encode_varint(1) + _encode_slice(b"leveldb.BytewiseComparator")
            + _encode_varint(7) + _encode_varint(1) + _encode_varint(3) + _encode_varint(ssz)
            + _encode_slice(smin) + _encode_slice(smax)
            + _encode_varint(2) + _encode_varint(0)
            + _encode_varint(3) + _encode_varint(4)
            + _encode_varint(4) + _encode_varint(len(items))
        )
        (db_dir / "MANIFEST-000001").write_bytes(_encode_log_file([edit]))
        (db_dir / "CURRENT").write_text("MANIFEST-000001\n", encoding="utf-8")

        # 1) 近两年（>= 20241112）+ 排除索引 kidx: + 保留无日期元数据
        flt_keep_meta = leveldb_snapshot.KeyScopeFilter(
            since_date="20241112",
            exclude_prefixes=(b"kidx:",),
            include_undated=True,
        )
        snap = leveldb_snapshot.open_leveldb_snapshot(db_dir)
        got_meta = [k for k, _ in snap.iter_entries(key_filter=flt_keep_meta)]
        self.assertEqual(
            got_meta,
            [
                b"\x00\xffbin_undated",
                "k分钟k:000001:20241112093000".encode("utf-8"),
                "k日k:000001:20250318".encode("utf-8"),
                "k日k:000001:20261112".encode("utf-8"),
                "k股票代码:000001".encode("utf-8"),
            ],
        )

        # 2) 近两年 + 排除无日期键 + 断点续传 + max_rows=2
        flt_strict = leveldb_snapshot.KeyScopeFilter(
            since_date="20241112",
            exclude_prefixes=(b"kidx:",),
            include_undated=False,
            max_rows=2,
        )

        class DropSecondTransport(bulk_sync_client.LocalBulkImportTransport):
            def __init__(self, root: Path) -> None:
                super().__init__(root)
                self.n = 0

            def upload_batch(self, meta, parquet_bytes):
                self.n += 1
                if self.n == 2:
                    raise bulk_sync_client.TransportError("drop 2nd batch")
                return super().upload_batch(meta, parquet_bytes)

        tp = DropSecondTransport(self.wh_root)
        ckpt = self.ckpt_dir / "scoped.json"
        with self.assertRaises(bulk_sync_client.TransportError):
            bulk_sync_client.sync_snapshot_to_nas(
                db_dir,
                snapshot_id="snap_scoped_2y",
                transport=tp,
                checkpoint_path=ckpt,
                batch_size=1,
                key_filter=flt_strict,
            )

        # 修改过滤条件复用同一 snapshot_id/checkpoint 必须冲突拒收
        with self.assertRaises(bulk_import.BulkImportConflictError):
            bulk_sync_client.sync_snapshot_to_nas(
                db_dir,
                snapshot_id="snap_scoped_2y",
                transport=tp,
                checkpoint_path=ckpt,
                batch_size=1,
                key_filter=flt_keep_meta,
            )

        # 用原范围续传完成，严格导入 2 条并零差异核对
        res = bulk_sync_client.sync_snapshot_to_nas(
            db_dir,
            snapshot_id="snap_scoped_2y",
            transport=tp,
            checkpoint_path=ckpt,
            batch_size=1,
            key_filter=flt_strict,
        )
        self.assertTrue(res["passed"])
        self.assertEqual(res["total_rows"], 2)
        self.assertEqual(res["readback_verify"]["missing_keys"], 0)
        self.assertEqual(res["readback_verify"]["extra_keys"], 0)
        self.assertEqual(res["readback_verify"]["mismatched_values"], 0)

    def test_warehouse_inventory_types_ranges_and_change_tracking(self):
        from storage.warehouse import inventory
        from storage.warehouse import sink as wh_sink
        from services import warehouse_tasks as wh_tasks
        from interfaces.web.handlers import Handler
        from http.server import ThreadingHTTPServer
        import urllib.request
        import app as root_app
        from storage.warehouse import engine as wh_engine

        # 1. 类型识别覆盖标准中文表 + 二级索引 \x1f 键
        self.assertEqual(
            inventory.resolve_leveldb_type(b"k\xe6\x97\xa5k:000001:20250102"),
            ("k日k", "日线（原库）", False),
        )
        self.assertEqual(
            inventory.resolve_leveldb_type(b"kidx:hk\xe6\x97\xa5k:k2:20261105\x1fhk\xe6\x97\xa5k:00700:20261105"),
            ("kidx:hk日k", "港股日线索引", True),
        )
        self.assertEqual(
            inventory.resolve_leveldb_type(b"k\xe5\x88\x86\xe9\x92\x9fk:600519:20261112093500"),
            ("k分钟k", "分钟线", False),
        )

        # 2. 写入一日标准日线分区 + 持久化对账记录
        wh_sink.write_daily(
            self.wh_root,
            "20261112",
            [
                {
                    "code": "600519",
                    "open": 1500.0,
                    "high": 1510.0,
                    "low": 1495.0,
                    "close": 1505.0,
                    "prev_close": 1498.0,
                    "volume": 1000,
                    "amount": 1500000.0,
                    "is_st": False,
                    "name": "贵州茅台",
                }
            ],
        )
        inventory.save_daily_reconcile(
            self.wh_root,
            {"date": "20261112", "ok": True, "traded": 5196, "stored": 5196},
        )
        # 首次读取建立基线
        inv0 = inventory.get_warehouse_inventory(self.wh_root)
        self.assertEqual(inv0["highlights"][0]["label"], "日线")
        self.assertEqual(inv0["highlights"][0]["range_text"], "2026-11-12")
        self.assertEqual(inv0["reconcile"]["daily"]["traded"], 5196)
        self.assertGreaterEqual(len(inv0["changes"]), 1)

        # 3. 执行一轮包含多种数据类型与二级索引的批量导入，验证类型与变化增量准确记录
        db_dir = self.snap_root / "mydb_inv"
        db_dir.mkdir(parents=True, exist_ok=True)
        inv_kvs = sorted(
            [
                (b"khk\xe6\x97\xa5k:00700:20241115", b'{"c": 400}'),
                (b"khk\xe6\x97\xa5k:00700:20261112", b'{"c": 420}'),
                ("kidx:hk日k:k2:20261112\x1fhk日k:00700:20261112".encode("utf-8"), b""),
                (b"k\xe8\xb5\x84\xe9\x87\x91\xe6\xb5\x81:000001:20250601", b'{"net": 12}'),
            ],
            key=lambda x: x[0],
        )
        entries = [(k, idx + 1, 1, v) for idx, (k, v) in enumerate(inv_kvs)]
        smin, smax, ssz = _write_sstable(db_dir / "000003.ldb", entries, block_max_entries=2, compression_types=(1,))
        edit = (
            _encode_varint(1) + _encode_slice(b"leveldb.BytewiseComparator")
            + _encode_varint(7) + _encode_varint(1) + _encode_varint(3) + _encode_varint(ssz)
            + _encode_slice(smin) + _encode_slice(smax)
            + _encode_varint(2) + _encode_varint(0)
            + _encode_varint(3) + _encode_varint(4)
            + _encode_varint(4) + _encode_varint(len(entries))
        )
        (db_dir / "MANIFEST-000001").write_bytes(_encode_log_file([edit]))
        (db_dir / "CURRENT").write_text("MANIFEST-000001\n", encoding="utf-8")

        orig_root = config.WAREHOUSE_DIR
        orig_token = config.WEBUI_TOKEN
        orig_enabled = config.WAREHOUSE_ENABLED
        orig_inv = wh_tasks.inventory
        orig_bulk = wh_tasks.bulk_import
        orig_root_fn = wh_tasks.warehouse_root
        orig_avail = wh_tasks.availability
        orig_refresh = wh_tasks.refresh_views

        wh_engine.reset_engine()
        config.WAREHOUSE_DIR = self.wh_root
        config.WEBUI_TOKEN = "test-secret-token"
        config.WAREHOUSE_ENABLED = True
        wh_tasks.inventory = inventory
        wh_tasks.bulk_import = bulk_import
        wh_tasks.warehouse_root = lambda: self.wh_root
        wh_tasks.availability = lambda: (True, "ok")
        wh_tasks.refresh_views = lambda: wh_engine.get_engine().refresh_views()
        root_app.invalidate_warehouse_caches()

        server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
        port = server.server_address[1]
        t = threading.Thread(target=server.serve_forever, daemon=True)
        t.start()
        try:
            tp = bulk_sync_client.HttpBulkImportTransport(f"http://127.0.0.1:{port}", token="test-secret-token")
            ckpt = self.tmp / "inv.ckpt.json"
            res = bulk_sync_client.sync_snapshot_to_nas(
                db_dir,
                snapshot_id="snap_inv_001",
                source_db="mydb",
                transport=tp,
                checkpoint_path=ckpt,
                batch_size=2,
            )
            self.assertTrue(res["passed"])

            req = urllib.request.Request(
                f"http://127.0.0.1:{port}/api/warehouse/inventory?refresh=1",
                headers={"X-StockDB-Token": "test-secret-token"},
                method="GET",
            )
            with urllib.request.urlopen(req, timeout=15) as resp:
                inv_http = json.loads(resp.read().decode("utf-8"))

            self.assertTrue(inv_http["ok"])
            by_label = {d["label"]: d for d in inv_http["datasets"]}
            self.assertEqual(by_label["日线"]["range_text"], "2026-11-12")
            self.assertEqual(by_label["港股日线"]["rows"], 2)
            self.assertEqual(by_label["港股日线"]["range_text"], "2024-11-15 ~ 2026-11-12")
            self.assertEqual(by_label["资金流向"]["rows"], 1)
            self.assertEqual(by_label["资金流向"]["range_text"], "2025-06-01")
            self.assertEqual(by_label["港股日线索引"]["is_index"], True)
            self.assertEqual(by_label["港股日线索引"]["rows"], 1)

            # 变化历史里应记录导入完成后相对基线 +4 行，并指明新增类型
            complete_events = [c for c in inv_http["changes"] if c["trigger_kind"] == "bulk_complete"]
            self.assertGreaterEqual(len(complete_events), 1)
            self.assertEqual(complete_events[0]["delta_rows"], 4)
            self.assertIn("港股日线", complete_events[0]["summary_text"])
            self.assertEqual(inv_http["reconcile"]["bulk"]["verified_count"], 1)
        finally:
            server.shutdown()
            server.server_close()
            wh_engine.reset_engine()
            root_app.invalidate_warehouse_caches()
            config.WAREHOUSE_DIR = orig_root
            config.WEBUI_TOKEN = orig_token
            config.WAREHOUSE_ENABLED = orig_enabled
            wh_tasks.inventory = orig_inv
            wh_tasks.bulk_import = orig_bulk
            wh_tasks.warehouse_root = orig_root_fn
            wh_tasks.availability = orig_avail
            wh_tasks.refresh_views = orig_refresh

    def test_exclude_prefix_skips_excluded_sstable_files(self):
        db_dir = self.snap_root / "sst_skip_db"
        db_dir.mkdir(parents=True, exist_ok=True)
        # L1 两个不重叠 SST：000003 仅含 k分钟k:，000004 仅含 k资金流:；L0 000005 仅含 k分钟k:
        min_entries = [
            ("k分钟k:000001:20250603093000".encode("utf-8"), 1, 1, b"m1"),
            ("k分钟k:600519:20250603093000".encode("utf-8"), 2, 1, b"m2"),
        ]
        cap_entries = [
            ("k资金流:000001:20250603".encode("utf-8"), 3, 1, b"cap1"),
        ]
        l0_min_entries = [
            ("k分钟k:000002:20250603093000".encode("utf-8"), 4, 1, b"m3"),
        ]
        m_min, m_max, m_sz = _write_sstable(db_dir / "000003.ldb", min_entries)
        c_min, c_max, c_sz = _write_sstable(db_dir / "000004.ldb", cap_entries)
        l0_min, l0_max, l0_sz = _write_sstable(db_dir / "000005.ldb", l0_min_entries)

        edit = (
            _encode_varint(1) + _encode_slice(b"leveldb.BytewiseComparator")
            + _encode_varint(7) + _encode_varint(1) + _encode_varint(3) + _encode_varint(m_sz)
            + _encode_slice(m_min) + _encode_slice(m_max)
            + _encode_varint(7) + _encode_varint(1) + _encode_varint(4) + _encode_varint(c_sz)
            + _encode_slice(c_min) + _encode_slice(c_max)
            + _encode_varint(7) + _encode_varint(0) + _encode_varint(5) + _encode_varint(l0_sz)
            + _encode_slice(l0_min) + _encode_slice(l0_max)
            + _encode_varint(2) + _encode_varint(0)
            + _encode_varint(3) + _encode_varint(6)
            + _encode_varint(4) + _encode_varint(4)
        )
        (db_dir / "MANIFEST-000001").write_bytes(_encode_log_file([edit]))
        (db_dir / "CURRENT").write_text("MANIFEST-000001\n", encoding="utf-8")

        # 破坏纯 k分钟k: SSTable 正文，若被打开读取必抛异常
        (db_dir / "000003.ldb").write_bytes(b"corrupted-excluded-sst")
        (db_dir / "000005.ldb").write_bytes(b"corrupted-excluded-l0-sst")

        opened_tables: list[str] = []
        orig_reader_cls = leveldb_snapshot._SSTableReader

        def _spy_reader(table_path: Path, verify_checksums: bool = True):
            opened_tables.append(table_path.name)
            return orig_reader_cls(table_path, verify_checksums=verify_checksums)

        flt = leveldb_snapshot.KeyScopeFilter(exclude_prefixes=(b"k\xe5\x88\x86\xe9\x92\x9fk:",))
        snap = leveldb_snapshot.open_leveldb_snapshot(db_dir)
        from unittest import mock as _mock
        with _mock.patch.object(leveldb_snapshot, "_SSTableReader", side_effect=_spy_reader):
            got = list(snap.iter_entries(key_filter=flt))

        self.assertEqual(opened_tables, ["000004.ldb"])
        self.assertEqual(got, [("k资金流:000001:20250603".encode("utf-8"), b"cap1")])

    @staticmethod
    def _sample_kdaily_msgpack(code: str, close: float, prev_close: float, *, name: str = "平安银行") -> bytes:
        payload = {
            "code": code,
            "name": name,
            "is_st": False,
            "open": prev_close + 0.1,
            "high": close + 0.2,
            "low": prev_close - 0.1,
            "close": close,
            "pre_close": prev_close,
            "volume": 1000000.0,
            "amount": close * 1000000.0,
            "turnover": 1.25,
            "pct_chg": round((close - prev_close) / prev_close * 100.0, 2),
            "amplitude": 2.1,
            "vol_ratio": 1.1,
            "pb": 0.9,
            "pe_ttm": 6.5,
            "total_share": 1e9,
            "float_share": 8e8,
            "total_mv": close * 1e9,
            "float_mv": close * 8e8,
        }
        return _pack_msgpack_test(payload)

    def _build_single_sstable_db(self, db_dir: Path, items: list[tuple[bytes, bytes]]) -> None:
        db_dir.mkdir(parents=True, exist_ok=True)
        sorted_items = sorted(items, key=lambda kv: kv[0])
        entries = [(k, idx + 1, 1, v) for idx, (k, v) in enumerate(sorted_items)]
        smin, smax, ssz = _write_sstable(db_dir / "000003.ldb", entries, block_max_entries=2)
        edit = (
            _encode_varint(1) + _encode_slice(b"leveldb.BytewiseComparator")
            + _encode_varint(7) + _encode_varint(1) + _encode_varint(3) + _encode_varint(ssz)
            + _encode_slice(smin) + _encode_slice(smax)
            + _encode_varint(2) + _encode_varint(0)
            + _encode_varint(3) + _encode_varint(4)
            + _encode_varint(4) + _encode_varint(len(entries))
        )
        (db_dir / "MANIFEST-000001").write_bytes(_encode_log_file([edit]))
        (db_dir / "CURRENT").write_text("MANIFEST-000001\n", encoding="utf-8")

    def test_daily_backfill_and_prune_preserves_recent_and_filters_stocks(self):
        from storage.warehouse import inventory
        from storage.warehouse import sink as wh_sink

        # 1) 预置标准 facts/daily 最近日 20251121（含哨兵字段）与 adjust_events
        wh_sink.write_daily(
            self.wh_root,
            "20251121",
            [{
                "code": "000001", "name": "KEEP_EXISTING", "is_st": False,
                "open": 11.0, "high": 11.2, "low": 10.9, "close": 11.1,
                "prev_close": 11.0, "volume": 100.0, "amount": 1100.0,
            }],
        )
        wh_catalog.record_adjust_events(
            self.wh_root,
            [{"code": "000001", "date": "20251110", "div": 0.1, "give": 0.0, "trans": 0.0, "mult": 1.0, "cum": 2.0}],
        )
        self.assertEqual(wh_catalog.get_watermark(self.wh_root, "daily"), "20251121")

        # 2) 构建并导入模拟 data / data1（batch_size=2 让 k分钟k 前缀批保持免重写）
        db_data = self.snap_root / "data"
        db_data1 = self.snap_root / "data1"
        week_days = ["20251110", "20251111", "20251112", "20251113", "20251114"]
        data_items: list[tuple[bytes, bytes]] = [
            ("k股票代码:000001".encode("utf-8"), b'{"code":"000001"}'),
            ("k日k:510300:20251110".encode("utf-8"), self._sample_kdaily_msgpack("510300", 4.1, 4.0, name="沪深300ETF")),
            ("k日k:920001:20251110".encode("utf-8"), self._sample_kdaily_msgpack("920001", 8.2, 8.0, name="北交所样例")),
            ("k日k:000001:20251121".encode("utf-8"), self._sample_kdaily_msgpack("000001", 99.9, 90.0, name="SHOULD_NOT_OVERWRITE")),
        ]
        for idx, d8 in enumerate(week_days):
            data_items.append(
                (f"k日k:000001:{d8}".encode("utf-8"), self._sample_kdaily_msgpack("000001", 10.0 + idx, 9.8 + idx))
            )
            data_items.append(
                (f"k日k:600519:{d8}".encode("utf-8"), self._sample_kdaily_msgpack("600519", 1500.0 + idx, 1490.0 + idx, name="贵州茅台"))
            )
        self._build_single_sstable_db(db_data, data_items)

        data1_items: list[tuple[bytes, bytes]] = [
            ("k分钟k:000001:20251110093000".encode("utf-8"), b"m1"),
            ("k分钟k:000001:20251110093100".encode("utf-8"), b"m2"),
            ("k分钟k:000001:20251110093200".encode("utf-8"), b"m3"),
            ("k分钟k:000001:20251110093300".encode("utf-8"), b"m4"),
            ("k日k:000001:20251114".encode("utf-8"), self._sample_kdaily_msgpack("000001", 14.5, 13.0)),
            ("k复权:600519".encode("utf-8"), _pack_msgpack_test([{"date": "20251112", "div": 5.0, "give": 0.0, "trans": 0.0, "mult": 1.0, "cum": 1.25}])),
            ("k资金流:000001:20251110".encode("utf-8"), b"cap-row"),
        ]
        self._build_single_sstable_db(db_data1, data1_items)

        tp = bulk_sync_client.LocalBulkImportTransport(self.wh_root)
        r_data = bulk_sync_client.sync_snapshot_to_nas(
            db_data,
            snapshot_id="snap_dedup",
            source_db="data",
            transport=tp,
            checkpoint_path=self.ckpt_dir / "data.json",
            batch_size=4,
        )
        r_data1 = bulk_sync_client.sync_snapshot_to_nas(
            db_data1,
            snapshot_id="snap_dedup",
            source_db="data1",
            transport=tp,
            checkpoint_path=self.ckpt_dir / "data1.json",
            batch_size=2,
        )
        self.assertTrue(r_data["passed"] and r_data1["passed"])

        # 3) 回填至唯一标准 facts/daily（只留 A 股，跳过已存在 20251121，级联完整周 20251114）
        orig_dir = config.WAREHOUSE_DIR
        wh_engine.reset_engine()
        config.WAREHOUSE_DIR = self.wh_root
        try:
            bf = daily_backfill.backfill_daily_from_leveldb_warehouse(
                self.wh_root,
                code_predicate=is_supported_a_share_code,
                is_trading_day_fn=lambda d: d.weekday() < 5,
                batch_days=3,
                refresh_views_cb=lambda: wh_engine.get_engine().refresh_views(),
            )
            self.assertTrue(bf["passed"])
            self.assertEqual(sorted(bf["written_dates"]), week_days)
            self.assertIn("20251121", bf["skipped_dates"])
            self.assertGreaterEqual(bf["filtered_non_stock_rows"], 2)
            self.assertIn("20251114", bf["weeks_written"])
            self.assertEqual(wh_catalog.get_watermark(self.wh_root, "daily"), "20251121")

            eng = wh_engine.get_engine()
            eng.refresh_views()
            # 验证 20251121 原分区未被改写
            keep_row = eng.run_sql("SELECT name, close FROM v_daily WHERE code='000001' AND date='2025-11-21'")["rows"][0]
            self.assertEqual((keep_row[0], float(keep_row[1])), ("KEEP_EXISTING", 11.1))
            # 验证 ETF/北交所被剔除
            non_stock = eng.run_sql("SELECT COUNT(*) FROM v_daily WHERE code IN ('510300', '920001')")["rows"][0][0]
            self.assertEqual(non_stock, 0)
            # 验证 data1 覆盖 data + pre_close -> prev_close + adjust_events 与 k复权物化复权列
            ov_row = eng.run_sql(
                "SELECT close, prev_close, adj_factor, close_fq FROM v_daily WHERE code='000001' AND date='2025-11-14'"
            )["rows"][0]
            self.assertEqual((float(ov_row[0]), float(ov_row[1]), float(ov_row[2]), float(ov_row[3])), (14.5, 13.0, 2.0, 29.0))
            fq_before = eng.run_sql("SELECT adj_factor FROM v_daily WHERE code='600519' AND date='2025-11-11'")["rows"][0][0]
            fq_after = eng.run_sql("SELECT adj_factor, close, close_fq FROM v_daily WHERE code='600519' AND date='2025-11-12'")["rows"][0]
            self.assertIsNone(fq_before)
            self.assertEqual((float(fq_after[0]), float(fq_after[1]), float(fq_after[2])), (1.25, 1502.0, 1502.0 * 1.25))
            # 周K 已自动物化
            wk_cnt = eng.run_sql("SELECT COUNT(*) FROM v_week WHERE date='2025-11-14'")["rows"][0][0]
            self.assertEqual(wk_cnt, 2)

            # 4) 裁剪 facts/leveldb_raw 中的 k日k 重复数据并验证自洽
            prune_res = daily_backfill.prune_leveldb_namespace(
                self.wh_root,
                namespaces=("k日k",),
                batch_size=2,
                refresh_views_cb=lambda: wh_engine.get_engine().refresh_views(),
            )
            self.assertTrue(prune_res["passed"])
            self.assertGreater(prune_res["removed_rows"], 0)
            jobs_by_db = {j["source_db"]: j for j in prune_res["updated_jobs"]}
            # data1 前两批均为 k分钟k（4行），应在无重写的前提下原封保留
            self.assertEqual(jobs_by_db["data1"]["prefix_batches_untouched"], 2)

            inv = inventory.get_warehouse_inventory(self.wh_root)
            type_keys = {d["type_key"] for d in inv["datasets"]}
            self.assertIn("daily", type_keys)
            self.assertNotIn("k日k", type_keys)
            self.assertIn("k资金流", type_keys)
            self.assertIn("k分钟k", type_keys)
            self.assertTrue(
                all(j["status"] == "verified" for j in inv["jobs"])
            )
        finally:
            wh_engine.reset_engine()
            config.WAREHOUSE_DIR = orig_dir

    def test_http_sync_all_standard_daily_mode_e2e(self):
        import app as root_app
        from interfaces.web.handlers import Handler
        from scripts import sync_leveldb_to_warehouse as cli_sync
        import services.warehouse_tasks as wh_tasks

        db_data = self.snap_root / "e2e_snap" / "data"
        db_data1 = self.snap_root / "e2e_snap" / "data1"
        self._build_single_sstable_db(
            db_data,
            [
                ("k股票代码:000001".encode("utf-8"), b'{"code":"000001"}'),
                ("k日k:000001:20250603".encode("utf-8"), self._sample_kdaily_msgpack("000001", 10.2, 10.0)),
                ("k日k:510300:20250603".encode("utf-8"), self._sample_kdaily_msgpack("510300", 4.0, 3.9)),
            ],
        )
        self._build_single_sstable_db(
            db_data1,
            [
                ("k分钟k:000001:20250603093000".encode("utf-8"), b"m1"),
                ("k复权:000001".encode("utf-8"), _pack_msgpack_test({"20250602": {"div": 0.2, "give": 0.0, "trans": 0.0, "mult": 1.0, "cum": 1.5}})),
                ("k日k:600519:20250603".encode("utf-8"), self._sample_kdaily_msgpack("600519", 1600.0, 1590.0)),
                ("k资金流:000001:20250603".encode("utf-8"), b"cap"),
            ],
        )

        orig_root = config.WAREHOUSE_DIR
        orig_token = config.WEBUI_TOKEN
        orig_enabled = config.WAREHOUSE_ENABLED
        orig_inv = wh_tasks.inventory
        orig_bulk = wh_tasks.bulk_import
        orig_bf = wh_tasks.daily_backfill
        orig_code_fn = wh_tasks.is_supported_code
        orig_root_fn = wh_tasks.warehouse_root
        orig_avail = wh_tasks.availability
        orig_refresh = wh_tasks.refresh_views

        wh_engine.reset_engine()
        config.WAREHOUSE_DIR = self.wh_root
        config.WEBUI_TOKEN = "test-secret-token"
        config.WAREHOUSE_ENABLED = True
        root_app._wire_warehouse_tasks()
        root_app.invalidate_warehouse_caches()

        server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
        port = server.server_address[1]
        t = threading.Thread(target=server.serve_forever, daemon=True)
        t.start()
        try:
            exit_code = cli_sync.main([
                "--snapshot-root", str(self.snap_root / "e2e_snap"),
                "--snapshot-id", "snap_std_daily_e2e",
                "--nas-url", f"http://127.0.0.1:{port}",
                "--token", "test-secret-token",
                "--checkpoint-dir", str(self.ckpt_dir / "e2e_std"),
                "--standard-daily-mode", "sync-and-exclude",
                "--batch-size", "2",
            ])
            self.assertEqual(exit_code, 0)

            eng = wh_engine.get_engine()
            eng.refresh_views()
            rows = eng.run_sql(
                "SELECT code, close, prev_close, adj_factor, close_fq FROM v_daily ORDER BY code"
            )["rows"]
            self.assertEqual(len(rows), 2)
            self.assertEqual(rows[0][0], "000001")
            self.assertAlmostEqual(float(rows[0][1]), 10.2)
            self.assertAlmostEqual(float(rows[0][2]), 10.0)
            self.assertAlmostEqual(float(rows[0][3]), 1.5)
            self.assertAlmostEqual(float(rows[0][4]), 10.2 * 1.5)
            self.assertEqual(rows[1][0], "600519")
            # 原库中绝无 k日k:（已排除），默认亦排除 k资金流:，仅含 k分钟k/k复权/k股票代码
            kdaily_raw_cnt = eng.run_sql("SELECT COUNT(*) FROM v_leveldb_raw WHERE key_utf8 LIKE 'k日k:%'")["rows"][0][0]
            self.assertEqual(kdaily_raw_cnt, 0)
            kcap_raw_cnt = eng.run_sql("SELECT COUNT(*) FROM v_leveldb_raw WHERE key_utf8 LIKE 'k资金流:%'")["rows"][0][0]
            self.assertEqual(kcap_raw_cnt, 0)

            # 显式传 --include-capital-flow 时，允许重新纳入 k资金流:
            exit_code_inc = cli_sync.main([
                "--snapshot-root", str(self.snap_root / "e2e_snap"),
                "--snapshot-id", "snap_std_daily_e2e_cap",
                "--nas-url", f"http://127.0.0.1:{port}",
                "--token", "test-secret-token",
                "--checkpoint-dir", str(self.ckpt_dir / "e2e_std_cap"),
                "--standard-daily-mode", "sync-and-exclude",
                "--include-capital-flow",
                "--batch-size", "2",
            ])
            self.assertEqual(exit_code_inc, 0)
            eng.refresh_views()
            kcap_inc_cnt = eng.run_sql(
                "SELECT COUNT(*) FROM v_leveldb_raw WHERE snapshot_id = 'snap_std_daily_e2e_cap' AND key_utf8 LIKE 'k资金流:%'"
            )["rows"][0][0]
            self.assertEqual(kcap_inc_cnt, 1)

            # 校验 POST /api/warehouse/daily-backfill/consolidate 路由正常响应
            import urllib.request
            req = urllib.request.Request(
                f"http://127.0.0.1:{port}/api/warehouse/daily-backfill/consolidate",
                data=json.dumps({"prune_namespaces": ["k日k"]}).encode("utf-8"),
                headers={
                    "X-StockDB-Token": "test-secret-token",
                    "Content-Type": "application/json",
                },
                method="POST",
            )
            with urllib.request.urlopen(req, timeout=15) as resp:
                cons = json.loads(resp.read().decode("utf-8"))
            self.assertTrue(cons["ok"] and cons["passed"])
            self.assertEqual(cons["prune"]["removed_rows"], 0)
        finally:
            server.shutdown()
            server.server_close()
            wh_engine.reset_engine()
            root_app.invalidate_warehouse_caches()
            config.WAREHOUSE_DIR = orig_root
            config.WEBUI_TOKEN = orig_token
            config.WAREHOUSE_ENABLED = orig_enabled
            wh_tasks.inventory = orig_inv
            wh_tasks.bulk_import = orig_bulk
            wh_tasks.daily_backfill = orig_bf
            wh_tasks.is_supported_code = orig_code_fn
            wh_tasks.warehouse_root = orig_root_fn
            wh_tasks.availability = orig_avail
            wh_tasks.refresh_views = orig_refresh


class MinuteBackfillTests(unittest.TestCase):
    """Stage 1 1m 分钟线向量化回填、Fallback 解码、严格核对与 k分钟k 安全裁剪测试。"""

    def _make_minute_bar(
        self,
        code: str,
        ts14: str,
        *,
        open_p: float = 10.0,
        high_p: float = 10.5,
        low_p: float = 9.8,
        close_p: float = 10.2,
        volume: float | None = 1200.0,
        amount: float | None = 12240.0,
        extra: dict | None = None,
    ) -> dict:
        payload = {
            "code": code,
            "date": ts14,
            "open": open_p,
            "high": high_p,
            "low": low_p,
            "close": close_p,
            "volume": volume,
            "amount": amount,
        }
        if extra:
            payload.update(extra)
        return payload

    def _ingest_kv_items(
        self,
        root: Path,
        *,
        snapshot_id: str,
        source_db: str,
        items: list[tuple[bytes, bytes]],
        batch_size: int = 500,
        fallback_mutations: dict[bytes, tuple[str | None, str | None]] | None = None,
    ) -> None:
        import hashlib
        import duckdb

        sorted_items = sorted(items, key=lambda kv: kv[0])
        stage_dir = root / ".test_stage" / snapshot_id / source_db
        stage_dir.mkdir(parents=True, exist_ok=True)
        total_batches = max(1, (len(sorted_items) + batch_size - 1) // batch_size)

        for idx in range(total_batches):
            chunk = sorted_items[idx * batch_size : (idx + 1) * batch_size]
            bid = f"b{idx:06d}"
            start_seq = idx * batch_size
            stage_pq = stage_dir / f"{bid}.parquet"
            meta = bulk_import.write_leveldb_batch_parquet(
                stage_pq,
                snapshot_id=snapshot_id,
                source_db=source_db,
                batch_id=bid,
                start_seq=start_seq,
                items=chunk,
            )

            if fallback_mutations:
                chunk_keys = {rk for rk, _ in chunk}
                hit_keys = [rk for rk in fallback_mutations if rk in chunk_keys]
                if hit_keys:
                    con = duckdb.connect(":memory:")
                    try:
                        con.execute(
                            "CREATE TEMP TABLE _mut (rk BLOB, new_kind VARCHAR, new_vt VARCHAR)"
                        )
                        for rk in hit_keys:
                            nk, nvt = fallback_mutations[rk]
                            con.execute(
                                "INSERT INTO _mut VALUES (?, ?, ?)", [rk, nk, nvt]
                            )
                        mut_pq = stage_dir / f"{bid}_mut.parquet"
                        src_sql = str(stage_pq).replace("\\", "/").replace("'", "''")
                        dst_sql = str(mut_pq).replace("\\", "/").replace("'", "''")
                        con.execute(
                            f"""
                            COPY (
                                SELECT
                                    s.snapshot_id,
                                    s.source_db,
                                    s.batch_id,
                                    s.seq_no,
                                    s.raw_key,
                                    s.raw_value,
                                    s.key_sha256,
                                    s.value_sha256,
                                    s.key_len,
                                    s.value_len,
                                    s.key_utf8,
                                    s.key_namespace,
                                    CASE WHEN m.rk IS NOT NULL THEN m.new_kind ELSE s.value_kind END AS value_kind,
                                    CASE WHEN m.rk IS NOT NULL THEN m.new_vt ELSE s.value_text END AS value_text
                                FROM read_parquet('{src_sql}') s
                                LEFT JOIN _mut m ON s.raw_key = m.rk
                                ORDER BY s.seq_no ASC
                            ) TO '{dst_sql}' (FORMAT PARQUET, COMPRESSION ZSTD)
                            """
                        )
                    finally:
                        con.close()
                    os.replace(mut_pq, stage_pq)

            payload_bytes = stage_pq.read_bytes()
            sha = hashlib.sha256(payload_bytes).hexdigest()
            bulk_import.ingest_parquet_batch(
                root,
                payload_bytes,
                snapshot_id=snapshot_id,
                source_db=source_db,
                batch_id=bid,
                batch_index=idx,
                parquet_sha256=sha,
                expected_row_count=int(meta["row_count"]),
                expected_seq_start=int(meta["seq_start"]),
                expected_seq_end=int(meta["seq_end"]),
                expected_rows_digest=str(meta["rows_digest_sha256"]),
            )

        bulk_import.complete_bulk_import(
            root,
            snapshot_id=snapshot_id,
            source_db=source_db,
            expected_batches=total_batches,
            expected_total_rows=len(sorted_items),
        )
        shutil.rmtree(stage_dir, ignore_errors=True)

    def test_vectorized_value_text_and_raw_value_msgpack_fallback(self) -> None:
        import duckdb
        from storage.warehouse import layout, minute_backfill, sink

        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            long_note = "x" * 4500  # triggers length(value_text) >= 4096 fallback
            k_fast_msgpack = b"k\xe5\x88\x86\xe9\x92\x9fk:000001:20260105093100"
            k_fast_json = b"k\xe5\x88\x86\xe9\x92\x9fk:600519:20260105093100"
            k_fast_nan = b"k\xe5\x88\x86\xe9\x92\x9fk:000001:20260105093200"
            k_fb_null_vt = b"k\xe5\x88\x86\xe9\x92\x9fk:000001:20260105093300"
            k_fb_bad_kind = b"k\xe5\x88\x86\xe9\x92\x9fk:600519:20260105093200"
            k_fb_bad_json = b"k\xe5\x88\x86\xe9\x92\x9fk:000001:20260105093400"
            k_fb_trunc = b"k\xe5\x88\x86\xe9\x92\x9fk:300750:20260105093100"

            items = [
                (
                    k_fast_msgpack,
                    _pack_msgpack_test(
                        self._make_minute_bar("000001", "20260105093100", close_p=11.1)
                    ),
                ),
                (
                    k_fast_json,
                    json.dumps(
                        self._make_minute_bar("600519", "20260105093100", close_p=1500.5)
                    ).encode("utf-8"),
                ),
                (
                    k_fast_nan,
                    _pack_msgpack_test(
                        self._make_minute_bar(
                            "000001",
                            "20260105093200",
                            close_p=11.2,
                            volume=float("nan"),
                            amount=float("inf"),
                        )
                    ),
                ),
                (
                    k_fb_null_vt,
                    _pack_msgpack_test(
                        self._make_minute_bar("000001", "20260105093300", close_p=11.3)
                    ),
                ),
                (
                    k_fb_bad_kind,
                    _pack_msgpack_test(
                        self._make_minute_bar("600519", "20260105093200", close_p=1501.0)
                    ),
                ),
                (
                    k_fb_bad_json,
                    _pack_msgpack_test(
                        self._make_minute_bar("000001", "20260105093400", close_p=11.4)
                    ),
                ),
                (
                    k_fb_trunc,
                    _pack_msgpack_test(
                        self._make_minute_bar(
                            "300750",
                            "20260105093100",
                            close_p=210.0,
                            extra={"note": long_note},
                        )
                    ),
                ),
            ]
            mutations = {
                k_fb_null_vt: ("msgpack", None),
                k_fb_bad_kind: ("binary", None),
                k_fb_bad_json: ("json", "{broken_json"),
            }
            self._ingest_kv_items(
                root,
                snapshot_id="snap_20260105",
                source_db="data1",
                items=items,
                fallback_mutations=mutations,
            )

            probe = minute_backfill.probe_minute_completeness(root)
            self.assertEqual(probe["total_rows"], 7)
            self.assertEqual(probe["fast_path_rows"], 3)
            self.assertEqual(probe["fallback_rows"], 4)
            self.assertEqual(probe["null_value_text_rows"], 2)
            self.assertEqual(probe["non_json_kind_rows"], 1)
            self.assertEqual(probe["invalid_json_rows"], 1)
            self.assertEqual(probe["truncated_value_text_rows"], 1)

            res = minute_backfill.backfill_minute_from_leveldb_warehouse(
                root, code_predicate=is_supported_a_share_code
            )
            self.assertTrue(res["ok"])
            self.assertEqual(res["written_rows"], 7)
            self.assertEqual(res["written_files"], 2)  # sh & sz on 20260105
            self.assertEqual(wh_catalog.get_watermark(root, "minute"), "20260105")

            sz_pq = layout.minute_partition(root, "1m", "20260105", "sz")
            sh_pq = layout.minute_partition(root, "1m", "20260105", "sh")
            self.assertTrue(sz_pq.is_file())
            self.assertTrue(sh_pq.is_file())

            con = duckdb.connect(":memory:")
            try:
                sz_cols = [
                    r[0]
                    for r in con.execute(
                        f"DESCRIBE SELECT * FROM read_parquet('{sz_pq.as_posix()}', hive_partitioning=false)"
                    ).fetchall()
                ]
                self.assertEqual(sz_cols, [c[0] for c in sink._MINUTE_COLUMNS])
                comp = con.execute(
                    f"SELECT DISTINCT compression FROM parquet_metadata('{sz_pq.as_posix()}')"
                ).fetchall()
                self.assertEqual(comp, [("ZSTD",)])

                sz_rows = con.execute(
                    f"SELECT code, strftime(datetime, '%Y%m%d%H%M%S'), close, volume, amount "
                    f"FROM read_parquet('{sz_pq.as_posix()}')"
                ).fetchall()
                # Strictly sorted by (code, datetime)
                self.assertEqual(
                    [(r[0], r[1]) for r in sz_rows],
                    [
                        ("000001", "20260105093100"),
                        ("000001", "20260105093200"),
                        ("000001", "20260105093300"),
                        ("000001", "20260105093400"),
                        ("300750", "20260105093100"),
                    ],
                )
                # NaN/Inf row kept with sanitized NULL volume/amount
                nan_row = sz_rows[1]
                self.assertEqual(nan_row[2], 11.2)
                self.assertIsNone(nan_row[3])
                self.assertIsNone(nan_row[4])
            finally:
                con.close()

            verify = minute_backfill.verify_minute_backfill(
                root, code_predicate=is_supported_a_share_code
            )
            self.assertTrue(verify["ok"], verify)
            self.assertEqual(verify["expected_rows"], 7)
            self.assertEqual(verify["actual_rows"], 7)
            self.assertEqual(verify["value_mismatches"], 0)

    def test_a_share_code_predicate_filters_etf_and_bj_and_updates_v_minute(self) -> None:
        from storage.warehouse import minute_backfill

        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            bars = [
                ("000001", "20260105093100", 10.1),  # SZ main A-share -> keep
                ("300750", "20260105093100", 200.2),  # SZ ChiNext A-share -> keep
                ("600519", "20260105093100", 1510.3),  # SH main A-share -> keep
                ("688981", "20260105093100", 88.4),  # SH STAR A-share -> keep
                ("159915", "20260105093100", 2.5),  # SZ ETF -> exclude
                ("510300", "20260105093100", 4.1),  # SH ETF -> exclude
                ("830799", "20260105093100", 15.0),  # BJ -> exclude
                ("430047", "20260105093100", 12.0),  # BJ -> exclude
                ("920001", "20260105093100", 9.0),  # BJ -> exclude
                ("00700", "20260105093100", 380.0),  # HK -> exclude
            ]
            items = [
                (
                    f"k分钟k:{code}:{ts14}".encode("utf-8"),
                    _pack_msgpack_test(self._make_minute_bar(code, ts14, close_p=cp)),
                )
                for code, ts14, cp in bars
            ]
            self._ingest_kv_items(
                root, snapshot_id="snap_filter", source_db="data1", items=items
            )

            res = minute_backfill.backfill_minute_from_leveldb_warehouse(
                root, code_predicate=is_supported_a_share_code
            )
            self.assertTrue(res["ok"])
            self.assertEqual(res["written_rows"], 4)

            bj_dirs = list((root / "facts" / "minute" / "period=1m").rglob("market=bj"))
            self.assertEqual(bj_dirs, [])

            verify = minute_backfill.verify_minute_backfill(
                root, code_predicate=is_supported_a_share_code
            )
            self.assertTrue(verify["ok"], verify)
            self.assertEqual(verify["expected_rows"], 4)
            self.assertEqual(verify["actual_rows"], 4)
            self.assertEqual(verify["disallowed_code_count"], 0)

            # End-to-end check against @storage's WarehouseEngine v_minute view
            eng = wh_engine.WarehouseEngine(root)
            try:
                eng.refresh_views()
                rows = eng.run_sql(
                    "SELECT code, market, close FROM v_minute ORDER BY code ASC"
                )["rows"]
                self.assertEqual(
                    rows,
                    [
                        ["000001", "sz", 10.1],
                        ["300750", "sz", 200.2],
                        ["600519", "sh", 1510.3],
                        ["688981", "sh", 88.4],
                    ],
                )
            finally:
                eng.close()

    def test_cross_source_priority_data1_over_data_over_mydb(self) -> None:
        import duckdb
        from storage.warehouse import layout, minute_backfill

        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            # Overlap key1: mydb (100) vs data (200) vs data1 (300) -> data1 wins
            # Overlap key2: data1 snap_old (400) vs data1 snap_new (500) -> snap_new wins
            # Overlap key3: mydb (600) vs data (700) -> data wins
            # Unique key4: mydb only (800) -> mydb kept
            mydb_items = [
                (
                    b"k\xe5\x88\x86\xe9\x92\x9fk:000001:20260105093100",
                    _pack_msgpack_test(self._make_minute_bar("000001", "20260105093100", close_p=100.0)),
                ),
                (
                    b"k\xe5\x88\x86\xe9\x92\x9fk:000001:20260105093300",
                    _pack_msgpack_test(self._make_minute_bar("000001", "20260105093300", close_p=600.0)),
                ),
                (
                    b"k\xe5\x88\x86\xe9\x92\x9fk:000001:20260105093400",
                    _pack_msgpack_test(self._make_minute_bar("000001", "20260105093400", close_p=800.0)),
                ),
            ]
            data_items = [
                (
                    b"k\xe5\x88\x86\xe9\x92\x9fk:000001:20260105093100",
                    _pack_msgpack_test(self._make_minute_bar("000001", "20260105093100", close_p=200.0)),
                ),
                (
                    b"k\xe5\x88\x86\xe9\x92\x9fk:000001:20260105093300",
                    _pack_msgpack_test(self._make_minute_bar("000001", "20260105093300", close_p=700.0)),
                ),
            ]
            data1_snap1 = [
                (
                    b"k\xe5\x88\x86\xe9\x92\x9fk:000001:20260105093100",
                    _pack_msgpack_test(self._make_minute_bar("000001", "20260105093100", close_p=300.0)),
                ),
                (
                    b"k\xe5\x88\x86\xe9\x92\x9fk:000001:20260105093200",
                    _pack_msgpack_test(self._make_minute_bar("000001", "20260105093200", close_p=400.0)),
                ),
            ]
            data1_snap2 = [
                (
                    b"k\xe5\x88\x86\xe9\x92\x9fk:000001:20260105093200",
                    _pack_msgpack_test(self._make_minute_bar("000001", "20260105093200", close_p=500.0)),
                ),
            ]
            self._ingest_kv_items(root, snapshot_id="snap_01", source_db="mydb", items=mydb_items)
            self._ingest_kv_items(root, snapshot_id="snap_01", source_db="data", items=data_items)
            self._ingest_kv_items(
                root,
                snapshot_id="snap_01",
                source_db="data1",
                items=data1_snap1,
                # Also verify fallback data1 wins over fast-path data!
                fallback_mutations={b"k\xe5\x88\x86\xe9\x92\x9fk:000001:20260105093100": ("msgpack", None)},
            )
            self._ingest_kv_items(root, snapshot_id="snap_02", source_db="data1", items=data1_snap2)

            res = minute_backfill.backfill_minute_from_leveldb_warehouse(
                root, code_predicate=is_supported_a_share_code
            )
            self.assertEqual(res["written_rows"], 4)

            sz_pq = layout.minute_partition(root, "1m", "20260105", "sz")
            con = duckdb.connect(":memory:")
            try:
                got = con.execute(
                    f"SELECT strftime(datetime, '%H%M%S'), close FROM read_parquet('{sz_pq.as_posix()}') ORDER BY datetime"
                ).fetchall()
                self.assertEqual(
                    got,
                    [("093100", 300.0), ("093200", 500.0), ("093300", 700.0), ("093400", 800.0)],
                )
            finally:
                con.close()

            verify = minute_backfill.verify_minute_backfill(
                root, code_predicate=is_supported_a_share_code
            )
            self.assertTrue(verify["ok"], verify)
            self.assertEqual(verify["overlap_checked"], 3)
            self.assertGreaterEqual(verify["data1_over_data_count"], 1)
            self.assertEqual(verify["overlap_mismatches"], 0)

    def test_monthly_checkpoint_resume_idempotent_rerun_and_watermark(self) -> None:
        from storage.warehouse import minute_backfill

        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            cp_path = root / "custom_cp.json"
            items = [
                (
                    f"k分钟k:000001:{ts}".encode("utf-8"),
                    _pack_msgpack_test(self._make_minute_bar("000001", ts, close_p=px)),
                )
                for ts, px in [
                    ("20260105093100", 10.0),
                    ("20260105093200", 10.1),
                    ("20260203093100", 11.0),
                    ("20260302093100", 12.0),
                ]
            ]
            self._ingest_kv_items(root, snapshot_id="snap_m", source_db="data1", items=items)

            def _boom_after_jan(info: dict) -> None:
                if info["month"] == "202601":
                    raise RuntimeError("simulated crash after 202601")

            with self.assertRaisesRegex(RuntimeError, "simulated crash"):
                minute_backfill.backfill_minute_from_leveldb_warehouse(
                    root,
                    code_predicate=is_supported_a_share_code,
                    checkpoint_path=cp_path,
                    on_month_complete=_boom_after_jan,
                )

            cp_data = json.loads(cp_path.read_text(encoding="utf-8"))
            self.assertEqual(cp_data["completed_months"], ["202601"])
            self.assertIsNone(wh_catalog.get_watermark(root, "minute"))

            resumed = minute_backfill.backfill_minute_from_leveldb_warehouse(
                root,
                code_predicate=is_supported_a_share_code,
                checkpoint_path=cp_path,
                resume=True,
            )
            self.assertEqual(resumed["months_skipped"], ["202601"])
            self.assertEqual(resumed["months_written"], ["202602", "202603"])
            self.assertEqual(wh_catalog.get_watermark(root, "minute"), "20260302")

            # Idempotent single-month overwrite rerun
            rerun = minute_backfill.backfill_minute_from_leveldb_warehouse(
                root,
                months=["202601"],
                code_predicate=is_supported_a_share_code,
                checkpoint_path=cp_path,
                overwrite_existing=True,
                advance_watermark=False,
            )
            self.assertEqual(rerun["months_written"], ["202601"])
            self.assertEqual(rerun["written_rows"], 2)
            self.assertFalse(rerun["watermark_updated"])
            self.assertEqual(wh_catalog.get_watermark(root, "minute"), "20260302")

            verify = minute_backfill.verify_minute_backfill(
                root, code_predicate=is_supported_a_share_code
            )
            self.assertTrue(verify["ok"], verify)
            self.assertEqual(verify["actual_rows"], 4)
            self.assertEqual(verify["duplicate_key_count"], 0)

    def test_verify_tamper_detection_and_prune_refusal(self) -> None:
        import duckdb
        from storage.warehouse import layout, minute_backfill

        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            items = [
                (
                    b"k\xe5\x88\x86\xe9\x92\x9fk:000001:20260105093100",
                    _pack_msgpack_test(self._make_minute_bar("000001", "20260105093100", close_p=10.5)),
                ),
                (
                    b"k\xe5\x88\x86\xe9\x92\x9fk:000001:20260105093200",
                    _pack_msgpack_test(self._make_minute_bar("000001", "20260105093200", close_p=10.6)),
                ),
                (
                    b"k\xe8\xb5\x84\xe9\x87\x91\xe6\xb5\x81:000001:20260105",
                    _pack_msgpack_test({"date": "20260105", "net": 1234.5}),
                ),
            ]
            self._ingest_kv_items(root, snapshot_id="snap_t", source_db="data1", items=items)

            minute_backfill.backfill_minute_from_leveldb_warehouse(
                root, code_predicate=is_supported_a_share_code
            )
            sz_pq = layout.minute_partition(root, "1m", "20260105", "sz")
            clean_bytes = sz_pq.read_bytes()

            # Case 1: Tamper close value -> value_mismatches > 0, refuse prune
            con = duckdb.connect(":memory:")
            try:
                tmp_tamper = root / "tamper.parquet"
                con.execute(
                    f"COPY (SELECT code, date, datetime, open, high, low, "
                    f"CASE WHEN close = 10.5 THEN 999.0 ELSE close END AS close, volume, amount "
                    f"FROM read_parquet('{sz_pq.as_posix()}')) TO '{tmp_tamper.as_posix()}' "
                    f"(FORMAT PARQUET, COMPRESSION ZSTD)"
                )
            finally:
                con.close()
            os.replace(tmp_tamper, sz_pq)

            bad_val = minute_backfill.verify_minute_backfill(
                root, code_predicate=is_supported_a_share_code
            )
            self.assertFalse(bad_val["ok"])
            self.assertGreater(bad_val["value_mismatches"], 0)
            with self.assertRaisesRegex(RuntimeError, "拒绝裁剪"):
                minute_backfill.consolidate_and_prune_minute(
                    root,
                    code_predicate=is_supported_a_share_code,
                    run_backfill=False,
                    prune=True,
                )

            # Case 2: Duplicate key row in parquet -> duplicate_key_count > 0, refuse prune
            sz_pq.write_bytes(clean_bytes)
            con = duckdb.connect(":memory:")
            try:
                tmp_dup = root / "dup.parquet"
                con.execute(
                    f"COPY ((SELECT * FROM read_parquet('{sz_pq.as_posix()}')) "
                    f"UNION ALL (SELECT * FROM read_parquet('{sz_pq.as_posix()}') LIMIT 1)) "
                    f"TO '{tmp_dup.as_posix()}' (FORMAT PARQUET, COMPRESSION ZSTD)"
                )
            finally:
                con.close()
            os.replace(tmp_dup, sz_pq)
            bad_dup = minute_backfill.verify_minute_backfill(
                root, code_predicate=is_supported_a_share_code
            )
            self.assertFalse(bad_dup["ok"])
            self.assertGreater(bad_dup["duplicate_key_count"], 0)
            self.assertGreater(bad_dup["actual_rows"], bad_dup["expected_rows"])

            # Case 3: Missing row in parquet -> missing_key_count > 0
            sz_pq.write_bytes(clean_bytes)
            con = duckdb.connect(":memory:")
            try:
                tmp_drop = root / "drop.parquet"
                con.execute(
                    f"COPY (SELECT * FROM read_parquet('{sz_pq.as_posix()}') LIMIT 1) "
                    f"TO '{tmp_drop.as_posix()}' (FORMAT PARQUET, COMPRESSION ZSTD)"
                )
            finally:
                con.close()
            os.replace(tmp_drop, sz_pq)
            bad_miss = minute_backfill.verify_minute_backfill(
                root, code_predicate=is_supported_a_share_code
            )
            self.assertFalse(bad_miss["ok"])
            self.assertEqual(bad_miss["missing_key_count"], 1)

            # Confirm k分钟k is still completely intact in v_leveldb_raw
            con = duckdb.connect(":memory:")
            try:
                bulk_import.ensure_leveldb_views(con, root)
                km_cnt = con.execute(
                    "SELECT COUNT(*) FROM v_leveldb_raw WHERE key_namespace = 'k分钟k'"
                ).fetchone()[0]
                self.assertEqual(km_cnt, 2)
            finally:
                con.close()

    def test_consolidate_prunes_only_kminute_keeps_kcapital_and_daily_prune_safe(self) -> None:
        import duckdb
        from storage.warehouse import inventory as wh_inventory, minute_backfill

        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            items_d1 = [
                (
                    b"k\xe5\x88\x86\xe9\x92\x9fk:000001:20260105093100",
                    _pack_msgpack_test(self._make_minute_bar("000001", "20260105093100", close_p=10.5)),
                ),
                (
                    b"k\xe6\x97\xa5k:000001:20260105",
                    _pack_msgpack_test(
                        {
                            "code": "000001",
                            "date": "20260105",
                            "open": 10.0,
                            "high": 10.8,
                            "low": 9.9,
                            "close": 10.5,
                            "volume": 5000.0,
                            "amount": 52500.0,
                        }
                    ),
                ),
                (
                    b"k\xe8\xb5\x84\xe9\x87\x91\xe6\xb5\x81:000001:20260105",
                    _pack_msgpack_test({"date": "20260105", "net": 888.0}),
                ),
            ]
            items_d = [
                (
                    b"k\xe5\x88\x86\xe9\x92\x9fk:000001:20260105093100",
                    _pack_msgpack_test(self._make_minute_bar("000001", "20260105093100", close_p=9.9)),
                ),
                (
                    b"k\xe8\xb5\x84\xe9\x87\x91\xe6\xb5\x81:600519:20260105",
                    _pack_msgpack_test({"date": "20260105", "net": 999.0}),
                ),
            ]
            self._ingest_kv_items(root, snapshot_id="snap_c", source_db="data1", items=items_d1)
            self._ingest_kv_items(root, snapshot_id="snap_c", source_db="data", items=items_d)

            eng = wh_engine.WarehouseEngine(root)
            try:
                res = minute_backfill.consolidate_and_prune_minute(
                    root,
                    code_predicate=is_supported_a_share_code,
                    prune=True,
                    refresh_views_cb=eng.refresh_views,
                )
                self.assertTrue(res["ok"])
                self.assertTrue(res["passed"])
                self.assertTrue(res["verify"]["ok"])
                self.assertEqual(res["prune"]["removed_rows"], 2)
                self.assertEqual(len(res["bulk_import_verify"]), 2)
                self.assertTrue(all(v["passed"] for v in res["bulk_import_verify"]))

                eng.refresh_views()
                self.assertEqual(
                    eng.run_sql("SELECT code, close FROM v_minute")["rows"],
                    [["000001", 10.5]],
                )
            finally:
                eng.close()

            con = duckdb.connect(":memory:")
            try:
                bulk_import.ensure_leveldb_views(con, root)
                counts = dict(
                    con.execute(
                        "SELECT key_namespace, COUNT(*) FROM v_leveldb_raw GROUP BY 1"
                    ).fetchall()
                )
                self.assertEqual(counts.get("k分钟k", 0), 0)
                self.assertEqual(counts.get("k资金流", 0), 2)
                self.assertEqual(counts.get("k日k", 0), 1)
            finally:
                con.close()

            # Zero stray 'dedup_daily' event on minute consolidate & prune; .staging minute_* cleaned up
            self.assertEqual(list((root / ".staging").glob("minute_*")), [])
            inv_min = wh_inventory.get_warehouse_inventory(root)
            min_triggers = [h["trigger_kind"] for h in inv_min.get("changes") or []]
            self.assertIn("minute_backfill", min_triggers)
            self.assertIn("dedup_minute", min_triggers)
            self.assertNotIn("dedup_daily", min_triggers)

            # Existing daily prune_leveldb_namespace(namespaces=("k日k",)) continues working cleanly
            daily_prune = daily_backfill.prune_leveldb_namespace(root, namespaces=("k日k",))
            self.assertTrue(daily_prune["passed"])
            self.assertEqual(daily_prune["removed_rows"], 1)

            con = duckdb.connect(":memory:")
            try:
                bulk_import.ensure_leveldb_views(con, root)
                counts2 = dict(
                    con.execute(
                        "SELECT key_namespace, COUNT(*) FROM v_leveldb_raw GROUP BY 1"
                    ).fetchall()
                )
                self.assertEqual(counts2, {"k资金流": 2})
            finally:
                con.close()

            inv = wh_inventory.get_warehouse_inventory(root)
            ds_by_key = {item["key"]: item for item in inv.get("datasets") or []}
            self.assertIn("std:minute", ds_by_key)
            self.assertEqual(ds_by_key["std:minute"]["rows"], 1)
            self.assertFalse(any(k.endswith(":k分钟k") for k in ds_by_key))
            self.assertFalse(any(k.endswith(":k日k") for k in ds_by_key))
            self.assertIn("ldb:data1:k资金流", ds_by_key)
            self.assertIn("ldb:data:k资金流", ds_by_key)
            history_triggers = [h["trigger_kind"] for h in inv.get("changes") or []]
            self.assertIn("minute_backfill", history_triggers)
            self.assertIn("dedup_minute", history_triggers)
            self.assertIn("dedup_daily", history_triggers)
            highlight_keys = [h["key"] for h in inv.get("highlights") or []]
            self.assertIn("std:minute", highlight_keys)

    def test_multimonth_verify_narrowing_and_staging_cleanup(self) -> None:
        from storage.warehouse import layout, minute_backfill, sink

        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            d1_items = [
                (
                    f"k分钟k:000001:{ts}".encode("utf-8"),
                    _pack_msgpack_test(self._make_minute_bar("000001", ts, close_p=px)),
                )
                for ts, px in [
                    ("20260105093100", 10.1),
                    ("20260203093100", 11.2),
                    ("20260302093100", 12.3),
                ]
            ]
            d_items = [
                (
                    b"k\xe5\x88\x86\xe9\x92\x9fk:000001:20260203093100",
                    _pack_msgpack_test(self._make_minute_bar("000001", "20260203093100", close_p=9.9)),
                ),
                (
                    b"k\xe5\x88\x86\xe9\x92\x9fk:600519:20260302093100",
                    _pack_msgpack_test(self._make_minute_bar("600519", "20260302093100", close_p=1500.0)),
                ),
            ]
            other_items = [
                (
                    b"k\xe8\xb5\x84\xe9\x87\x91\xe6\xb5\x81:000001:20260105",
                    _pack_msgpack_test({"date": "20260105", "net": 42.0}),
                )
            ]
            self._ingest_kv_items(root, snapshot_id="snap_mm", source_db="data1", items=d1_items)
            self._ingest_kv_items(root, snapshot_id="snap_mm", source_db="data", items=d_items)
            self._ingest_kv_items(root, snapshot_id="snap_mm", source_db="mydb", items=other_items)

            res = minute_backfill.backfill_minute_from_leveldb_warehouse(
                root, code_predicate=is_supported_a_share_code
            )
            self.assertTrue(res["ok"])
            self.assertEqual(res["months_written"], ["202601", "202602", "202603"])
            self.assertEqual(res["written_rows"], 4)
            self.assertEqual(list((root / ".staging").glob("minute_*")), [])

            verify = minute_backfill.verify_minute_backfill(
                root,
                code_predicate=is_supported_a_share_code,
                sample_days=3,
                sample_per_day=50,
            )
            self.assertTrue(verify["ok"], verify)
            self.assertEqual(verify["expected_rows"], 4)
            self.assertEqual(verify["actual_rows"], 4)
            self.assertEqual(verify["sampled_days"], 3)
            self.assertEqual(verify["overlap_checked"], 1)
            self.assertEqual(verify["overlap_mismatches"], 0)
            self.assertEqual(verify["value_mismatches"], 0)
            self.assertEqual(list((root / ".staging").glob("minute_*")), [])

            # Extra stray month partition in facts/minute triggers extra_months & extra_key_count
            sink.write_minute(
                root,
                "20260401",
                [
                    {
                        "code": "600519",
                        "date": "2026-04-01",
                        "datetime": "2026-04-01 09:31:00",
                        "open": 1510.0,
                        "high": 1515.0,
                        "low": 1505.0,
                        "close": 1512.0,
                        "volume": 100.0,
                        "amount": 151200.0,
                    }
                ],
                code_predicate=is_supported_a_share_code,
                advance_watermark=False,
            )
            bad_extra = minute_backfill.verify_minute_backfill(
                root, code_predicate=is_supported_a_share_code
            )
            self.assertFalse(bad_extra["ok"])
            self.assertGreater(bad_extra["extra_key_count"], 0)
            self.assertTrue(any("多余月份分区" in e for e in bad_extra["errors"]))
            self.assertEqual(list((root / ".staging").glob("minute_*")), [])

            # Remove stray 20260401 and delete 20260203 partition -> missing month detected
            layout.minute_partition(root, "1m", "20260401", "sh").unlink(missing_ok=True)
            layout.minute_partition(root, "1m", "20260203", "sz").unlink(missing_ok=True)
            bad_missing = minute_backfill.verify_minute_backfill(
                root, code_predicate=is_supported_a_share_code
            )
            self.assertFalse(bad_missing["ok"])
            self.assertGreater(bad_missing["missing_key_count"], 0)
            self.assertTrue(any("缺少目标月份分区" in e for e in bad_missing["errors"]))
            self.assertEqual(list((root / ".staging").glob("minute_*")), [])

    def test_app_wiring_and_services_warehouse_tasks_minute_api(self) -> None:
        import app as stockdb_app
        import services.warehouse_tasks as wt
        from storage.warehouse import minute_backfill

        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            items = [
                (
                    b"k\xe5\x88\x86\xe9\x92\x9fk:000001:20260105093100",
                    _pack_msgpack_test(self._make_minute_bar("000001", "20260105093100", close_p=12.3)),
                ),
                (
                    b"k\xe5\x88\x86\xe9\x92\x9fk:159915:20260105093100",
                    _pack_msgpack_test(self._make_minute_bar("159915", "20260105093100", close_p=2.5)),
                ),
            ]
            self._ingest_kv_items(root, snapshot_id="snap_s", source_db="data1", items=items)

            orig_mb = wt.minute_backfill
            orig_root = wt.warehouse_root
            orig_refresh = wt.refresh_views
            try:
                stockdb_app._wire_warehouse_tasks()
                self.assertIs(wt.minute_backfill, minute_backfill)
                wt.warehouse_root = lambda: root
                wt.refresh_views = None

                run_res = wt.minute_backfill_run()
                self.assertTrue(run_res["ok"])
                self.assertEqual(run_res["written_rows"], 1)

                verify_res = wt.minute_backfill_verify()
                self.assertTrue(verify_res["ok"])
                self.assertEqual(verify_res["expected_rows"], 1)

                cons_res = wt.minute_backfill_consolidate(run_backfill=False, prune=True)
                self.assertTrue(cons_res["passed"])
                self.assertEqual(cons_res["prune"]["removed_rows"], 2)
            finally:
                wt.minute_backfill = orig_mb
                wt.warehouse_root = orig_root
                wt.refresh_views = orig_refresh


if __name__ == "__main__":
    unittest.main()
