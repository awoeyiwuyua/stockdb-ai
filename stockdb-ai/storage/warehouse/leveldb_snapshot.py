"""storage.warehouse.leveldb_snapshot — 只读 LevelDB 快照全量流式遍历器（纯 Python，零 C 扩展依赖）。

设计目标：
1. **严格只读、不写 LOCK/MANIFEST**：直接解析 `CURRENT`、`MANIFEST-*`（VersionEdit 回放）、
   `*.ldb` / `*.sst`（SSTable prefix-compressed block + Snappy/Zstd/无压缩 + CRC32C 校验）
   以及活跃 `*.log` WAL（WriteBatch）。适合直接读取 Windows 冻结快照或只读共享目录。
2. **全量按字典序流式归并**：通过 `heapq.merge` 对 WAL、L0 重叠文件及 L1+ 非重叠分层流执行
   `(user_key ASC, sequence DESC)` 归并，天然消解覆盖写与墓碑删除（Tombstone），仅占用常量级块缓存内存。
3. **字节级保真与断点续读**：完整保留任意二进制键/值（含空键 `b""`、空值 `b""`、含 `\\x00`/`\\xff` 字节）；
   支持 `iter_entries(start_after_key=...)`（利用 SSTable index block 二分跳过已同步分区）与
   `iter_batches(...)` 按行数/字节数切批，供 Windows → NAS 断点续传使用。
"""
from __future__ import annotations

import hashlib
import heapq
import json
import os
import re
import struct
from collections.abc import Iterator
from dataclasses import dataclass
from pathlib import Path

try:
    from compression import zstd as _zstd  # Python 3.14 标准库
except ImportError:  # pragma: no cover
    _zstd = None


_SST_MAGIC = 0xDB4775248B80FB57
_SST_FOOTER_SIZE = 48
_BLOCK_TRAILER_SIZE = 5
_LOG_BLOCK_SIZE = 32768
_LOG_HEADER_SIZE = 7

_COMPRESS_NONE = 0
_COMPRESS_SNAPPY = 1
_COMPRESS_ZSTD = 2

_TYPE_DELETION = 0
_TYPE_VALUE = 1

_LOG_ZERO = 0
_LOG_FULL = 1
_LOG_FIRST = 2
_LOG_MIDDLE = 3
_LOG_LAST = 4

# VersionEdit tags (Google LevelDB db/version_edit.cc)
_TAG_COMPARATOR = 1
_TAG_LOG_NUMBER = 2
_TAG_NEXT_FILE_NUMBER = 3
_TAG_LAST_SEQUENCE = 4
_TAG_COMPACT_POINTER = 5
_TAG_DELETED_FILE = 6
_TAG_NEW_FILE = 7
_TAG_PREV_LOG_NUMBER = 9

# 匹配 LevelDB 键中的 YYYYMMDD / YYYYMMDDHHMMSS（如 k日k:000001:20250102、k分钟k:...:20250102093000）
_DATE_IN_KEY_RE = re.compile(
    rb"(?:^|[:|\x1f_/])((?:19|20)\d{2}(?:0[1-9]|1[0-2])(?:0[1-9]|[12]\d|3[01]))(?:\d{6})?(?:$|[:|\x1f_/])"
)
_YYYYMMDD_RE = re.compile(r"^(?:19|20)\d{2}(?:0[1-9]|1[0-2])(?:0[1-9]|[12]\d|3[01])$")


def _normalize_date(value: str | None, label: str) -> bytes | None:
    if value is None or value == "":
        return None
    s = value.strip()
    if not _YYYYMMDD_RE.match(s):
        raise ValueError(f"{label} 必须为 YYYYMMDD 格式: {value!r}")
    return s.encode("ascii")


def _prefix_upper_bound(pfx: bytes) -> bytes | None:
    """返回匹配前缀 pfx 的字节串严格上界；若全为 0xFF 则无有限上界。"""
    trimmed = bytearray(pfx)
    while trimmed and trimmed[-1] == 0xFF:
        trimmed.pop()
    if not trimmed:
        return None
    trimmed[-1] += 1
    return bytes(trimmed)


def extract_key_date(raw_key: bytes) -> bytes | None:
    """从 LevelDB 原始键提取尾部日期（YYYYMMDD bytes）；无日期返回 None。"""
    matches = _DATE_IN_KEY_RE.findall(raw_key)
    return matches[-1] if matches else None


