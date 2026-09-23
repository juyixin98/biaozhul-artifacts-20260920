"""Solidity 合约元数据处理：CBOR 元数据尾解析、哈希真实核验。

solc 会把一段 CBOR 编码的 map 追加在 deployed/creation 字节码末尾，
CBOR 之后的最后 2 个字节是该 CBOR 段的**大端长度**。典型字段：
``ipfs``(32B SHA-256)、``bzzr0``/``bzzr1``(32B Swarm BMT)、``solc``(版本三段)、
``experimental`` 等。未知键不会被丢弃，而是原样保留进证据链。
"""

from __future__ import annotations

import json

from . import crypto


class MetadataError(ValueError):
    """元数据缺失或结构非法。"""


# --------------------------------------------------------------------------- #
# 最小确定性 CBOR 解码器（RFC 8949 子集，覆盖 solc 全部已知输出）
# --------------------------------------------------------------------------- #
def _read_len(buf: bytes, off: int, ai: int) -> tuple[int, int]:
    if ai < 24:
        return ai, off
    sizes = {24: 1, 25: 2, 26: 4, 27: 8}
    if ai not in sizes:
        raise MetadataError(f"CBOR 不支持的附加信息 {ai}（拒绝不定长）")
    n = sizes[ai]
    if off + n > len(buf):
        raise MetadataError("CBOR 长度前缀越界")
    return int.from_bytes(buf[off : off + n], "big"), off + n


def _decode(buf: bytes, off: int) -> tuple[object, int]:
    if off >= len(buf):
        raise MetadataError("CBOR 在条目中途结束")
    initial = buf[off]
    off += 1
    major, ai = initial >> 5, initial & 0x1F
    if ai == 31:
        raise MetadataError("CBOR 不接受不定长条目")
    val, off = _read_len(buf, off, ai)
    if major == 0:
        return val, off
    if major == 1:
        return -1 - val, off
    if major in (2, 3):
        if off + val > len(buf):
            raise MetadataError("CBOR 字节串/字符串越界")
        raw = buf[off : off + val]
        off += val
        if major == 2:
            return raw, off
        try:
            return raw.decode("utf-8"), off
        except UnicodeDecodeError as exc:
            raise MetadataError(f"CBOR 文本不是 UTF-8: {exc}") from exc
    if major == 4:
        arr = []
        for _ in range(val):
            item, off = _decode(buf, off)
            arr.append(item)
        return arr, off
    if major == 5:
        obj = {}
        for _ in range(val):
            k, off = _decode(buf, off)
            if not isinstance(k, str):
                raise MetadataError("CBOR map 键必须是文本串")
            v, off = _decode(buf, off)
            obj[k] = v
        return obj, off
    if major == 7:
        if val == 20:
            return False, off
        if val == 21:
            return True, off
        if val in (22, 23):
            return None, off
        raise MetadataError(f"CBOR 不支持的 simple/float 值 {val}")
    raise MetadataError(f"CBOR 未知主类型 {major}")


def decode_cbor_segment(segment: bytes) -> dict:
    """解码完整 CBOR map，且要求恰好消费整个段（尾随字节即非法）。"""
    obj, end = _decode(segment, 0)
    if end != len(segment):
        raise MetadataError(f"CBOR 段有 {len(segment) - end} 个尾随字节")
    if not isinstance(obj, dict):
        raise MetadataError("CBOR 顶层不是 map")
    return obj


def parse_cbor_tail(bytecode_hex: str) -> tuple[dict, int]:
    """从字节码中切出 solc CBOR 元数据尾。

    返回 ``(cbor_map, 元数据起始nibble)``，起始位置用于掩码比较。
    """
    raw = bytes.fromhex(bytecode_hex)
    if len(raw) < 4:
        raise MetadataError("字节码过短，不可能包含 CBOR 元数据尾")
    tail_len = int.from_bytes(raw[-2:], "big")
    if tail_len == 0 or tail_len + 2 > len(raw):
        raise MetadataError(f"CBOR 尾长度 {tail_len} 非法")
    segment = raw[-2 - tail_len : -2]
    cbor = decode_cbor_segment(segment)
    start_nibble = (len(raw) - 2 - tail_len) * 2
    return cbor, start_nibble


def cbor_value_repr(v: object) -> object:
    """把 CBOR 解出的值转成可入证据链的 JSON 友好形式。"""
    if isinstance(v, bytes):
        return "0x" + v.hex()
    if isinstance(v, list):
        return [cbor_value_repr(x) for x in v]
    if isinstance(v, dict):
        return {k: cbor_value_repr(x) for k, x in v.items()}
    return v


# --------------------------------------------------------------------------- #
# 元数据 JSON
# --------------------------------------------------------------------------- #
def parse_metadata_json(raw: bytes | str) -> tuple[dict, bytes]:
    """解析 solc 标准 JSON 输出中的 metadata 字符串，返回 (对象, 原始字节)。"""
    if isinstance(raw, str):
        raw_b = raw.encode("utf-8")
    else:
        raw_b = raw
    try:
        obj = json.loads(raw_b.decode("utf-8"))
    except (UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise MetadataError(f"metadata 不是合法 JSON: {exc}") from exc
    if not isinstance(obj, dict):
        raise MetadataError("metadata 顶层不是对象")
    return obj, raw_b


def verify_embedded_hash(metadata_raw: bytes, cbor: dict) -> dict:
    """真实重算元数据哈希并与 CBOR 内嵌值比对。

    支持 ipfs（SHA-256）、bzzr0/bzzr1（Swarm BMT）；未知哈希键保留上报。
    """
    results: list[dict] = []
    for key in (crypto.HASH_IPFS, crypto.HASH_BZZR0, crypto.HASH_BZZR1):
        embedded = cbor.get(key)
        if embedded is None:
            continue
        if not isinstance(embedded, bytes):
            results.append({"scheme": key, "ok": False, "reason": "内嵌值不是字节串"})
            continue
        try:
            if key == crypto.HASH_IPFS:
                computed = crypto.sha256(metadata_raw)
                extra = {"cidv0": crypto.to_cidv0(computed)}
            else:
                computed = crypto.swarm_bmt_single(metadata_raw, key)
                extra = {}
        except crypto.HashNotSupported as exc:
            results.append(
                {
                    "scheme": key,
                    "ok": None,
                    "reason": str(exc),
                    "embedded": embedded.hex(),
                }
            )
            continue
        results.append(
            {
                "scheme": key,
                "ok": computed == embedded,
                "computed": computed.hex(),
                "embedded": embedded.hex(),
                **extra,
            }
        )
    unknown = [k for k in cbor if k not in (crypto.HASH_IPFS, crypto.HASH_BZZR0, crypto.HASH_BZZR1, "solc", "experimental")]
    return {"hash_checks": results, "unknown_keys": unknown}
