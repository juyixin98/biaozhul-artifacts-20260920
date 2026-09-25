"""固定块 AEAD 对象存储格式（版本 ENCRRNG1）。

文件布局（全部整数为大端）：

    偏移  长度          内容
    0     8             魔数 b"ENCRRNG1"
    8     1             版本 = 1
    9     1             盐长度 n_salt
    10    n_salt        对象盐 salt（随机）
    10+n  8             清单密文长度 m
    18+n  12            清单 nonce
    30+n  m             清单密文（含 GCM 16 字节标签）
    ...                 数据块记录（每块独立 nonce + 密文）

清单明文为 JSON，含：object_id、block_size、plaintext_len、n_blocks，
由 manifest_key 用 AES-256-GCM 加密（AAD 绑定魔数/版本/盐）。

每个数据块由 block_key 加密：
    12 字节随机 nonce + 4 字节密文长度 + 密文（含 16 字节 GCM 标签）。
块的 AAD 为长度前缀编码，绑定对象标识、块索引、块在文件中的字节偏移、
逻辑块大小、本块明文长度、对象总明文长度、块总数——
因此跨对象 / 跨位置的密文交换以及截断、拼接都会在认证阶段失败。

密钥体系：主密钥（本地随机生成，不接入任何生产账号）
    manifest_key = HKDF(salt, "manifest", master_key)
    block_key    = HKDF(salt, "blocks",  master_key)

读取保证：read_range 先对范围内的*所有*块逐个解密并验证，
全部通过后才返回拼接好的明文；任何一块认证失败即抛 IntegrityError，
不返回任何未经认证的字节。
"""

from __future__ import annotations

import json
import os
import struct
from dataclasses import dataclass

from cryptography.exceptions import InvalidTag
from cryptography.hazmat.primitives.ciphers.aead import AESGCM
from cryptography.hazmat.primitives.kdf.hkdf import HKDF
from cryptography.hazmat.primitives.hashes import SHA256

from .errors import IntegrityError

MAGIC = b"ENCRRNG1"
VERSION = 1
SALT_LEN = 16
NONCE_LEN = 12
KEY_LEN = 32  # AES-256
TAG_LEN = 16

DEFAULT_BLOCK_SIZE = 64 * 1024  # 64 KiB 逻辑明文块
MAX_PLAINTEXT_LEN = 4 * 1024 * 1024 * 1024  # 4 GiB，防止损坏长度字段导致巨量分配


def generate_master_key() -> bytes:
    """生成 32 字节随机主密钥（测试 / 本地使用）。"""
    return os.urandom(KEY_LEN)


def _derive_key(salt: bytes, info_label: bytes, master_key: bytes) -> bytes:
    return HKDF(
        algorithm=SHA256(),
        length=KEY_LEN,
        salt=salt,
        info=b"encrypted-range-store/v1/" + info_label,
    ).derive(master_key)


def _aad_length_prefixed(*fields: tuple[bytes, object]) -> bytes:
    """长度前缀编码 AAD，避免任何分隔符 / 字段拼接歧义。

    每个字段为 (标签, 值)，整数值按 8 字节大端编码，字节串按原内容，
    两者都带 2 字节标签长度前缀与 4 字节内容长度前缀。
    """
    out = bytearray()
    for label, value in fields:
        if isinstance(value, int):
            payload = struct.pack(">Q", value)
        else:
            payload = bytes(value)
        out += struct.pack(">H", len(label))
        out += label
        out += struct.pack(">I", len(payload))
        out += payload
    return bytes(out)


@dataclass(frozen=True)
class Manifest:
    """明文清单（自身经过 GCM 加密认证）。"""

    object_id: str
    block_size: int
    plaintext_len: int
    n_blocks: int

    def to_json_bytes(self) -> bytes:
        return json.dumps(
            {
                "object_id": self.object_id,
                "block_size": self.block_size,
                "plaintext_len": self.plaintext_len,
                "n_blocks": self.n_blocks,
            },
            separators=(",", ":"),
            sort_keys=True,
        ).encode("utf-8")

    @classmethod
    def from_json_bytes(cls, data: bytes) -> "Manifest":
        try:
            obj = json.loads(data.decode("utf-8"))
        except (UnicodeDecodeError, json.JSONDecodeError) as exc:
            raise IntegrityError("清单明文不是合法 JSON") from exc
        if not isinstance(obj, dict) or set(obj.keys()) != {
            "object_id",
            "block_size",
            "plaintext_len",
            "n_blocks",
        }:
            raise IntegrityError("清单字段集合不匹配")
        object_id = obj["object_id"]
        block_size = obj["block_size"]
        plaintext_len = obj["plaintext_len"]
        n_blocks = obj["n_blocks"]
        if not isinstance(object_id, str) or not object_id:
            raise IntegrityError("清单中的 object_id 非法")
        if not all(isinstance(v, int) and not isinstance(v, bool) for v in
                   (block_size, plaintext_len, n_blocks)):
            raise IntegrityError("清单中的长度字段不是整数")
        if block_size <= 0 or plaintext_len < 0 or n_blocks < 0:
            raise IntegrityError("清单中的长度字段为负或为零")
        if plaintext_len > MAX_PLAINTEXT_LEN:
            raise IntegrityError("清单声明的明文长度超出上限")
        expected_blocks = (plaintext_len + block_size - 1) // block_size
        if expected_blocks != n_blocks:
            raise IntegrityError("清单中块数与明文长度 / 块大小不一致")
        # 空对象必须恰好 0 块；非空对象最后一块为短块。
        if n_blocks > 0:
            last_len = plaintext_len - (n_blocks - 1) * block_size
            if not (0 < last_len <= block_size):
                raise IntegrityError("清单中最后一块长度非法")
        return cls(object_id, block_size, plaintext_len, n_blocks)


