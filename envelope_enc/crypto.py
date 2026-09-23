"""底层密码原语封装。

唯一算法：AES-256-GCM（AEAD），由 `cryptography` 提供实现，本模块只做
"生成/密封/开启/nonce 拼装/AAD 编码"，绝不发明加密算法。

nonce 唯一性策略（务必理解）：
- AES-GCM 的安全前提是同一把密钥下 nonce 绝不复用。
- 本项目中数据密钥 DEK 是"每文件一把随机密钥"（见 container.encrypt_bytes），
  因此即使两个文件的块 nonce 字节相同，它们也处于不同密钥下，不构成复用。
- 单个文件内部：块 nonce = NONCE_PREFIX（4 字节随机，随文件头存储）
  + 8 字节大端块计数器（从 0 开始），天然唯一。
- 包裹 DEK、认证文件头各使用一次独立的 12 字节随机 nonce，
  且其密钥作用域分别是"主密钥""该文件 DEK"。
"""

from __future__ import annotations

import os
import struct

from cryptography.exceptions import InvalidTag
from cryptography.hazmat.primitives.ciphers.aead import AESGCM

#: 主密钥 / 数据密钥长度（字节）：256 位
KEY_LEN = 32
#: GCM 标准 nonce 长度（字节）：96 位
NONCE_LEN = 12
#: GCM 认证标签长度（字节）：128 位
TAG_LEN = 16
#: 每文件随机 nonce 前缀长度（字节），其余 8 字节为块计数器
NONCE_PREFIX_LEN = 4

# 块计数器 8 字节；工程上把单文件块数限制在 2^32，远小于 2^64
MAX_CHUNKS = 1 << 32


class NonceReuseError(Exception):
    """同一数据密钥下 nonce 计数器超出安全范围（防御性检查）。"""


def random_key() -> bytes:
    """生成一把 256 位随机密钥，熵来自操作系统 CSPRNG。"""
    return AESGCM.generate_key(bit_length=256)


def random_nonce() -> bytes:
    """生成一把随机 96 位 nonce（用于一次性的包裹/头部认证）。"""
    return os.urandom(NONCE_LEN)


def random_nonce_prefix() -> bytes:
    """生成每文件随机 nonce 前缀（4 字节）。"""
    return os.urandom(NONCE_PREFIX_LEN)


def chunk_nonce(prefix: bytes, chunk_index: int) -> bytes:
    """拼装块 nonce：4 字节随机前缀 + 8 字节大端计数器。

    :param prefix: 文件头中保存的 4 字节随机前缀
    :param chunk_index: 从 0 开始的块序号
    """
    if len(prefix) != NONCE_PREFIX_LEN:
        raise ValueError(f"nonce 前缀长度必须为 {NONCE_PREFIX_LEN} 字节")
    if not 0 <= chunk_index < MAX_CHUNKS:
        raise NonceReuseError(f"块序号 {chunk_index} 超出 {MAX_CHUNKS} 上限")
    return prefix + struct.pack(">Q", chunk_index)


def seal(key: bytes, nonce: bytes, plaintext: bytes, aad: bytes) -> bytes:
    """AES-GCM 加密，返回 密文||16字节标签；key/nonce 长度被 AESGCM 校验。"""
    if len(key) != KEY_LEN:
        raise ValueError(f"密钥长度必须为 {KEY_LEN} 字节（AES-256）")
    if len(nonce) != NONCE_LEN:
        raise ValueError(f"nonce 长度必须为 {NONCE_LEN} 字节")
    return AESGCM(key).encrypt(nonce, plaintext, aad)


def open_sealed(key: bytes, nonce: bytes, ciphertext: bytes, aad: bytes) -> bytes:
    """AES-GCM 解密并认证；认证失败抛出 ``cryptography.exceptions.InvalidTag``。"""
    if len(key) != KEY_LEN:
        raise ValueError(f"密钥长度必须为 {KEY_LEN} 字节（AES-256）")
    if len(nonce) != NONCE_LEN:
        raise ValueError(f"nonce 长度必须为 {NONCE_LEN} 字节")
    return AESGCM(key).decrypt(nonce, ciphertext, aad)


# ---------------------------------------------------------------------------
# AAD（关联数据）编码
#
# 所有需要绑定的字段都以"域标签 + 8 字节大端长度 + 原始字节"的方式拼接，
# 域标签用于区分不同语义的 AAD（包裹 / 文件头 / 块），杜绝跨上下文混用。
# ---------------------------------------------------------------------------

_BE64 = struct.Struct(">Q")


def _field(tag: bytes, data: bytes = b"", n: int | None = None) -> bytes:
    """编码一个 AAD 字段：tag(定长ASCII) || len(data)(BE64) || data。"""
    if n is not None:
        data = _BE64.pack(n)
    return tag + _BE64.pack(len(data)) + data


def wrap_aad(kid: str) -> bytes:
    """包裹数据密钥时的 AAD：把 DEK 绑定到"主密钥包裹"这一用途和 kid 上。

    这样 DEK 的密文不能被挪到其它用途（算法/上下文混淆），
    也不能在不同 kid 的包裹字段之间互换（头 AAD 还会再绑定 kid）。
    """
    raw_kid = kid.encode("utf-8")
    return _field(b"envenc/v1/wrap", b"") + _field(b"kid", raw_kid)


def header_aad(
    *,
    file_id: str,
    kid: str,
    chunk_size: int,
    plaintext_size: int,
    chunk_count: int,
    nonce_prefix: bytes,
) -> bytes:
    """文件头的 AAD：认证所有影响解密语义的头部明文字段。"""
    return b"".join(
        (
            _field(b"envenc/v1/header", b""),
            _field(b"file_id", file_id.encode("utf-8")),
            _field(b"kid", kid.encode("utf-8")),
            _field(b"chunk_size", n=chunk_size),
            _field(b"plaintext_size", n=plaintext_size),
            _field(b"chunk_count", n=chunk_count),
            _field(b"nonce_prefix", nonce_prefix),
        )
    )


def chunk_aad(*, file_id: str, chunk_index: int, plaintext_len: int) -> bytes:
    """块的 AAD：把密文块绑定到 文件ID、块序号、该块明文长度。

    - 绑定 file_id：A 文件的块复制/交换到 B 文件 → 认证失败；
    - 绑定 chunk_index：同一文件内调换/重放块顺序 → 认证失败；
    - 绑定 plaintext_len：块长度被改动 → 认证失败（截断见 container 解析层）。
    """
    return b"".join(
        (
            _field(b"envenc/v1/chunk", b""),
            _field(b"file_id", file_id.encode("utf-8")),
            _field(b"index", n=chunk_index),
            _field(b"pt_len", n=plaintext_len),
        )
    )
