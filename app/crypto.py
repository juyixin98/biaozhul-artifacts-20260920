"""信封加密: 容器格式、AEAD 加解密、数据密钥包裹。

设计要点
--------
* 每个对象独立生成一个 256 位数据密钥 (DEK), 主密钥 (KEK) 从不加密数据本体。
* 数据使用 AES-256-GCM (成熟 AEAD) 分块加密; 每块 nonce =
  对象随机 salt(8B) || 块序号(4B 大端), 同一 DEK 下 nonce 唯一。
* 块 AAD 绑定: 魔数、容器版本、块序号、明文总长度 —— 防止块调换 / 拼接。
* 头部为明文但所有字段都进入 DEK 包裹的 AAD; 头部版本号因此被认证,
  篡改头部字段会导致 DEK 解包失败, 不会解出任何明文。
* 主密钥轮换只重新包裹 DEK (重写头部), 密文数据块原样保留。

容器字节布局 (多字节整数均大端)::

    magic        4B   = b"ENV1"
    version      1B   = 0x01
    key_id_len   1B
    key_id       key_id_len B (ASCII)
    salt         8B
    block_size   4B
    pt_len       8B   (明文总长度)
    wrapped_len  2B
    wrapped_dek  wrapped_len B   = wrap_nonce(12) + AESGCM(DEK)
    blocks       依次拼接的 AES-256-GCM 输出 (密文 + 16B tag)

wrapped_dek 的 AAD 覆盖整个头部 (含 version、key_id、salt、block_size、pt_len)。
"""

from __future__ import annotations

import os
import struct
from collections.abc import Callable
from dataclasses import dataclass

from cryptography.exceptions import InvalidTag
from cryptography.hazmat.primitives.ciphers.aead import AESGCM

MAGIC = b"ENV1"
VERSION = 1
KEY_LEN = 32  # AES-256
NONCE_LEN = 12  # AES-GCM 推荐 96 bit nonce
TAG_LEN = 16
SALT_LEN = 8
MAX_KEY_ID_LEN = 32
MAX_BLOCKS = 0xFFFFFFFF
DEFAULT_BLOCK_SIZE = 64 * 1024
MIN_BLOCK_SIZE = 1

_WRAP_DOMAIN = b"env1-dek-wrap"
_BLOCK_DOMAIN = b"env1-blk"


class EnvelopeError(Exception):
    """信封加解密相关错误的基类。"""


class FormatError(EnvelopeError):
    """容器格式非法 (长度不足、字段越界、尾部多余字节等)。"""


class DecryptError(EnvelopeError):
    """认证失败: 错误密钥、头部 / 密文被篡改或截断。"""


class KeyUnavailableError(EnvelopeError):
    """头部 key_id 对应的主密钥不可用 (被删除 / 尚未接收)。"""


@dataclass(frozen=True)
class Header:
    version: int
    key_id: str
    salt: bytes
    block_size: int
    plaintext_len: int
    wrapped_dek: bytes


def new_key() -> bytes:
    """生成一个新的 256 位密钥 (DEK 或主密钥均可)。"""
    return AESGCM.generate_key(bit_length=256)


def _wrap_aad(version: int, key_id: str, salt: bytes, block_size: int, pt_len: int) -> bytes:
    return (
        _WRAP_DOMAIN
        + struct.pack(">B", version)
        + struct.pack(">B", len(key_id))
        + key_id.encode("ascii")
        + salt
        + struct.pack(">IQ", block_size, pt_len)
    )


def _block_aad(index: int, pt_len: int) -> bytes:
    return _BLOCK_DOMAIN + struct.pack(">B", VERSION) + struct.pack(">IQ", index, pt_len)


def _block_nonce(salt: bytes, index: int) -> bytes:
    # salt 对每个对象随机; index 保证同一 salt 下不重用 nonce
    return salt + struct.pack(">I", index)


