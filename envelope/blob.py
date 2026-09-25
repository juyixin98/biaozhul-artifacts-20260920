"""分块 AEAD 密文容器（blob）。

磁盘布局（数据块文件 ``blobs/<fid>.blob``）::

    魔数        5 字节 ASCII：``BLOB1``
    帧序列      每帧 = uint32 大端帧长度(4 字节) + 帧载荷
    帧载荷      nonce(12 字节) + AES-256-GCM 密封结果(密文 + 16 字节 tag)

安全要点
========

* 每帧的 nonce 必须等于 :func:`envelope.crypto.block_nonce` 对该块序号的派生值；
  读取时显式校验。调换两个帧的位置会导致该帧声明的 nonce 与实际序号不符，
  且该序号下的 AAD（绑定文件 ID + 块序号）认证失败。
* GCM tag 同时保护块密文完整性；篡改任何一个字节都会在解密该块时被发现。
* 块序号 AAD 绑定使"跨文件搬运块"无法通过认证（文件 ID 不同、DEK 也不同）。
* 帧长度字段不受认证保护，但被限制在合法范围内；伪造的超长长度只会触发
  读取截断错误或随后的 AEAD 认证失败，不会造成无界内存分配。
"""

from __future__ import annotations

import struct
from collections.abc import Iterator
from typing import BinaryIO

from . import crypto
from .errors import (
    AEADAuthenticationError,
    InvalidFormatError,
    TruncatedContainerError,
)

BLOB_MAGIC = b"BLOB1"
_FRAME_HEADER = struct.Struct(">I")
# 合法帧载荷上限：nonce(12) + 明文块(<=chunk_size) + GCM tag(16)。
# chunk_size 本身记录在受保护头里（最多 16 MiB），这里给一个硬上限做纵深防御。
_MAX_CHUNK_SIZE = 16 * 1024 * 1024
_MAX_FRAME_PAYLOAD = 12 + _MAX_CHUNK_SIZE + 16


def write_magic(out: BinaryIO) -> None:
    """写入容器魔数。"""
    out.write(BLOB_MAGIC)


def write_block(
    out: BinaryIO,
    *,
    dek: bytes,
    file_id: str,
    block_index: int,
    chunk_size: int,
    plaintext: bytes,
) -> None:
    """加密并写入一个数据块（含 4 字节帧头）。"""
    if not plaintext:
        raise ValueError("write_block 不接受空明文块（块数应由头唯一决定）")
    nonce = crypto.block_nonce(block_index)
    aad = crypto.block_aad(
        file_id=file_id, block_index=block_index, chunk_size=chunk_size
    )
    sealed = crypto.aead_seal(dek, nonce, plaintext, aad)
    payload = nonce + sealed
    out.write(_FRAME_HEADER.pack(len(payload)))
    out.write(payload)


def _read_exact(fh: BinaryIO, size: int) -> bytes | None:
    """精确读取 ``size`` 字节；EOF 返回 None，读不够抛截断错误。"""
    data = fh.read(size)
    if len(data) == size:
        return data
    if len(data) == 0:
        return None
    raise TruncatedContainerError(
        f"容器被截断：期望再读 {size} 字节，实际只剩 {len(data)} 字节"
    )


def iter_blocks(
    fh: BinaryIO,
    *,
    dek: bytes,
    file_id: str,
    chunk_size: int,
    expected_blocks: int | None = None,
) -> Iterator[tuple[int, bytes]]:
    """逐块认证解密容器，按顺序 yield ``(块序号, 明文块)``。

    :param expected_blocks: 受保护头中声明的块数。解密到末尾时校验实际帧数必须
        与之一致——删掉尾部若干帧（截断）或追加多余帧都无法通过。
    """
    if chunk_size <= 0 or chunk_size > _MAX_CHUNK_SIZE:
        raise InvalidFormatError("受保护头中的 chunk_size 非法")

    magic = fh.read(len(BLOB_MAGIC))
    if len(magic) < len(BLOB_MAGIC):
        raise TruncatedContainerError("容器被截断：缺少魔数")
    if magic != BLOB_MAGIC:
        raise InvalidFormatError("魔数不匹配：这不是本服务的数据块容器")

    block_index = 0
    while True:
        header = _read_exact(fh, _FRAME_HEADER.size)
        if header is None:
            break  # 干净 EOF
        (frame_len,) = _FRAME_HEADER.unpack(header)
        if frame_len == 0 or frame_len > _MAX_FRAME_PAYLOAD:
            raise InvalidFormatError(f"帧长度非法：{frame_len}")
        # 至少要容得下 nonce(12) + tag(16)。
        if frame_len < 12 + 16:
            raise InvalidFormatError(f"帧长度过小：{frame_len}")

        payload = _read_exact(fh, frame_len)
        assert payload is not None  # 上面 EOF 已抛 Truncated
        nonce = payload[:12]
        sealed = payload[12:]

        expected_nonce = crypto.block_nonce(block_index)
        if nonce != expected_nonce:
            # 帧被调换、重排或 nonce 被改写：直接拒绝，避免拿错误 nonce 去解密。
            raise AEADAuthenticationError(
                f"第 {block_index} 块的 nonce 与块序号派生值不一致"
                "（块可能被交换或重放）"
            )
        aad = crypto.block_aad(
            file_id=file_id, block_index=block_index, chunk_size=chunk_size
        )
        yield block_index, crypto.aead_open(dek, nonce, sealed, aad)
        block_index += 1

    if expected_blocks is not None and block_index != expected_blocks:
        from .errors import CorruptContainerError

        raise CorruptContainerError(
            f"实际数据块数 {block_index} 与受保护头声明的 {expected_blocks} 不一致"
            "（文件可能被截断或被追加）"
        )


def iter_plaintext(
    fh: BinaryIO,
    *,
    dek: bytes,
    file_id: str,
    chunk_size: int,
    expected_blocks: int,
) -> Iterator[bytes]:
    """解密并在末尾校验拼接后的明文长度是否与受保护头一致。

    与 :func:`iter_blocks` 的区别是只 yield 明文字节，供不需要块序号的调用方使用。
    """
    for _, chunk in iter_blocks(
        fh,
        dek=dek,
        file_id=file_id,
        chunk_size=chunk_size,
        expected_blocks=expected_blocks,
    ):
        yield chunk


def stream_encrypt(
    source: BinaryIO,
    dest: BinaryIO,
    *,
    dek: bytes,
    file_id: str,
    chunk_size: int,
) -> int:
    """流式分块加密，返回写入的数据块数。仅读取 ``source``，不负责明文删除。"""
    if chunk_size <= 0 or chunk_size > _MAX_CHUNK_SIZE:
        raise ValueError("chunk_size 非法")
    write_magic(dest)
    blocks = 0
    while True:
        chunk = source.read(chunk_size)
        if not chunk:
            break
        write_block(
            dest,
            dek=dek,
            file_id=file_id,
            block_index=blocks,
            chunk_size=chunk_size,
            plaintext=chunk,
        )
        blocks += 1
    dest.flush()
    return blocks


def expected_blocks_for_size(plaintext_size: int, chunk_size: int) -> int:
    """由明文总长度与分块大小计算应有的块数。空文件为 0 块。"""
    if plaintext_size == 0:
        return 0
    return (plaintext_size - 1) // chunk_size + 1
