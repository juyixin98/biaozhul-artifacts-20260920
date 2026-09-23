"""真实密码学原语封装：SHA-256、Keccak-256、EIP-55 地址校验、CIDv0、Swarm BMT。

- SHA-256 / Keccak-256 使用标准库与 pycryptodome 实际计算，绝无桩函数。
- Swarm bzzr0/bzzr1 为以太坊 Solidity 元数据使用的两种 BMT 哈希；二者在单分块
  （元数据 < 4096 字节，真实 solc 输出恒满足）时计算完全一致，多分块时策略见
  :func:`swarm_bmt_single`，本服务对超出单分块能力的数据如实报告不可判定，
  不静默跳过。
"""

from __future__ import annotations

import hashlib

from Crypto.Hash import keccak as _keccak

#: Solidity 元数据 CBOR 使用的哈希键
HASH_IPFS = "ipfs"
HASH_BZZR0 = "bzzr0"
HASH_BZZR1 = "bzzr1"

#: Swarm 分块大小（128 个 32 字节哈希段）
_BMT_SEGMENT_SIZE = 32
_BMT_SEGMENT_COUNT = 128
_BMT_CHUNK_SIZE = _BMT_SEGMENT_SIZE * _BMT_SEGMENT_COUNT  # 4096


def sha256(data: bytes) -> bytes:
    """原生 SHA-256。"""
    return hashlib.sha256(data).digest()


def keccak256(data: bytes) -> bytes:
    """Keccak-256（以太坊/旧 Solidity 使用；注意与 NIST SHA3-256 不同）。"""
    h = _keccak.new(digest_bits=256)
    h.update(data)
    return h.digest()


def _bmt_sum(segments: list[bytes]) -> bytes:
    """在 128 路 BMT 树的一层上做归并（不足补零段）。"""
    segs = list(segments)
    if len(segs) > _BMT_SEGMENT_COUNT:
        raise ValueError("BMT segment overflow")
    segs.extend([b"\x00" * _BMT_SEGMENT_SIZE] * (_BMT_SEGMENT_COUNT - len(segs)))
    while len(segs) > 1:
        nxt: list[bytes] = []
        for i in range(0, len(segs), 2):
            nxt.append(keccak256(segs[i] + segs[i + 1]))
        segs = nxt
    return segs[0]


def _bmt_chunk_hash(chunk_data: bytes, span: int) -> bytes:
    """单个 Swarm 分块的 BMT 哈希：data 右侧零填充到 4096，前置 8 字节小端 span。"""
    if len(chunk_data) > _BMT_CHUNK_SIZE:
        raise ValueError("chunk data exceeds 4096 bytes")
    padded = chunk_data + b"\x00" * (_BMT_CHUNK_SIZE - len(chunk_data))
    segments = [padded[i : i + _BMT_SEGMENT_SIZE] for i in range(0, _BMT_CHUNK_SIZE, _BMT_SEGMENT_SIZE)]
    root = _bmt_sum(segments)
    span_bytes = span.to_bytes(8, "little", signed=False)
    return keccak256(span_bytes + root)


class HashNotSupported(Exception):
    """数据规模超出本服务真实实现能力，无法判定——禁止静默忽略。"""


def swarm_bmt_single(data: bytes, scheme: str = HASH_BZZR1) -> bytes:
    """计算 Swarm 元数据哈希（bzzr0 / bzzr1）。

    solc 元数据 JSON 体积远小于 4096 字节，始终只有一个分块，span 即数据长度，
    bzzr0 与 bzzr1 的单分块计算相同。对 >= 4096 字节的输入抛出
    :class:`HashNotSupported`，由上层如实记录为不可判定，绝不当作通过。
    """
    if scheme not in (HASH_BZZR0, HASH_BZZR1):
        raise ValueError(f"unknown swarm scheme: {scheme}")
    if len(data) >= _BMT_CHUNK_SIZE:
        raise HashNotSupported(
            f"metadata {len(data)} bytes >= {_BMT_CHUNK_SIZE}; 多分块 {scheme} 不在离线核验能力内"
        )
    return _bmt_chunk_hash(data, len(data))


_ALPHABET = b"123456789ABCDEFGHJKLMNPQRSTUVWXYZabcdefghijkmnopqrstuvwxyz"
_ALPHABET_INDEX = {c: i for i, c in enumerate(_ALPHABET)}


def base58_encode(data: bytes) -> str:
    """Bitcoin 字母表 Base58 编码（CIDv0 使用）。"""
    n = int.from_bytes(data, "big")
    out = bytearray()
    while n > 0:
        n, r = divmod(n, 58)
        out.append(_ALPHABET[r])
    # 前导零字节 -> 前导 '1'
    for b in data:
        if b == 0:
            out.append(_ALPHABET[0])
        else:
            break
    return bytes(reversed(out)).decode("ascii")


def base58_decode(s: str) -> bytes:
    n = 0
    for ch in s.encode("ascii"):
        n = n * 58 + _ALPHABET_INDEX[ch]
    pad = 0
    for ch in s:
        if ch == "1":
            pad += 1
        else:
            break
    body = n.to_bytes((n.bit_length() + 7) // 8, "big") if n else b""
    return b"\x00" * pad + body


def to_cidv0(ipfs_sha256_digest: bytes) -> str:
    """把字节码内嵌的 32 字节原始 SHA-256 转成 CIDv0（dag-pb + sha2-256 multihash）。"""
    if len(ipfs_sha256_digest) != 32:
        raise ValueError("ipfs digest must be 32 bytes")
    multihash = bytes([0x12, 0x20]) + ipfs_sha256_digest  # sha2-256, len 32
    return base58_encode(multihash)


def to_eip55(addr20: bytes) -> str:
    """EIP-55 校验和地址（全部小写 keccak 哈希决定大写位）。"""
    if len(addr20) != 20:
        raise ValueError("address must be 20 bytes")
    low = addr20.hex()
    h = keccak256(low.encode("ascii")).hex()
    out = "0x"
    for ch, hh in zip(low, h):
        if ch in "0123456789":
            out += ch
        else:
            out += ch.upper() if int(hh, 16) >= 8 else ch
    return out


def is_valid_eip55(address: str) -> bool:
    """严格 EIP-55 校验：由全小写地址重算校验和并逐字符比较。

    按 EIP-55 标准，哈希后所有字母位恰好都大写/都小写的地址，其全大写/全小写
    串本身就是合法校验和形式（见官方测试向量），因此不额外拒绝同态大小写。
    任何大小写位置与标准不符（如样例 08）即不通过。
    """
    if not (address.startswith("0x") and len(address) == 42):
        return False
    body = address[2:]
    try:
        int(body, 16)
    except ValueError:
        return False
    return to_eip55(bytes.fromhex(body)) == address