@dataclass(frozen=True)
class KeyScopeFilter:
    """LevelDB 导入与对账共享的键范围过滤器。"""

    since_date: str | None = None
    until_date: str | None = None
    include_prefixes: tuple[bytes, ...] = ()
    exclude_prefixes: tuple[bytes, ...] = ()
    include_undated: bool = True
    max_rows: int | None = None

    def __post_init__(self) -> None:
        since_b = _normalize_date(self.since_date, "since_date")
        until_b = _normalize_date(self.until_date, "until_date")
        if since_b and until_b and since_b > until_b:
            raise ValueError(f"since_date ({self.since_date}) 不能晚于 until_date ({self.until_date})")
        if self.max_rows is not None and int(self.max_rows) <= 0:
            raise ValueError(f"max_rows 必须大于 0: {self.max_rows}")
        object.__setattr__(self, "_since_b", since_b)
        object.__setattr__(self, "_until_b", until_b)

    def is_unfiltered(self) -> bool:
        return (
            self.since_date is None
            and self.until_date is None
            and not self.include_prefixes
            and not self.exclude_prefixes
            and self.include_undated
            and self.max_rows is None
        )

    def matches(self, raw_key: bytes) -> bool:
        if self.exclude_prefixes:
            for pfx in self.exclude_prefixes:
                if raw_key.startswith(pfx):
                    return False
        if self.include_prefixes:
            if not any(raw_key.startswith(pfx) for pfx in self.include_prefixes):
                return False
        since_b: bytes | None = object.__getattribute__(self, "_since_b")
        until_b: bytes | None = object.__getattribute__(self, "_until_b")
        if since_b is not None or until_b is not None:
            kd = extract_key_date(raw_key)
            if kd is None:
                return self.include_undated
            if since_b is not None and kd < since_b:
                return False
            if until_b is not None and kd > until_b:
                return False
        return True

    def to_dict(self) -> dict[str, object]:
        return {
            "since_date": self.since_date,
            "until_date": self.until_date,
            "include_prefixes_hex": [p.hex() for p in self.include_prefixes],
            "exclude_prefixes_hex": [p.hex() for p in self.exclude_prefixes],
            "include_undated": self.include_undated,
            "max_rows": self.max_rows,
        }

    def scope_digest(self) -> str:
        payload = json.dumps(self.to_dict(), sort_keys=True, separators=(",", ":"))
        return hashlib.sha256(payload.encode("ascii")).hexdigest()

    def sstable_range_bounds(self) -> tuple[bytes | None, bytes | None]:
        """当设置 include_prefixes 时返回可安全跳扫的闭开区间 `(min_key, upper_bound_key)`。"""
        if not self.include_prefixes:
            return None, None
        min_key = min(self.include_prefixes)
        upper: bytes | None = None
        for pfx in self.include_prefixes:
            cand = _prefix_upper_bound(pfx)
            if cand is None:
                return min_key, None
            if upper is None or cand > upper:
                upper = cand
        return min_key, upper

    def sstable_is_excluded(self, smallest_user_key: bytes, largest_user_key: bytes) -> bool:
        """当 SSTable 整个键区间 `[smallest, largest]` 完全落在某个排除前缀桶内时返回 True。"""
        if not self.exclude_prefixes:
            return False
        for pfx in self.exclude_prefixes:
            if smallest_user_key < pfx:
                continue
            upper = _prefix_upper_bound(pfx)
            if upper is None or largest_user_key < upper:
                return True
        return False


class LevelDBFormatError(ValueError):
    """LevelDB 快照目录、清单、WAL 或 SSTable 格式损坏或不完整。"""


def _build_crc32c_table() -> tuple[int, ...]:
    poly = 0x82F63B78
    table: list[int] = []
    for byte in range(256):
        crc = byte
        for _ in range(8):
            crc = (crc >> 1) ^ poly if (crc & 1) else (crc >> 1)
        table.append(crc)
    return tuple(table)


_CRC32C_TABLE = _build_crc32c_table()


def crc32c(data: bytes, init: int = 0) -> int:
    crc = init ^ 0xFFFFFFFF
    table = _CRC32C_TABLE
    for b in data:
        crc = table[(crc ^ b) & 0xFF] ^ (crc >> 8)
    return crc ^ 0xFFFFFFFF


def mask_crc(crc: int) -> int:
    return (((crc >> 15) | (crc << 17)) + 0xA282EAD8) & 0xFFFFFFFF


def unmask_crc(masked: int) -> int:
    rot = (masked - 0xA282EAD8) & 0xFFFFFFFF
    return ((rot >> 17) | (rot << 15)) & 0xFFFFFFFF


