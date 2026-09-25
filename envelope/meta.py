"""信封元数据（受保护头 + 数据密钥信封）。

磁盘文件 ``meta/<fid>.meta`` 布局::

    魔数        5 字节 ASCII：``ENVLP``
    uint32 大端 外层 JSON 字节长度
    外层 JSON    未加密但所有敏感操作都被 AEAD 绑定的路由信息：
                 - fid / kid：选择文件与主密钥
                 - envelope_nonce_b64 / envelope_b64：主密钥包裹 DEK 的结果
                 - header_nonce_b64 / header_b64：用 DEK 封装的受保护头
    uint32 大端 受保护头密封结果长度（仅用于快速切分，冗余于外层 JSON）

外层 JSON 本身不保密也不独立可信：它携带的 fid 会作为 AAD 参与"包裹 DEK"与
"封装受保护头"两次 AEAD 校验，kid 必须能在密钥库中找到对应主密钥。任何对外层
字段的篡改都会在 AEAD 认证阶段暴露；把整个 meta 文件搬到别的文件名下则会被
fid 校验拒绝（见 :mod:`envelope.service`）。

受保护头（用 DEK 加密、由 DEK 的 tag 认证）记录加密参数与规模，轮换主密钥时
**重新包裹 DEK、重新封装受保护头**（header_version 加 1），数据块文件不触碰。
"""

from __future__ import annotations

import base64
import json
import struct
from typing import BinaryIO

from . import crypto
from .errors import (
    AEADAuthenticationError,
    InvalidFormatError,
    TruncatedContainerError,
)

META_MAGIC = b"ENVLP"
_U32 = struct.Struct(">I")
PROTECTED_HEADER_VERSION = 1


def b64e(data: bytes) -> str:
    return base64.b64encode(data).decode("ascii")


def b64d(text: str) -> str | bytes:
    return base64.b64decode(text.encode("ascii"), validate=True)


def build_protected_header(
    *,
    file_id: str,
    plaintext_size: int,
    chunk_size: int,
    blocks: int,
    header_version: int,
) -> dict:
    """构造受保护头的明文字典。"""
    return {
        "v": PROTECTED_HEADER_VERSION,
        "alg": crypto.ALGORITHM,
        "fid": file_id,
        "plaintext_size": plaintext_size,
        "chunk_size": chunk_size,
        "blocks": blocks,
        "header_version": header_version,
    }


def seal_meta(
    *,
    file_id: str,
    kid: str,
    dek: bytes,
    master_key: bytes,
    plaintext_size: int,
    chunk_size: int,
    blocks: int,
    header_version: int,
    wrap_counter: int,
) -> bytes:
    """生成完整 meta 文件字节。

    步骤：
    1. 用主密钥 + 计数器 nonce + AAD(fid) 包裹随机 DEK；
    2. 用 DEK + 固定头 nonce + AAD(fid) 密封受保护头；
    3. 组装外层路由信息并序列化。
    """
    envelope_nonce = crypto.wrap_nonce(wrap_counter)
    envelope_ct = crypto.aead_seal(
        master_key, envelope_nonce, dek, crypto.wrap_aad(file_id=file_id)
    )

    header = build_protected_header(
        file_id=file_id,
        plaintext_size=plaintext_size,
        chunk_size=chunk_size,
        blocks=blocks,
        header_version=header_version,
    )
    header_nonce = crypto.header_nonce()
    header_ct = crypto.aead_seal(
        dek,
        header_nonce,
        crypto.canonical_json(header),
        crypto.header_aad(file_id=file_id),
    )

    outer = {
        "format": "envelope-meta",
        "v": 1,
        "fid": file_id,
        "kid": kid,
        "envelope_nonce_b64": b64e(envelope_nonce),
        "envelope_b64": b64e(envelope_ct),
        "header_nonce_b64": b64e(header_nonce),
        "header_b64": b64e(header_ct),
    }
    outer_bytes = crypto.canonical_json(outer)
    return (
        META_MAGIC
        + _U32.pack(len(outer_bytes))
        + outer_bytes
        + _U32.pack(len(header_ct))
    )


def _read_exact(fh: BinaryIO, size: int, what: str) -> bytes:
    data = fh.read(size)
    if len(data) != size:
        raise TruncatedContainerError(f"meta 文件被截断：{what} 不完整")
    return data