def wrap_dek(master_key: bytes, key_id: str, dek: bytes, header_base: tuple[int, bytes, int, int]) -> bytes:
    """用主密钥包裹 DEK。header_base = (version, salt, block_size, plaintext_len)。"""
    version, salt, block_size, pt_len = header_base
    aes = AESGCM(master_key)
    nonce = os.urandom(NONCE_LEN)
    ct = aes.encrypt(nonce, dek, _wrap_aad(version, key_id, salt, block_size, pt_len))
    return nonce + ct


def unwrap_dek(
    master_key: bytes,
    key_id: str,
    wrapped: bytes,
    header_base: tuple[int, bytes, int, int],
) -> bytes:
    """解包 DEK; 密钥错误或头部字段被改 -> DecryptError。"""
    version, salt, block_size, pt_len = header_base
    if len(wrapped) < NONCE_LEN + TAG_LEN:
        raise FormatError("wrapped DEK 长度不足")
    aes = AESGCM(master_key)
    nonce, ct = wrapped[:NONCE_LEN], wrapped[NONCE_LEN:]
    try:
        return aes.decrypt(nonce, ct, _wrap_aad(version, key_id, salt, block_size, pt_len))
    except InvalidTag as exc:
        raise DecryptError("DEK 解包认证失败 (主密钥错误或头部被篡改)") from exc


def _encode_header(h: Header) -> bytes:
    kid = h.key_id.encode("ascii")
    return (
        MAGIC
        + struct.pack(">B", h.version)
        + struct.pack(">B", len(kid))
        + kid
        + h.salt
        + struct.pack(">IQ", h.block_size, h.plaintext_len)
        + struct.pack(">H", len(h.wrapped_dek))
        + h.wrapped_dek
    )


def parse_header(buf: bytes) -> tuple[Header, int]:
    """解析头部, 返回 (Header, 数据块起始偏移)。"""

    def need(n: int) -> None:
        if len(buf) < n:
            raise FormatError("容器截断: 头部不完整")

    need(4 + 1 + 1)
    if buf[:4] != MAGIC:
        raise FormatError("魔数不匹配")
    version = buf[4]
    if version != VERSION:
        raise FormatError(f"不支持的容器版本: {version}")
    (kid_len,) = struct.unpack(">B", buf[5:6])
    off = 6
    need(off + kid_len + SALT_LEN + 4 + 8 + 2)
    try:
        key_id = buf[off : off + kid_len].decode("ascii")
    except UnicodeDecodeError as exc:
        raise FormatError("key_id 不是 ASCII") from exc
    off += kid_len
    salt = buf[off : off + SALT_LEN]
    off += SALT_LEN
    block_size, pt_len = struct.unpack(">IQ", buf[off : off + 12])
    off += 12
    if block_size < MIN_BLOCK_SIZE:
        raise FormatError("block_size 非法")
    if pt_len > len(buf):
        raise FormatError("声明的明文长度超过容器大小")
    (wrapped_len,) = struct.unpack(">H", buf[off : off + 2])
    off += 2
    need(off + wrapped_len)
    wrapped = buf[off : off + wrapped_len]
    off += wrapped_len
    return (
        Header(version, key_id, salt, block_size, pt_len, wrapped),
        off,
    )


def _iter_block_lengths(total_len: int, block_size: int):
    full, last = divmod(total_len, block_size)
    for _ in range(full):
        yield block_size
    if last:
        yield last


def encrypt_blocks(plaintext: bytes, dek: bytes, salt: bytes, block_size: int) -> bytes:
    """用 DEK 分块加密明文。"""
    aes = AESGCM(dek)
    pt_len = len(plaintext)
    out = bytearray()
    index = 0
    for start in range(0, pt_len, block_size):
        if index > MAX_BLOCKS:
            raise FormatError("块数超过 2^32-1 限制")
        chunk = plaintext[start : start + block_size]
        out += aes.encrypt(_block_nonce(salt, index), chunk, _block_aad(index, pt_len))
        index += 1
    return bytes(out)