def _read_varint(buf: bytes | memoryview, pos: int) -> tuple[int, int]:
    shift = 0
    result = 0
    end = len(buf)
    while pos < end:
        b = buf[pos]
        pos += 1
        result |= (b & 0x7F) << shift
        if (b & 0x80) == 0:
            return result, pos
        shift += 7
        if shift > 63:
            raise LevelDBFormatError("varint 溢出")
    raise LevelDBFormatError("varint 截断")


def _read_slice(buf: bytes | memoryview, pos: int) -> tuple[bytes, int]:
    length, pos = _read_varint(buf, pos)
    end = pos + length
    if end > len(buf):
        raise LevelDBFormatError(f"slice 越界: need={length}, remain={len(buf) - pos}")
    return bytes(buf[pos:end]), end


def snappy_decompress(src: bytes | memoryview) -> bytes:
    """纯 Python Snappy 块解压器。"""
    n = len(src)
    expected_len, pos = _read_varint(src, 0)
    out = bytearray()
    while pos < n:
        tag = src[pos]
        pos += 1
        elem_type = tag & 0x03
        if elem_type == 0:
            len_m1 = tag >> 2
            if len_m1 < 60:
                lit_len = len_m1 + 1
            else:
                extra = len_m1 - 59
                if pos + extra > n:
                    raise LevelDBFormatError("Snappy literal 长度截断")
                lit_len = int.from_bytes(src[pos : pos + extra], "little") + 1
                pos += extra
            if pos + lit_len > n:
                raise LevelDBFormatError("Snappy literal 数据截断")
            out.extend(src[pos : pos + lit_len])
            pos += lit_len
        else:
            if elem_type == 1:
                copy_len = ((tag >> 2) & 0x07) + 4
                if pos >= n:
                    raise LevelDBFormatError("Snappy copy-1 截断")
                offset = ((tag & 0xE0) << 3) | src[pos]
                pos += 1
            elif elem_type == 2:
                copy_len = (tag >> 2) + 1
                if pos + 2 > n:
                    raise LevelDBFormatError("Snappy copy-2 截断")
                offset = int.from_bytes(src[pos : pos + 2], "little")
                pos += 2
            else:
                copy_len = (tag >> 2) + 1
                if pos + 4 > n:
                    raise LevelDBFormatError("Snappy copy-4 截断")
                offset = int.from_bytes(src[pos : pos + 4], "little")
                pos += 4
            if offset <= 0 or offset > len(out):
                raise LevelDBFormatError(f"Snappy 非法回溯偏移: offset={offset}, len={len(out)}")
            if offset >= copy_len:
                start = len(out) - offset
                out.extend(out[start : start + copy_len])
            else:
                # 重叠 RLE 拷贝：每次扩倍拷贝当前尾部
                while copy_len > 0:
                    chunk = min(offset, copy_len)
                    start = len(out) - offset
                    out.extend(out[start : start + chunk])
                    copy_len -= chunk
                    offset += chunk
    if len(out) != expected_len:
        raise LevelDBFormatError(f"Snappy 解压长度不匹配: got={len(out)}, expected={expected_len}")
    return bytes(out)


def _iter_log_records(path: Path, verify_checksums: bool = True) -> Iterator[bytes]:
    """读取 LevelDB 日志文件（MANIFEST-* / *.log）的完整逻辑记录。"""
    data = path.read_bytes()
    total = len(data)
    pos = 0
    fragment: bytearray | None = None

    while pos < total:
        block_offset = pos % _LOG_BLOCK_SIZE
        remain_in_block = _LOG_BLOCK_SIZE - block_offset
        if remain_in_block < _LOG_HEADER_SIZE:
            # 块尾不足 7 字节零填充，跳至下一 32KiB 块
            pos += remain_in_block
            continue
        if pos + _LOG_HEADER_SIZE > total:
            break
        masked_crc, length, rec_type = struct.unpack_from("<IHB", data, pos)
        if rec_type == _LOG_ZERO and length == 0 and masked_crc == 0:
            # 预分配空白尾部
            break
        payload_start = pos + _LOG_HEADER_SIZE
        payload_end = payload_start + length
        if payload_end > total or length > remain_in_block - _LOG_HEADER_SIZE:
            raise LevelDBFormatError(f"日志记录越界: {path} @ {pos}")
        payload = data[payload_start:payload_end]
        if verify_checksums:
            expected_crc = unmask_crc(masked_crc)
            actual_crc = crc32c(bytes((rec_type,)) + payload)
            if actual_crc != expected_crc:
                raise LevelDBFormatError(
                    f"日志记录 CRC32C 校验失败: {path} @ {pos} "
                    f"(expected={expected_crc:#x}, actual={actual_crc:#x})"
                )
        pos = payload_end

        if rec_type == _LOG_FULL:
            if fragment is not None:
                raise LevelDBFormatError(f"未闭合日志分片后出现 FULL 记录: {path}")
            yield payload
        elif rec_type == _LOG_FIRST:
            if fragment is not None:
                raise LevelDBFormatError(f"未闭合日志分片后出现 FIRST 记录: {path}")
            fragment = bytearray(payload)
        elif rec_type == _LOG_MIDDLE:
            if fragment is None:
                raise LevelDBFormatError(f"孤立 MIDDLE 日志分片: {path}")
            fragment.extend(payload)
        elif rec_type == _LOG_LAST:
            if fragment is None:
                raise LevelDBFormatError(f"孤立 LAST 日志分片: {path}")
            fragment.extend(payload)
            yield bytes(fragment)
            fragment = None
        else:
            raise LevelDBFormatError(f"未知日志记录类型 {rec_type}: {path}")

    if fragment is not None:
        raise LevelDBFormatError(f"日志尾部存在未闭合分片: {path}")


