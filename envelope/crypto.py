"""密码原语封装（AES-256-GCM）。

设计约定
========

* 主密钥 (MK) 与数据密钥 (DEK) 均为 256 位，由 ``os.urandom`` 本地生成，仅用于测试。
* 分块 AEAD 使用 AES-256-GCM。**块 nonce 不使用随机数**，而是在"单文件 + 单 DEK"
  作用域内由块序号确定性派生（``BLCK`` 域前缀 + 64 位大端序号），因此同一文件内
  nonce 天然唯一、无碰撞概率问题；不同文件使用不同的随机 DEK，GCM 的密钥独立。
* 包裹数据密钥与封装受保护头时，nonce 在"单主密钥"作用域内由持久化计数器分配
  （见 :mod:`envelope.keystore`），计数器在使用前原子落盘，进程崩溃 / 重试不会
  重放 nonce，从而满足 GCM "同一密钥下 nonce 绝不可重复"的硬性要求。
* 所有认证加密都带显式关联数据 (AAD)，把文件 ID、块序号、算法等上下文绑定进去，
  防止跨文件 / 跨块搬运密文。AAD 采用键排序、无多余空白的 canonical JSON 序列化，
  保证加解密两端字节一致。

GCM 单密钥安全上限：每密钥最多加密 ``2^32`` 个 nonce（本实现的计数器 / 序号空间
远未触及），且建议单密钥加密总量远低于 64 GiB；轮换主密钥也可作为总量管理手段。
"""

from __future__ import annotations

import json
import os
import struct

from cryptography.exceptions import InvalidTag
from cryptography.hazmat.primitives.ciphers.aead import AESGCM

from .errors import AEADAuthenticationError

KEY_BYTES = 32  # AES-256
NONCE_BYTES = 12  # GCM 推荐 96 位 nonce
ALGORITHM = "AES-256-GCM"
VERSION = 1

# nonce 域前缀（前 4 字节），把不同用途的 nonce 空间显式分开。
_BLOCK_NONCE_PREFIX = b"BLCK"
_WRAP_NONCE_PREFIX = b"WRAP"
_HEADER_NONCE = b"HDR" + b"\x00" * 8 + b"\x01"  # 12 字节；受保护头每信封只封装一次  # 受保护头：每信封只封装一次


def generate_key() -> bytes:
    """生成 256 位随机密钥（主密钥或数据密钥）。"""
    return os.urandom(KEY_BYTES)


def canonical_json(obj: dict) -> bytes:
    """确定性 JSON 序列化：键排序、无空白分隔符、不转义非 ASCII。

    加解密两端必须逐字节一致，因此不能依赖 dict 顺序或默认空白。
    """
    return json.dumps(obj, sort_keys=True, separators=(",", ":"), ensure_ascii=False).encode(
        "utf-8"
    )


def block_aad(*, file_id: str, block_index: int, chunk_size: int) -> bytes:
    """构造单个数据块的关联数据。

    绑定：文件 ID、块序号、分块大小、算法与容器版本。
    块序号绑定可检测"同文件内交换块"，文件 ID 绑定可检测"跨文件交换块"。
    """
    return canonical_json(
        {
            "purpose": "data-block",
            "fid": file_id,
            "block": block_index,
            "chunk_size": chunk_size,
            "alg": ALGORITHM,
            "v": VERSION,
        }
    )


def wrap_aad(*, file_id: str) -> bytes:
    """构造"主密钥包裹数据密钥"的关联数据，防止跨文件搬运被包裹的 DEK。"""
    return canonical_json(
        {
            "purpose": "wrap-dek",
            "fid": file_id,
            "alg": ALGORITHM,
            "v": VERSION,
        }
    )


def header_aad(*, file_id: str) -> bytes:
    """构造受保护头封装的关联数据。"""
    return canonical_json(
        {
            "purpose": "protected-header",
            "fid": file_id,
            "alg": ALGORITHM,
            "v": VERSION,
        }
    )


def block_nonce(block_index: int) -> bytes:
    """由块序号确定性生成 12 字节 nonce：``BLCK`` + uint64 大端序号。"""
    if not 0 <= block_index <= 0xFFFFFFFFFFFFFFFF:
        raise ValueError("block_index 超出 uint64 范围")
    return _BLOCK_NONCE_PREFIX + struct.pack(">Q", block_index)


def wrap_nonce(counter: int) -> bytes:
    """由持久化计数器生成 12 字节包裹 nonce：``WRAP`` + uint64 大端计数。"""
    if not 1 <= counter <= 0xFFFFFFFFFFFFFFFF:
        raise ValueError("wrap counter 超出 uint64 有效范围")
    return _WRAP_NONCE_PREFIX + struct.pack(">Q", counter)


def header_nonce() -> bytes:
    """受保护头的固定 nonce（信封内每个 DEK 只封装一次头，密钥域唯一）。"""
    return _HEADER_NONCE


def aead_seal(key: bytes, nonce: bytes, plaintext: bytes, aad: bytes) -> bytes:
    """AES-GCM 加密，返回 ``密文 || 16字节tag``（cryptography 的输出布局）。"""
    if len(key) != KEY_BYTES:
        raise ValueError("密钥必须为 32 字节")
    if len(nonce) != NONCE_BYTES:
        raise ValueError("nonce 必须为 12 字节")
    return AESGCM(key).encrypt(nonce, plaintext, aad)


def aead_open(key: bytes, nonce: bytes, ciphertext: bytes, aad: bytes) -> bytes:
    """AES-GCM 认证解密；tag 不匹配时抛出 :class:`AEADAuthenticationError`。"""
    if len(key) != KEY_BYTES:
        raise ValueError("密钥必须为 32 字节")
    if len(nonce) != NONCE_BYTES:
        raise ValueError("nonce 必须为 12 字节")
    try:
        return AESGCM(key).decrypt(nonce, ciphertext, aad)
    except InvalidTag as exc:
        raise AEADAuthenticationError(
            "AEAD 认证失败：密文/nonce/关联数据被篡改，或密钥不正确"
        ) from exc
