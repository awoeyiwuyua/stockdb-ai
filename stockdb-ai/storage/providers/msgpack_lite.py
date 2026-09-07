"""storage.providers.msgpack_lite — 零依赖 MsgPack 解包器（0.10.29）。

背景：上游引擎 0.3.5 起 HTTP 端口响应改为 MsgPack（Content-Type:
application/x-msgpack），旧版 JSON 响应不再提供（官方样例文档已过时）；
webui/MCP 全链路按 JSON 解析，必须先行解码。本模块为纯标准库实现
（只有 MINIMAL 子集 + 完整标准标量类型），不引第三方依赖（项目零依赖纪律）。

与 fetch 的配合：free_stockdb.fetch 按 Content-Type 嗅探——
msgpack → 本模块解包 → json.dumps 回 JSON 文本（调用方 json.loads 契约不变）；
其余 Content-Type 走原文本透传路径。

实现支持：nil/bool/uint(int8..64)/float32/64/str(fixstr,8,16,32)/bin(fixbin,8,16,32)/
array(fixarray,16,32)/map(fixmap,16,32)；ext 类型遇到即抛（引擎不用）。
"""
from __future__ import annotations


class MsgPackError(ValueError):
    """非法输入/未知类型标记。"""


def unpack(data: bytes) -> object:
    """解码单条 msgpack 值。数据尾部残余字节视为错误（引擎响应=单值）。"""
    value, offset = _unpack(data, 0)
    if offset != len(data):
        raise MsgPackError(f"trailing bytes after value: {len(data) - offset}")
    return value


def _err(data: bytes, offset: int, msg: str) -> "MsgPackError":
    return MsgPackError(f"{msg} @ {offset} (byte=0x{data[offset]:02x})")


def _read(data: bytes, offset: int, n: int) -> bytes:
    end = offset + n
    if end > len(data):
        raise _err(data, offset, "unexpected end of input")
    return data[offset:end]


def _unpack(data: bytes, offset: int) -> tuple[object, int]:
    b = data[offset]

    # ---- 原地标量（单字节标记）----
    if b <= 0x7F:
        return b, offset + 1
    if b >= 0xE0:
        return b - 0x100, offset + 1
    if 0x80 <= b <= 0x8F:
        return _unpack_map(data, offset + 1, b & 0x0F)
    if 0x90 <= b <= 0x9F:
        return _unpack_array(data, offset + 1, b & 0x0F)
    if 0xA0 <= b <= 0xBF:
        raw = _read(data, offset + 1, b & 0x1F)
        return raw.decode("utf-8"), offset + 1 + len(raw)

    if b == 0xC0:
        return None, offset + 1
    if b == 0xC2:
        return False, offset + 1
    if b == 0xC3:
        return True, offset + 1

    # ---- bin（预期不出现在引擎响应；按 UTF-8/原始字节容错）----
    if b == 0xC4:
        n = data[offset + 1]
        raw = _read(data, offset + 2, n)
        return raw.decode("utf-8"), offset + 2 + n
    if b == 0xC5:
        n = int.from_bytes(_read(data, offset + 1, 2), "big")
        raw = _read(data, offset + 3, n)
        return raw.decode("utf-8"), offset + 3 + n
    if b == 0xC6:
        n = int.from_bytes(_read(data, offset + 1, 4), "big")
        raw = _read(data, offset + 5, n)
        return raw.decode("utf-8"), offset + 5 + n

    # ---- 定长整数 ----
    if b == 0xCC:
        return data[offset + 1], offset + 2
    if b == 0xCD:
        return int.from_bytes(_read(data, offset + 1, 2), "big"), offset + 3
    if b == 0xCE:
        return int.from_bytes(_read(data, offset + 1, 4), "big"), offset + 5
    if b == 0xCF:
        return int.from_bytes(_read(data, offset + 1, 8), "big"), offset + 9
    if b == 0xD0:
        return int.from_bytes(_read(data, offset + 1, 1), "big", signed=True), offset + 2
    if b == 0xD1:
        return int.from_bytes(_read(data, offset + 1, 2), "big", signed=True), offset + 3
    if b == 0xD2:
        return int.from_bytes(_read(data, offset + 1, 4), "big", signed=True), offset + 5
    if b == 0xD3:
        return int.from_bytes(_read(data, offset + 1, 8), "big", signed=True), offset + 9

    # ---- 浮点 ----
    if b == 0xCA:
        raw = _read(data, offset + 1, 4)
        return _f32(raw), offset + 5
    if b == 0xCB:
        raw = _read(data, offset + 1, 8)
        return _f64(raw), offset + 9

    # ---- 字符串 ----
    if b == 0xD9:
        n = data[offset + 1]
        raw = _read(data, offset + 2, n)
        return raw.decode("utf-8"), offset + 2 + n
    if b == 0xDA:
        n = int.from_bytes(_read(data, offset + 1, 2), "big")
        raw = _read(data, offset + 3, n)
        return raw.decode("utf-8"), offset + 3 + n
    if b == 0xDB:
        n = int.from_bytes(_read(data, offset + 1, 4), "big")
        raw = _read(data, offset + 5, n)
        return raw.decode("utf-8"), offset + 5 + n

    # ---- 数组 ----
    if b == 0xDC:
        n = int.from_bytes(_read(data, offset + 1, 2), "big")
        return _unpack_array(data, offset + 3, n)
    if b == 0xDD:
        n = int.from_bytes(_read(data, offset + 1, 4), "big")
        return _unpack_array(data, offset + 5, n)

    # ---- 映射 ----
    if b == 0xDE:
        n = int.from_bytes(_read(data, offset + 1, 2), "big")
        return _unpack_map(data, offset + 3, n)
    if b == 0xDF:
        n = int.from_bytes(_read(data, offset + 1, 4), "big")
        return _unpack_map(data, offset + 5, n)

    raise _err(data, offset, "unsupported msgpack type (0xd4-0xd8/d0-c1 ext/others)")


def _unpack_array(data: bytes, offset: int, n: int) -> tuple[list, int]:
    items = []
    for _ in range(n):
        item, offset = _unpack(data, offset)
        items.append(item)
    return items, offset


def _unpack_map(data: bytes, offset: int, n: int) -> tuple[dict, int]:
    result = {}
    for _ in range(n):
        key, offset = _unpack(data, offset)
        value, offset = _unpack(data, offset)
        result[key] = value
    return result, offset


def _f32(raw: bytes) -> float:
    import struct

    return struct.unpack(">f", raw)[0]


def _f64(raw: bytes) -> float:
    import struct

    return struct.unpack(">d", raw)[0]
