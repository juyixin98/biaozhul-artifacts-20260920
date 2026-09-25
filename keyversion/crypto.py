"""密码原语封装。

安全边界说明：
- 对称算法固定为 AES-256-GCM（cryptography 库的 AESGCM，内部为 OpenSSL）。
- 不自创算法；每次加密使用 os.urandom 生成的 12 字节随机 nonce。
- 密钥指纹仅为 SHA-256 摘要，用于完整性/存在性核对，不暴露密钥材料。
- 磁盘上的 DEK（数据加密密钥）由本地 KEK 以 AES-GCM 包装后保存；
  KEK 保存在单独的、权限 0600 的文件中。删除 KEK 文件即等同于销毁本地
  密钥库，包装后的 DEK 无法解开（本项目不涉及 HSM/KMS）。
"""

from __future__ import annotations

import hashlib
import os

from cryptography.hazmat.primitives.ciphers.aead import AESGCM

#: AES-256 密钥长度（字节）
KEY_LEN = 32
#: GCM 推荐 nonce 长度（字节）
NONCE_LEN = 12
#: 信封魔数，标识格式与版本
MAGIC = b"KVA1"


def generate_key() -> bytes:
    """生成 256 位随机数据加密密钥（DEK）。"""
    return AESGCM.generate_key(bit_length=256)


def generate_nonce() -> bytes:
    """生成 12 字节随机 nonce。"""
    return os.urandom(NONCE_LEN)


def key_fingerprint(key_material: bytes) -> str:
    """返回密钥材料的 SHA-256 十六进制指纹。

    指纹用于在内存/磁盘记录之间核对密钥是否被替换；它不是秘密的可逆表示。
    """
    return hashlib.sha256(key_material).hexdigest()


def _aad(version_id: str) -> bytes:
    """GCM 附加认证数据：把版本号绑定进密文，防止密文被换到别的版本名下。"""
    vid = version_id.encode("utf-8")
    # MAGIC | 版本号长度(2B, BE) | 版本号
    return MAGIC + len(vid).to_bytes(2, "big") + vid


def encrypt(key_material: bytes, version_id: str, plaintext: bytes) -> bytes:
    """用指定版本的密钥加密明文，返回自描述二进制信封。

    信封布局::

        MAGIC(4B) | vlen(2B BE) | version_id(UTF-8) | nonce(12B) | ct+tag

    解密时以 version_id 作为 AAD，因此改动信封中的版本号会导致认证失败。
    """
    if not isinstance(plaintext, (bytes, bytearray)):
        raise TypeError("plaintext must be bytes")
    nonce = generate_nonce()
    ct = AESGCM(key_material).encrypt(nonce, bytes(plaintext), _aad(version_id))
    vid = version_id.encode("utf-8")
    if len(vid) > 0xFFFF:
        raise ValueError("version_id too long")
    return MAGIC + len(vid).to_bytes(2, "big") + vid + nonce + ct


def parse_envelope(envelope: bytes) -> str:
    """从信封中解析出版本 ID（不做解密）。格式错误抛 InvalidEnvelope。"""
    from .errors import InvalidEnvelope

    if not isinstance(envelope, (bytes, bytearray)):
        raise InvalidEnvelope("envelope must be bytes")
    buf = bytes(envelope)
    if len(buf) < 4 + 2 or buf[:4] != MAGIC:
        raise InvalidEnvelope("bad magic: not a KVA1 envelope")
    vlen = int.from_bytes(buf[4:6], "big")
    end = 6 + vlen
    if vlen == 0 or end + NONCE_LEN > len(buf):
        raise InvalidEnvelope("truncated envelope")
    try:
        return buf[6:end].decode("utf-8")
    except UnicodeDecodeError as exc:
        raise InvalidEnvelope("invalid version id encoding") from exc


def decrypt(key_material: bytes, envelope: bytes) -> bytes:
    """解密 KVA1 信封。版本号被绑定进 AAD，错版本/篡改都会认证失败。"""
    from .errors import InvalidEnvelope

    buf = bytes(envelope)
    if len(buf) < 4 + 2 or buf[:4] != MAGIC:
        raise InvalidEnvelope("bad magic: not a KVA1 envelope")
    vlen = int.from_bytes(buf[4:6], "big")
    end = 6 + vlen
    if vlen == 0 or end + NONCE_LEN >= len(buf):
        raise InvalidEnvelope("truncated envelope")
    try:
        version_id = buf[6:end].decode("utf-8")
    except UnicodeDecodeError as exc:
        raise InvalidEnvelope("invalid version id encoding") from exc
    nonce, ct = buf[end : end + NONCE_LEN], buf[end + NONCE_LEN :]
    try:
        return AESGCM(key_material).decrypt(nonce, ct, _aad(version_id))
    except Exception as exc:  # InvalidTag 等，统一归类
        from .errors import InvalidEnvelope as _IE

        raise _IE("authentication failed: wrong key, wrong version, or tampered ciphertext") from exc


def wrap_dek(kek: bytes, dek: bytes) -> bytes:
    """用 KEK 包装 DEK，输出 nonce(12B) || ciphertext(含 tag)。"""
    nonce = generate_nonce()
    return nonce + AESGCM(kek).encrypt(nonce, dek, MAGIC + b"wrap")


def unwrap_dek(kek: bytes, wrapped: bytes) -> bytes:
    """解开被 KEK 包装的 DEK；KEK 错误或数据损坏时认证失败。"""
    from .errors import InvalidEnvelope

    if len(wrapped) <= NONCE_LEN:
        raise InvalidEnvelope("wrapped key blob too short")
    try:
        return AESGCM(kek).decrypt(wrapped[:NONCE_LEN], wrapped[NONCE_LEN:], MAGIC + b"wrap")
    except Exception as exc:
        from .errors import InvalidEnvelope as _IE

        raise _IE("cannot unwrap DEK: wrong KEK or corrupted blob") from exc