@dataclass(frozen=True)
class BlockLocation:
    index: int
    offset: int          # 该块 nonce 在文件中的字节偏移
    nonce: bytes
    ct_len: int          # 含标签


class ObjectReader:
    """已打开对象的只读视图：打开时即验证清单，读块时逐块认证。"""

    def __init__(
        self,
        f,
        object_id: str,
        manifest: Manifest,
        block_key: bytes,
        blocks: list[BlockLocation],
    ):
        self._f = f
        self._object_id = object_id
        self._manifest = manifest
        self._aesgcm = AESGCM(block_key)
        self._blocks = blocks

    @property
    def object_id(self) -> str:
        return self._object_id

    @property
    def plaintext_len(self) -> int:
        return self._manifest.plaintext_len

    @property
    def block_size(self) -> int:
        return self._manifest.block_size

    def _decrypt_block(self, location: BlockLocation, plaintext_len: int) -> bytes:
        self._f.seek(location.offset + NONCE_LEN + 4)
        ct = _read_exact(self._f, location.ct_len)
        aad = _aad_length_prefixed(
            (b"magic", MAGIC),
            (b"version", VERSION),
            (b"object_id", self._object_id.encode("utf-8")),
            (b"block_index", location.index),
            (b"block_file_offset", location.offset),
            (b"block_size", self._manifest.block_size),
            (b"plaintext_len_in_block", plaintext_len),
            (b"object_plaintext_len", self._manifest.plaintext_len),
            (b"n_blocks", self._manifest.n_blocks),
        )
        try:
            pt = self._aesgcm.decrypt(location.nonce, ct, aad)
        except InvalidTag as exc:
            raise IntegrityError(
                f"第 {location.index} 块 AEAD 认证失败"
            ) from exc
        if len(pt) != plaintext_len:
            # 正常情况下 AAD 已绑定长度，GCM 不可能放行长度不符的密文；
            # 这是纵深防御。
            raise IntegrityError(f"第 {location.index} 块解密长度不符")
        return pt

    def read_block(self, index: int) -> bytes:
        """读取并验证单个逻辑块（最后一块可能是短块）。"""
        if not 0 <= index < self._manifest.n_blocks:
            raise IntegrityError("块索引越界")
        start = index * self._manifest.block_size
        plaintext_len = min(
            self._manifest.block_size,
            self._manifest.plaintext_len - start,
        )
        return self._decrypt_block(self._blocks[index], plaintext_len)

    def read_range(self, start: int, end_exclusive: int) -> bytes:
        """返回明文区间 [start, end_exclusive)。

        先解密并验证范围内所有块；任一失败则整体失败，不返回部分明文。
        """
        if start < 0 or end_exclusive < start:
            raise IntegrityError("内部范围参数非法")
        if end_exclusive > self._manifest.plaintext_len:
            raise IntegrityError("内部范围超出明文长度")
        if start == end_exclusive:
            return b""
        bs = self._manifest.block_size
        first_block = start // bs
        last_block = (end_exclusive - 1) // bs
        pieces: list[bytes] = []
        # 全部块先解密验证通过，最后再做切片拼接。
        for i in range(first_block, last_block + 1):
            pieces.append(self.read_block(i))
        lo = start - first_block * bs
        hi = end_exclusive - first_block * bs
        return b"".join(pieces)[lo:hi]

    def close(self) -> None:
        self._f.close()

    def __enter__(self) -> "ObjectReader":
        return self

    def __exit__(self, *exc) -> None:
        self.close()


def _read_exact(f, n: int) -> bytes:
    data = f.read(n)
    if len(data) != n:
        raise IntegrityError("文件截断：期望读取 %d 字节，实际 %d" % (n, len(data)))
    return data