def decrypt_blocks(body: bytes, dek: bytes, salt: bytes, block_size: int, pt_len: int) -> bytes:
    """分块解密并在返回前完成全部认证; 任何一块失败即整体失败。

    先解密到内存中的临时缓冲, 全部块通过 GCM 认证后才返回,
    因此失败时不会向调用方泄露任何部分明文。
    """
    expected_blocks = (pt_len + block_size - 1) // block_size
    if expected_blocks > MAX_BLOCKS:
        raise FormatError("块数超限")
    aes = AESGCM(dek)
    out = bytearray()
    offset = 0
    for index, chunk_len in enumerate(_iter_block_lengths(pt_len, block_size)):
        end = offset + chunk_len + TAG_LEN
        if end > len(body):
            raise DecryptError("密文截断: 数据块不完整")
        blob = body[offset:end]
        nonce = _block_nonce(salt, index)
        try:
            chunk = aes.decrypt(nonce, blob, _block_aad(index, pt_len))
        except InvalidTag as exc:
            raise DecryptError(f"数据块 {index} 认证失败 (密钥错误 / 篡改 / 截断)") from exc
        out += chunk
        offset = end
    if offset != len(body):
        raise FormatError("容器尾部存在多余字节")
    if len(out) != pt_len:  # 由长度计算保证, 防御性校验
        raise DecryptError("解密长度与头部声明不符")
    return bytes(out)


def seal(master_key: bytes, key_id: str, plaintext: bytes, block_size: int = DEFAULT_BLOCK_SIZE) -> bytes:
    """加密一个对象: 生成独立 DEK, 包裹后产出完整容器字节。"""
    if block_size < MIN_BLOCK_SIZE:
        raise FormatError("block_size 必须 >= 1")
    if not 1 <= len(key_id) <= MAX_KEY_ID_LEN or not key_id.isascii():
        raise FormatError("key_id 必须是 1..32 字节 ASCII")
    salt = os.urandom(SALT_LEN)
    pt_len = len(plaintext)
    dek = new_key()
    wrapped = wrap_dek(master_key, key_id, dek, (VERSION, salt, block_size, pt_len))
    header = Header(VERSION, key_id, salt, block_size, pt_len, wrapped)
    return _encode_header(header) + encrypt_blocks(plaintext, dek, salt, block_size)


def open_container(container: bytes, resolve_master_key: Callable[[str], bytes]) -> bytes:
    """解密完整容器。

    resolve_master_key 按头部 key_id 取回主密钥; 取不到时抛 KeyUnavailableError。
    任何认证 / 格式错误都以异常形式整体失败, 不返回部分明文。
    """
    header, body_off = parse_header(container)
    master_key = resolve_master_key(header.key_id)
    dek = unwrap_dek(
        master_key,
        header.key_id,
        header.wrapped_dek,
        (header.version, header.salt, header.block_size, header.plaintext_len),
    )
    return decrypt_blocks(
        container[body_off:],
        dek,
        header.salt,
        header.block_size,
        header.plaintext_len,
    )


def rewrap_header(
    container: bytes,
    resolve_master_key: Callable[[str], bytes],
    new_master_key: bytes,
    new_key_id: str,
) -> bytes:
    """用新主密钥重新包裹 DEK; 数据块保持原样, 返回新容器字节。"""
    header, body_off = parse_header(container)
    old_key = resolve_master_key(header.key_id)
    dek = unwrap_dek(
        old_key,
        header.key_id,
        header.wrapped_dek,
        (header.version, header.salt, header.block_size, header.plaintext_len),
    )
    wrapped = wrap_dek(
        new_master_key,
        new_key_id,
        dek,
        (header.version, header.salt, header.block_size, header.plaintext_len),
    )
    new_header = Header(
        header.version,
        new_key_id,
        header.salt,
        header.block_size,
        header.plaintext_len,
        wrapped,
    )
    return _encode_header(new_header) + container[body_off:]