def _split_internal_key(ikey: bytes) -> tuple[bytes, int, int]:
    if len(ikey) < 8:
        raise LevelDBFormatError(f"InternalKey 长度不足 8 字节: {len(ikey)}")
    tag = struct.unpack_from("<Q", ikey, len(ikey) - 8)[0]
    return ikey[:-8], tag >> 8, tag & 0xFF


@dataclass(frozen=True)
class SSTableMeta:
    level: int
    file_number: int
    file_size: int
    smallest_ikey: bytes
    largest_ikey: bytes

    @property
    def smallest_user_key(self) -> bytes:
        return self.smallest_ikey[:-8]

    @property
    def largest_user_key(self) -> bytes:
        return self.largest_ikey[:-8]


@dataclass(frozen=True)
class ManifestState:
    comparator: str
    log_number: int
    prev_log_number: int
    next_file_number: int
    last_sequence: int
    files_by_level: dict[int, tuple[SSTableMeta, ...]]


def _parse_manifest(manifest_path: Path, verify_checksums: bool = True) -> ManifestState:
    comparator = "leveldb.BytewiseComparator"
    log_number = 0
    prev_log_number = 0
    next_file_number = 1
    last_sequence = 0
    live_files: dict[tuple[int, int], SSTableMeta] = {}

    for record in _iter_log_records(manifest_path, verify_checksums=verify_checksums):
        pos = 0
        n = len(record)
        while pos < n:
            tag, pos = _read_varint(record, pos)
            if tag == _TAG_COMPARATOR:
                comp_bytes, pos = _read_slice(record, pos)
                comparator = comp_bytes.decode("utf-8", errors="replace")
            elif tag == _TAG_LOG_NUMBER:
                log_number, pos = _read_varint(record, pos)
            elif tag == _TAG_PREV_LOG_NUMBER:
                prev_log_number, pos = _read_varint(record, pos)
            elif tag == _TAG_NEXT_FILE_NUMBER:
                next_file_number, pos = _read_varint(record, pos)
            elif tag == _TAG_LAST_SEQUENCE:
                last_sequence, pos = _read_varint(record, pos)
            elif tag == _TAG_COMPACT_POINTER:
                _, pos = _read_varint(record, pos)
                _, pos = _read_slice(record, pos)
            elif tag == _TAG_DELETED_FILE:
                level, pos = _read_varint(record, pos)
                file_num, pos = _read_varint(record, pos)
                live_files.pop((level, file_num), None)
            elif tag == _TAG_NEW_FILE:
                level, pos = _read_varint(record, pos)
                file_num, pos = _read_varint(record, pos)
                file_size, pos = _read_varint(record, pos)
                smallest, pos = _read_slice(record, pos)
                largest, pos = _read_slice(record, pos)
                live_files[(level, file_num)] = SSTableMeta(
                    level=level,
                    file_number=file_num,
                    file_size=file_size,
                    smallest_ikey=smallest,
                    largest_ikey=largest,
                )
            else:
                raise LevelDBFormatError(f"MANIFEST 未知 VersionEdit tag={tag}")

    grouped: dict[int, list[SSTableMeta]] = {}
    for meta in live_files.values():
        grouped.setdefault(meta.level, []).append(meta)
    frozen: dict[int, tuple[SSTableMeta, ...]] = {}
    for lvl, items in sorted(grouped.items()):
        if lvl == 0:
            # L0 允许键范围重叠，按 file_number 排序（实际排序仍靠 internal sequence）
            items.sort(key=lambda m: m.file_number)
        else:
            items.sort(key=lambda m: m.smallest_user_key)
        frozen[lvl] = tuple(items)

    return ManifestState(
        comparator=comparator,
        log_number=log_number,
        prev_log_number=prev_log_number,
        next_file_number=next_file_number,
        last_sequence=last_sequence,
        files_by_level=frozen,
    )