def write_object(
    path: str,
    object_id: str,
    plaintext: bytes,
    master_key: bytes,
    block_size: int = DEFAULT_BLOCK_SIZE,
) -> None:
    """把明文以固定块 AEAD 格式原子写入 path（先写临时文件再 rename）。"""
    if block_size <= 0:
        raise ValueError("block_size 必须为正数")
    if len(master_key) != KEY_LEN:
        raise ValueError("master_key 必须为 32 字节")
    plaintext_len = len(plaintext)
    if plaintext_len > MAX_PLAINTEXT_LEN:
        raise ValueError("明文超出大小上限")
    n_blocks = (plaintext_len + block_size - 1) // block_size
    manifest = Manifest(object_id, block_size, plaintext_len, n_blocks)

    salt = os.urandom(SALT_LEN)
    manifest_key = _derive_key(salt, b"manifest", master_key)
    block_key = _derive_key(salt, b"blocks", master_key)
    block_aead = AESGCM(block_key)

    tmp_path = path + ".tmp"
    with open(tmp_path, "wb") as f:
        f.write(MAGIC)
        f.write(bytes([VERSION]))
        f.write(bytes([len(salt)]))
        f.write(salt)

        manifest_aad = _aad_length_prefixed(
            (b"magic", MAGIC),
            (b"version", VERSION),
            (b"salt", salt),
            (b"section", b"manifest"),
        )
        manifest_nonce = os.urandom(NONCE_LEN)
        manifest_ct = AESGCM(manifest_key).encrypt(
            manifest_nonce, manifest.to_json_bytes(), manifest_aad
        )
        f.write(struct.pack(">Q", len(manifest_ct)))
        f.write(manifest_nonce)
        f.write(manifest_ct)

        for i in range(n_blocks):
            chunk = plaintext[i * block_size:(i + 1) * block_size]
            block_offset = f.tell()
            nonce = os.urandom(NONCE_LEN)
            aad = _aad_length_prefixed(
                (b"magic", MAGIC),
                (b"version", VERSION),
                (b"object_id", object_id.encode("utf-8")),
                (b"block_index", i),
                (b"block_file_offset", block_offset),
                (b"block_size", block_size),
                (b"plaintext_len_in_block", len(chunk)),
                (b"object_plaintext_len", plaintext_len),
                (b"n_blocks", n_blocks),
            )
            ct = block_aead.encrypt(nonce, chunk, aad)
            f.write(nonce)
            f.write(struct.pack(">I", len(ct)))
            f.write(ct)
    os.replace(tmp_path, path)


def open_object(path: str, object_id: str, master_key: bytes) -> ObjectReader:
    """打开对象：验证魔数、版本、清单 GCM 标签与清单内部一致性，
    并遍历块头部建立偏移索引（同时检测截断 / 未认证尾部字节）。

    object_id 作为期望标识参与块 AAD 校验；清单中的 id 与之不符即失败。
    """
    if len(master_key) != KEY_LEN:
        raise ValueError("master_key 必须为 32 字节")
    with open(path, "rb") as f:
        header = _read_exact(f, 10)
        if header[:8] != MAGIC:
            raise IntegrityError("魔数不匹配（不是本格式文件或已被替换）")
        if header[8] != VERSION:
            raise IntegrityError("不支持的格式版本：%r" % header[8])
        salt_len = header[9]
        if salt_len != SALT_LEN:
            raise IntegrityError("盐长度字段非法")
        salt = _read_exact(f, salt_len)

        (m_len,) = struct.unpack(">Q", _read_exact(f, 8))
        if m_len > 1_000_000:
            raise IntegrityError("清单密文长度异常")
        manifest_nonce = _read_exact(f, NONCE_LEN)
        manifest_ct = _read_exact(f, m_len)

        manifest_key = _derive_key(salt, b"manifest", master_key)
        manifest_aad = _aad_length_prefixed(
            (b"magic", MAGIC),
            (b"version", VERSION),
            (b"salt", salt),
            (b"section", b"manifest"),
        )
        try:
            manifest_pt = AESGCM(manifest_key).decrypt(
                manifest_nonce, manifest_ct, manifest_aad
            )
        except InvalidTag as exc:
            raise IntegrityError("清单 AEAD 认证失败") from exc

        manifest = Manifest.from_json_bytes(manifest_pt)
        if manifest.object_id != object_id:
            raise IntegrityError("清单绑定的对象标识与请求对象不符（检测到密文交换）")

        blocks: list[BlockLocation] = []
        for i in range(manifest.n_blocks):
            offset = f.tell()
            nonce = _read_exact(f, NONCE_LEN)
            (ct_len,) = struct.unpack(">I", _read_exact(f, 4))
            if ct_len <= TAG_LEN or ct_len > manifest.block_size + TAG_LEN:
                raise IntegrityError(f"第 {i} 块密文长度字段非法")
            f.seek(ct_len, os.SEEK_CUR)
            blocks.append(BlockLocation(i, offset, nonce, ct_len))
        trailing = f.read(1)
        if trailing:
            raise IntegrityError("文件存在未认证的尾部字节（检测到拼接 / 扩展）")

    f2 = open(path, "rb")
    return ObjectReader(f2, object_id, manifest,
                        _derive_key(salt, b"blocks", master_key), blocks)