def read_meta_bytes(path_or_fh: str | BinaryIO) -> dict:
    """解析 meta 外层结构并做基本字段校验，返回包含解码字节的字典。

    返回键：``fid, kid, envelope_nonce, envelope_ct, header_nonce, header_ct``。
    本函数不做任何密钥操作，因此不验证真实性（真实性由 :func:`open_meta` 保证）。
    """
    if hasattr(path_or_fh, "read"):
        fh = path_or_fh
        close = False
    else:
        fh = open(path_or_fh, "rb")
        close = True
    try:
        magic = fh.read(len(META_MAGIC))
        if len(magic) < len(META_MAGIC):
            raise TruncatedContainerError("meta 文件被截断：缺少魔数")
        if magic != META_MAGIC:
            raise InvalidFormatError("魔数不匹配：这不是本服务的信封元数据文件")

        (outer_len,) = _U32.unpack(_read_exact(fh, _U32.size, "外层长度"))
        if outer_len == 0 or outer_len > 1024 * 1024:
            raise InvalidFormatError(f"外层长度非法：{outer_len}")
        outer_raw = _read_exact(fh, outer_len, "外层 JSON")
        try:
            outer = json.loads(outer_raw.decode("utf-8"))
        except (UnicodeDecodeError, json.JSONDecodeError) as exc:
            raise InvalidFormatError("外层 JSON 无法解析") from exc

        trailing = fh.read(_U32.size)
        if len(trailing) == _U32.size:
            (header_ct_len,) = _U32.unpack(trailing)
            extra = fh.read(1)
            if extra:
                raise InvalidFormatError("meta 文件尾部存在多余字节")
        else:
            header_ct_len = None  # 旧布局或被截断尾部标记，下面用内容长度即可

        for field in (
            "fid",
            "kid",
            "envelope_nonce_b64",
            "envelope_b64",
            "header_nonce_b64",
            "header_b64",
        ):
            if not isinstance(outer.get(field), str):
                raise InvalidFormatError(f"外层缺少或非法字段：{field}")

        try:
            envelope_nonce = b64d(outer["envelope_nonce_b64"])
            envelope_ct = b64d(outer["envelope_b64"])
            header_nonce = b64d(outer["header_nonce_b64"])
            header_ct = b64d(outer["header_b64"])
        except Exception as exc:  # base64 非法
            raise InvalidFormatError("外层 base64 字段无法解码") from exc

        if len(envelope_nonce) != crypto.NONCE_BYTES:
            raise InvalidFormatError("包裹 nonce 长度非法")
        if len(header_nonce) != crypto.NONCE_BYTES:
            raise InvalidFormatError("头 nonce 长度非法")
        if len(envelope_ct) < 16 or len(header_ct) < 16:
            raise InvalidFormatError("密封结果长度非法")
        if header_ct_len is not None and header_ct_len != len(header_ct):
            raise InvalidFormatError("尾部长度标记与受保护头不一致")

        return {
            "fid": outer["fid"],
            "kid": outer["kid"],
            "envelope_nonce": envelope_nonce,
            "envelope_ct": envelope_ct,
            "header_nonce": header_nonce,
            "header_ct": header_ct,
        }
    finally:
        if close:
            fh.close()


def validate_protected_header(
    header: dict,
    *,
    file_id: str,
) -> None:
    """对解密后的受保护头做一致性检查。"""
    if not isinstance(header, dict):
        raise InvalidFormatError("受保护头不是 JSON 对象")
    if header.get("v") != PROTECTED_HEADER_VERSION:
        raise InvalidFormatError(f"受保护头版本不受支持：{header.get('v')!r}")
    if header.get("alg") != crypto.ALGORITHM:
        raise InvalidFormatError(f"受保护头算法不受支持：{header.get('alg')!r}")
    if header.get("fid") != file_id:
        raise AEADAuthenticationError("受保护头中的文件 ID 与信封不匹配")
    size = header.get("plaintext_size")
    chunk = header.get("chunk_size")
    blocks = header.get("blocks")
    if not isinstance(size, int) or size < 0:
        raise InvalidFormatError("plaintext_size 非法")
    if not isinstance(chunk, int) or chunk <= 0:
        raise InvalidFormatError("chunk_size 非法")
    if not isinstance(blocks, int) or blocks < 0:
        raise InvalidFormatError("blocks 非法")
    if not isinstance(header.get("header_version"), int) or header["header_version"] < 1:
        raise InvalidFormatError("header_version 非法")
    from .blob import expected_blocks_for_size

    if expected_blocks_for_size(size, chunk) != blocks:
        from .errors import CorruptContainerError

        raise CorruptContainerError(
            "受保护头内部不一致：plaintext_size / chunk_size / blocks 对不上"
        )