def _decode_block_entries(block: bytes, first_restart_idx: int = 0) -> Iterator[tuple[bytes, bytes]]:
    if len(block) < 4:
        raise LevelDBFormatError("SSTable block 过短")
    num_restarts = struct.unpack_from("<I", block, len(block) - 4)[0]
    restarts_offset = len(block) - 4 - num_restarts * 4
    if restarts_offset < 0 or num_restarts == 0:
        raise LevelDBFormatError(f"SSTable block restarts 非法: num_restarts={num_restarts}")

    start_pos = 0
    if first_restart_idx > 0 and first_restart_idx < num_restarts:
        start_pos = struct.unpack_from("<I", block, restarts_offset + first_restart_idx * 4)[0]

    pos = start_pos
    prev_key = b""
    mv = memoryview(block)
    while pos < restarts_offset:
        shared, pos = _read_varint(mv, pos)
        non_shared, pos = _read_varint(mv, pos)
        value_len, pos = _read_varint(mv, pos)
        if shared > len(prev_key):
            raise LevelDBFormatError("SSTable prefix shared 超出前驱键长度")
        key_end = pos + non_shared
        val_end = key_end + value_len
        if val_end > restarts_offset:
            raise LevelDBFormatError("SSTable 条目超出数据区边界")
        key = prev_key[:shared] + block[pos:key_end]
        value = block[key_end:val_end]
        prev_key = key
        pos = val_end
        yield key, value


def _find_block_restart_index(block: bytes, target_user_key: bytes, is_internal_key: bool) -> int:
    """二分定位 restart 点，返回最后一个保证 key <= target_user_key 的 restart 下标。"""
    num_restarts = struct.unpack_from("<I", block, len(block) - 4)[0]
    restarts_offset = len(block) - 4 - num_restarts * 4
    if num_restarts <= 1 or restarts_offset <= 0:
        return 0
    mv = memoryview(block)
    left = 0
    right = num_restarts - 1
    while left < right:
        mid = (left + right + 1) // 2
        offset = struct.unpack_from("<I", block, restarts_offset + mid * 4)[0]
        shared, pos = _read_varint(mv, offset)
        non_shared, pos = _read_varint(mv, pos)
        _, pos = _read_varint(mv, pos)
        if shared != 0:
            # 标准 LevelDB 每个 restart 点 shared 必为 0
            return 0
        key_bytes = block[pos : pos + non_shared]
        user_key = key_bytes[:-8] if (is_internal_key and len(key_bytes) >= 8) else key_bytes
        if user_key <= target_user_key:
            left = mid
        else:
            right = mid - 1
    return left


class _SSTableReader:
    def __init__(self, path: Path, verify_checksums: bool = True) -> None:
        self.path = path
        self.verify_checksums = verify_checksums
        self._size = path.stat().st_size
        if self._size < _SST_FOOTER_SIZE:
            raise LevelDBFormatError(f"SSTable 文件过小: {path}")

    def _read_block(self, fh, offset: int, size: int) -> bytes:
        fh.seek(offset)
        raw = fh.read(size + _BLOCK_TRAILER_SIZE)
        if len(raw) != size + _BLOCK_TRAILER_SIZE:
            raise LevelDBFormatError(f"SSTable 块读取截断: {self.path} @ {offset}")
        payload = raw[:size]
        comp_type = raw[size]
        masked_crc = struct.unpack_from("<I", raw, size + 1)[0]
        if self.verify_checksums:
            expected_crc = unmask_crc(masked_crc)
            actual_crc = crc32c(payload + bytes((comp_type,)))
            if actual_crc != expected_crc:
                raise LevelDBFormatError(
                    f"SSTable CRC32C 校验失败: {self.path} @ {offset} "
                    f"(expected={expected_crc:#x}, actual={actual_crc:#x})"
                )
        if comp_type == _COMPRESS_NONE:
            return payload
        if comp_type == _COMPRESS_SNAPPY:
            return snappy_decompress(payload)
        if comp_type == _COMPRESS_ZSTD:
            if _zstd is None:
                raise LevelDBFormatError("当前 Python 运行时不支持 Zstd 解压")
            return _zstd.decompress(payload)
        raise LevelDBFormatError(f"未知 SSTable 压缩算法 type={comp_type}: {self.path}")

    def iter_internal_entries(
        self, start_after_key: bytes | None = None
    ) -> Iterator[tuple[bytes, int, int, bytes]]:
        """按 InternalKey 顺序产出 `(user_key, neg_tag, val_type, value)`。"""
        with self.path.open("rb") as fh:
            fh.seek(self._size - _SST_FOOTER_SIZE)
            footer = fh.read(_SST_FOOTER_SIZE)
            magic = struct.unpack_from("<Q", footer, _SST_FOOTER_SIZE - 8)[0]
            if magic != _SST_MAGIC:
                raise LevelDBFormatError(f"SSTable magic 非法: {self.path} ({magic:#x})")
            pos = 0
            _, pos = _read_varint(footer, pos)  # metaindex offset
            _, pos = _read_varint(footer, pos)  # metaindex size
            idx_offset, pos = _read_varint(footer, pos)
            idx_size, pos = _read_varint(footer, pos)

            index_block = self._read_block(fh, idx_offset, idx_size)
            restart_idx = 0
            if start_after_key is not None:
                restart_idx = _find_block_restart_index(index_block, start_after_key, is_internal_key=True)

            for sep_ikey, handle_bytes in _decode_block_entries(index_block, first_restart_idx=restart_idx):
                sep_user_key, _, _ = _split_internal_key(sep_ikey)
                if start_after_key is not None and sep_user_key <= start_after_key:
                    continue
                hpos = 0
                blk_offset, hpos = _read_varint(handle_bytes, hpos)
                blk_size, _ = _read_varint(handle_bytes, hpos)
                data_block = self._read_block(fh, blk_offset, blk_size)
                data_restart = 0
                if start_after_key is not None:
                    data_restart = _find_block_restart_index(data_block, start_after_key, is_internal_key=True)
                for ikey, val in _decode_block_entries(data_block, first_restart_idx=data_restart):
                    user_key, seq, val_type = _split_internal_key(ikey)
                    if start_after_key is not None and user_key <= start_after_key:
                        continue
                    tag = (seq << 8) | val_type
                    yield user_key, -tag, val_type, val


def _resolve_table_path(db_dir: Path, file_number: int) -> Path:
    for ext in (".ldb", ".sst"):
        p = db_dir / f"{file_number:06d}{ext}"
        if p.is_file():
            return p
    raise LevelDBFormatError(f"MANIFEST 引用的 SSTable 缺失: {file_number:06d}.ldb/.sst @ {db_dir}")


def _parse_write_batch(record: bytes) -> Iterator[tuple[bytes, int, int, bytes]]:
    if len(record) < 12:
        raise LevelDBFormatError(f"WriteBatch 长度不足 12 字节: {len(record)}")
    seq, count = struct.unpack_from("<QI", record, 0)
    pos = 12
    n = len(record)
    for _ in range(count):
        if pos >= n:
            raise LevelDBFormatError("WriteBatch 记录数少于 header count")
        val_type = record[pos]
        pos += 1
        key, pos = _read_slice(record, pos)
        if val_type == _TYPE_VALUE:
            val, pos = _read_slice(record, pos)
        elif val_type == _TYPE_DELETION:
            val = b""
        else:
            raise LevelDBFormatError(f"WriteBatch 未知条目类型: {val_type}")
        tag = (seq << 8) | val_type
        yield key, -tag, val_type, val
        seq += 1
    if pos != n:
        raise LevelDBFormatError(f"WriteBatch 尾部存在未消费字节: remain={n - pos}")


class LevelDBSnapshot:
    """只读 LevelDB 快照句柄。"""

    def __init__(
        self,
        db_dir: str | Path,
        *,
        verify_checksums: bool = True,
    ) -> None:
        self.db_dir = Path(db_dir)
        self.verify_checksums = verify_checksums
        if not self.db_dir.is_dir():
            raise LevelDBFormatError(f"LevelDB 快照目录不存在: {self.db_dir}")
        current_path = self.db_dir / "CURRENT"
        if not current_path.is_file():
            raise LevelDBFormatError(f"缺少 CURRENT 文件: {self.db_dir}")
        manifest_name = current_path.read_text(encoding="utf-8").strip()
        if not manifest_name:
            raise LevelDBFormatError(f"CURRENT 文件为空: {current_path}")
        self.manifest_name = manifest_name
        self.manifest_path = self.db_dir / manifest_name
        if not self.manifest_path.is_file():
            raise LevelDBFormatError(f"CURRENT 指向的清单缺失: {self.manifest_path}")
        self.manifest = _parse_manifest(self.manifest_path, verify_checksums=verify_checksums)
        self._active_log_paths = self._discover_active_logs()

    def _discover_active_logs(self) -> tuple[Path, ...]:
        logs: list[tuple[int, Path]] = []
        for p in self.db_dir.iterdir():
            if not p.is_file() or p.suffix != ".log":
                continue
            stem = p.stem
            if not stem.isdigit():
                continue
            num = int(stem)
            if num >= self.manifest.log_number or (
                self.manifest.prev_log_number != 0 and num == self.manifest.prev_log_number
            ):
                logs.append((num, p))
        logs.sort(key=lambda item: item[0])
        return tuple(p for _, p in logs)

    def fingerprint(self) -> str:
        """计算覆盖 CURRENT、活跃 MANIFEST、活跃 WAL 与全部活跃 SSTable 的确定性 SHA-256 指纹。"""
        h = hashlib.sha256()
        h.update(self.manifest_name.encode("utf-8"))
        h.update(b"\0")
        h.update(hashlib.sha256(self.manifest_path.read_bytes()).digest())
        for log_p in self._active_log_paths:
            h.update(log_p.name.encode("utf-8"))
            h.update(hashlib.sha256(log_p.read_bytes()).digest())
        for lvl, files in sorted(self.manifest.files_by_level.items()):
            for meta in files:
                p = _resolve_table_path(self.db_dir, meta.file_number)
                st = p.stat()
                h.update(f"L{lvl}:{p.name}:{st.st_size}:".encode("utf-8"))
                h.update(meta.smallest_ikey)
                h.update(b":")
                h.update(meta.largest_ikey)
                h.update(b"\n")
        return h.hexdigest()

    def _iter_wal_entries(
        self, start_after_key: bytes | None = None
    ) -> Iterator[tuple[bytes, int, int, bytes]]:
        entries: list[tuple[bytes, int, int, bytes]] = []
        for log_p in self._active_log_paths:
            for rec in _iter_log_records(log_p, verify_checksums=self.verify_checksums):
                for item in _parse_write_batch(rec):
                    if start_after_key is not None and item[0] <= start_after_key:
                        continue
                    entries.append(item)
        entries.sort(key=lambda item: (item[0], item[1]))
        yield from entries

    @staticmethod
    def _sstable_skipped(
        meta: SSTableMeta,
        *,
        start_after_key: bytes | None,
        range_min: bytes | None,
        range_upper: bytes | None,
        key_filter: KeyScopeFilter | None,
    ) -> bool:
        if start_after_key is not None and meta.largest_user_key <= start_after_key:
            return True
        if range_min is not None and meta.largest_user_key < range_min:
            return True
        if range_upper is not None and meta.smallest_user_key >= range_upper:
            return True
        if key_filter is not None and key_filter.sstable_is_excluded(
            meta.smallest_user_key, meta.largest_user_key
        ):
            return True
        return False

    def _iter_level_stream(
        self,
        metas: tuple[SSTableMeta, ...],
        start_after_key: bytes | None,
        range_min: bytes | None = None,
        range_upper: bytes | None = None,
        key_filter: KeyScopeFilter | None = None,
    ) -> Iterator[tuple[bytes, int, int, bytes]]:
        for meta in metas:
            if self._sstable_skipped(
                meta,
                start_after_key=start_after_key,
                range_min=range_min,
                range_upper=range_upper,
                key_filter=key_filter,
            ):
                continue
            table_path = _resolve_table_path(self.db_dir, meta.file_number)
            reader = _SSTableReader(table_path, verify_checksums=self.verify_checksums)
            yield from reader.iter_internal_entries(start_after_key=start_after_key)

    def iter_entries(
        self,
        *,
        start_after_key: bytes | None = None,
        key_filter: KeyScopeFilter | None = None,
        max_rows_override: int | None = None,
    ) -> Iterator[tuple[bytes, bytes]]:
        """按严格字节序产出当前存活 `(raw_key, raw_value)`（支持断点续读与范围过滤）。

        `start_after_key=None` 从第一条（包含空键 `b""`）开始；
        传入 `bytes`（含 `b""`）时仅产出严格大于该键的条目。
        """
        row_cap = max_rows_override if max_rows_override is not None else (
            key_filter.max_rows if key_filter is not None else None
        )
        if row_cap is not None and row_cap <= 0:
            return

        range_min, range_upper = (
            key_filter.sstable_range_bounds() if key_filter is not None else (None, None)
        )
        streams: list[Iterator[tuple[bytes, int, int, bytes]]] = []
        if self._active_log_paths:
            streams.append(self._iter_wal_entries(start_after_key=start_after_key))
        for lvl, metas in sorted(self.manifest.files_by_level.items()):
            is_disjoint = lvl > 0 and all(
                metas[i].largest_user_key < metas[i + 1].smallest_user_key
                for i in range(len(metas) - 1)
            )
            if not is_disjoint:
                for meta in metas:
                    if self._sstable_skipped(
                        meta,
                        start_after_key=start_after_key,
                        range_min=range_min,
                        range_upper=range_upper,
                        key_filter=key_filter,
                    ):
                        continue
                    table_path = _resolve_table_path(self.db_dir, meta.file_number)
                    streams.append(
                        _SSTableReader(table_path, verify_checksums=self.verify_checksums)
                        .iter_internal_entries(start_after_key=start_after_key)
                    )
            else:
                streams.append(
                    self._iter_level_stream(
                        metas,
                        start_after_key=start_after_key,
                        range_min=range_min,
                        range_upper=range_upper,
                        key_filter=key_filter,
                    )
                )

        if not streams:
            return

        last_key: bytes | None = None
        emitted = 0
        for user_key, _neg_tag, val_type, value in heapq.merge(*streams, key=lambda x: (x[0], x[1])):
            if last_key is not None and user_key == last_key:
                continue
            last_key = user_key
            if val_type != _TYPE_VALUE:
                continue
            if key_filter is not None and not key_filter.matches(user_key):
                continue
            yield user_key, value
            emitted += 1
            if row_cap is not None and emitted >= row_cap:
                return

    def iter_batches(
        self,
        *,
        batch_size: int = 50000,
        max_batch_bytes: int = 32 * 1024 * 1024,
        start_after_key: bytes | None = None,
        start_seq: int = 0,
        start_batch_index: int = 0,
        key_filter: KeyScopeFilter | None = None,
        max_rows_override: int | None = None,
    ) -> Iterator[tuple[int, int, int, list[tuple[bytes, bytes]]]]:
        """分批产出 `(batch_index, seq_start, seq_end, items)`。"""
        if batch_size <= 0:
            raise ValueError("batch_size 必须大于 0")
        if max_batch_bytes <= 0:
            raise ValueError("max_batch_bytes 必须大于 0")

        batch: list[tuple[bytes, bytes]] = []
        batch_bytes = 0
        seq_start = start_seq
        seq_cursor = start_seq
        batch_idx = start_batch_index

        for k, v in self.iter_entries(
            start_after_key=start_after_key,
            key_filter=key_filter,
            max_rows_override=max_rows_override,
        ):
            item_bytes = len(k) + len(v)
            if batch and (len(batch) >= batch_size or batch_bytes + item_bytes > max_batch_bytes):
                yield batch_idx, seq_start, seq_cursor - 1, batch
                batch = []
                batch_bytes = 0
                batch_idx += 1
                seq_start = seq_cursor
            batch.append((k, v))
            batch_bytes += item_bytes
            seq_cursor += 1

        if batch:
            yield batch_idx, seq_start, seq_cursor - 1, batch

    def stats(self) -> dict[str, object]:
        total_sst = sum(len(v) for v in self.manifest.files_by_level.values())
        return {
            "db_dir": str(self.db_dir),
            "manifest": self.manifest_name,
            "comparator": self.manifest.comparator,
            "last_sequence": self.manifest.last_sequence,
            "active_logs": [p.name for p in self._active_log_paths],
            "levels": {str(k): len(v) for k, v in sorted(self.manifest.files_by_level.items())},
            "total_sstables": total_sst,
            "fingerprint": self.fingerprint(),
        }


def open_leveldb_snapshot(db_dir: str | Path, *, verify_checksums: bool = True) -> LevelDBSnapshot:
    return LevelDBSnapshot(db_dir, verify_checksums=verify_checksums)


def snapshot_fingerprint(db_dir: str | Path, *, verify_checksums: bool = True) -> str:
    return LevelDBSnapshot(db_dir, verify_checksums=verify_checksums).fingerprint()


def list_snapshot_dirs(parent_dir: str | Path) -> list[Path]:
    """发现父目录下所有包含 `CURRENT` 的子 LevelDB 库（及自身，若自身即为 LevelDB 库）。"""
    base = Path(parent_dir)
    if not base.is_dir():
        raise LevelDBFormatError(f"快照根目录不存在: {base}")
    if (base / "CURRENT").is_file():
        return [base]
    dbs = [p for p in sorted(base.iterdir()) if p.is_dir() and (p / "CURRENT").is_file()]
    if not dbs:
        raise LevelDBFormatError(f"目录下未发现含 CURRENT 的 LevelDB 子库: {base}")
    return dbs
