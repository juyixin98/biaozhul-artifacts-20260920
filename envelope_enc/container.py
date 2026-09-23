"""分块 AEAD 文件容器：加密、解密、主密钥轮换。

容器磁盘布局（ENVENC v1，大端）::

    偏移      内容
    0         MAGIC   = b"ENVENC"（6 字节）
    6         VERSION = 0x01（1 字节）
    7         HEADER_LEN（uint32，4 字节）
    11        HEADER_JSON（UTF-8、sort_keys 的规范 JSON，长度 = HEADER_LEN）
    11+HL     块序列，每块 = NONCE(12) || AES-GCM(明文块)（密文含 16 字节标签）

头部 JSON 字段：
    v                 格式版本（1）
    file_id           文件唯一标识（随机生成），同时参与头 AAD 与每块 AAD
    kid               包裹本文件 DEK 的主密钥 id
    chunk_size        普通块字节数（最后一块可更短）
    plaintext_size    明文总长度（用于严格的截断/长度校验）
    chunk_count       块数（空文件为 0）
    nonce_prefix_b64  4 字节随机块 nonce 前缀
    wrap_nonce_b64    包裹 DEK 用的 12 字节随机 nonce
    wrapped_dek_b64   主密钥 AES-GCM 包裹的数据密钥
    hdr_nonce_b64     头部认证 nonce
    hdr_tag_b64       头部认证标签（GCM 对空明文输出的 16 字节标签）

安全性质：
- 头部篡改 / 块篡改 / 换文件的块 / 同文件换块顺序 → 认证失败；
- 截断（头不完整、块 nonce 不完整、块密文不完整、块数不足）→ TruncatedContainer；
- 轮换主密钥只重包 DEK 并重算头部标签，密文块字节原样复制，绝不重新加密数据；
- 加密过程不产生明文临时文件；解密结果先写 0600 的 ``.part``，
  全部块认证通过后才原子改名，任何失败立即删除。
"""

from __future__ import annotations

import base64
import io
import json
import os
import secrets
import struct
from dataclasses import dataclass
from typing import BinaryIO, Callable, Iterable

from cryptography.exceptions import InvalidTag

from .crypto import (
    KEY_LEN,
    NONCE_LEN,
    NONCE_PREFIX_LEN,
    TAG_LEN,
    chunk_aad,
    chunk_nonce,
    header_aad,
    open_sealed,
    random_key,
    random_nonce,
    random_nonce_prefix,
    seal,
    wrap_aad,
)

MAGIC = b"ENVENC"
VERSION = 1
_HEADER_LEN = struct.Struct(">I")
DEFAULT_CHUNK_SIZE = 64 * 1024  # 64 KiB
# 头部不允许超过 1 MiB（实际只有几百字节），用于防畸形长度
_HEADER_MAX = 1 << 20

# 临时文件后缀（加密产物 / 解密产物 / 轮换临时）
_ENC_TMP_SUFFIX = ".enc.tmp"
_DEC_TMP_SUFFIX = ".part"
_ROT_TMP_SUFFIX = ".rot.tmp"
_FILE_MODE = 0o600


class ContainerError(Exception):
    """容器相关错误的基类。"""


class KeyNotFoundError(ContainerError):
    """头部 kid 对应的主密钥在密钥环中找不到（无法解密/轮换）。"""


class TruncatedContainer(ContainerError):
    """容器被截断：文件比头部声明的短。"""


class IntegrityError(ContainerError):
    """完整性/真实性校验失败：魔数、版本、头部或 AEAD 标签不合法。"""


@dataclass(frozen=True)
class RotateOutcome:
    old_kid: str
    new_kid: str
    skipped: bool  # True 表示文件本就由新主密钥包裹，未做改动


@dataclass
class FileRotateResult:
    path: str
    outcome: RotateOutcome | None = None
    error: str | None = None

    @property
    def ok(self) -> bool:
        return self.error is None


# ====================================================================== 加密
def encrypt_bytes(
    plaintext: bytes,
    keyring,
    *,
    file_id: str | None = None,
    chunk_size: int = DEFAULT_CHUNK_SIZE,
) -> bytes:
    """把明文字节加密成容器字节串，DEK 由密钥环当前 active 主密钥包裹。"""
    dst = io.BytesIO()
    _seal_stream(
        io.BytesIO(plaintext),
        dst,
        size=len(plaintext),
        master=keyring.active(),
        file_id=file_id or _new_file_id(),
        chunk_size=chunk_size,
    )
    return dst.getvalue()


def encrypt_file(
    src_path: str,
    dst_path: str,
    keyring,
    *,
    file_id: str | None = None,
    chunk_size: int = DEFAULT_CHUNK_SIZE,
) -> str:
    """加密磁盘文件。

    源文件以只读方式流式读取，密文先写 ``dst_path + .enc.tmp``（0600），
    fsync 后原子改名——全过程不产生任何明文临时文件。
    """
    _require_positive_chunk(chunk_size)
    tmp_path = dst_path + _ENC_TMP_SUFFIX
    _discard_stale(tmp_path)
    size = os.path.getsize(src_path)
    fd = _open_tmp(tmp_path)
    try:
        with os.fdopen(fd, "wb") as dst, open(src_path, "rb") as src:
            _seal_stream(
                src,
                dst,
                size=size,
                master=keyring.active(),
                file_id=file_id or _new_file_id(),
                chunk_size=chunk_size,
            )
            dst.flush()
            os.fsync(dst.fileno())
        os.replace(tmp_path, dst_path)
        _fsync_dir_of(dst_path)
    except BaseException:
        _unlink_quiet(tmp_path)
        raise
    return dst_path


def _seal_stream(
    src: BinaryIO,
    dst: BinaryIO,
    *,
    size: int,
    master,
    file_id: str,
    chunk_size: int,
) -> dict:
    """加密核心：读明文流、写完整容器流，返回头部字段字典。"""
    _require_positive_chunk(chunk_size)
    if size < 0:
        raise ValueError("size 不能为负")

    dek = random_key()                 # 每文件一把数据密钥
    prefix = random_nonce_prefix()    # 每文件一个随机 nonce 前缀
    chunk_count = 0 if size == 0 else (size - 1) // chunk_size + 1

    wrap_nonce = random_nonce()
    wrapped_dek = seal(master.key, wrap_nonce, dek, wrap_aad(master.kid))

    # chunk_count 由 size 精确算出，因此头部可以先于密文块写出，
    # 不需要 seek 回跳，也不需要把大文件整块缓冲在内存中。
    header = _build_header(
        file_id=file_id,
        kid=master.kid,
        chunk_size=chunk_size,
        plaintext_size=size,
        chunk_count=chunk_count,
        prefix=prefix,
        wrap_nonce=wrap_nonce,
        wrapped_dek=wrapped_dek,
    )
    hdr_aad = _header_aad_from_fields(
        file_id=file_id,
        kid=master.kid,
        chunk_size=chunk_size,
        plaintext_size=size,
        chunk_count=chunk_count,
        prefix=prefix,
    )
    hdr_nonce = random_nonce()
    hdr_tag = seal(dek, hdr_nonce, b"", hdr_aad)
    header["hdr_nonce_b64"] = _b64e(hdr_nonce)
    header["hdr_tag_b64"] = _b64e(hdr_tag)
    header_bytes = _canonical_header_bytes(header)

    dst.write(MAGIC)
    dst.write(bytes([VERSION]))
    dst.write(_HEADER_LEN.pack(len(header_bytes)))
    dst.write(header_bytes)

    # 分块加密；计数器从 0 开始，在"每文件独立 DEK + 随机前缀"作用域内唯一
    written = 0
    index = 0
    while True:
        block = src.read(chunk_size)
        if not block:
            break
        if index >= chunk_count:
            # 读取过程中源数据比声明 size 更长（文件被并发追加等异常情况）
            raise IntegrityError("明文实际长度超过声明长度，拒绝写出容器")
        nonce = chunk_nonce(prefix, index)
        aad = chunk_aad(
            file_id=file_id, chunk_index=index, plaintext_len=len(block)
        )
        dst.write(nonce)
        dst.write(seal(dek, nonce, block, aad))
        written += len(block)
        index += 1

    if written != size or index != chunk_count:
        # 声明长度与实际读取不一致（例如源文件在加密期间被改动）
        raise IntegrityError("明文长度声明与实际读取不一致，拒绝写出容器")
    return header


# ====================================================================== 解密
def decrypt_bytes(blob: bytes, keyring) -> bytes:
    """解密容器字节串，头部 kid 指向的主密钥（active 或 retired）均可。"""
    dst = io.BytesIO()
    with io.BytesIO(blob) as src:
        header = _open_stream(src, dst, get_key=_key_getter(keyring))
    return dst.getvalue()


def decrypt_file(src_path: str, dst_path: str, keyring) -> str:
    """解密磁盘文件到 ``dst_path``。

    明文先写 ``dst_path + .part``（0600 临时名），**所有块认证通过后**
    才原子改名为 dst_path；任何校验失败都删除 .part，绝不留下半截明文。
    """
    tmp_path = dst_path + _DEC_TMP_SUFFIX
    _discard_stale(tmp_path)
    fd = _open_tmp(tmp_path)
    try:
        with os.fdopen(fd, "wb") as dst, open(src_path, "rb") as src:
            _open_stream(src, dst, get_key=_key_getter(keyring))
            dst.flush()
            os.fsync(dst.fileno())
        os.replace(tmp_path, dst_path)
        _fsync_dir_of(dst_path)
    except BaseException:
        _unlink_quiet(tmp_path)
        raise
    return dst_path


def _key_getter(keyring) -> Callable[[str], bytes]:
    def get_key(kid: str) -> bytes:
        try:
            return keyring.get_for_unwrap(kid).key
        except Exception as exc:  # KeyringError -> 统一成容器域异常
            raise KeyNotFoundError(f"密钥环中找不到主密钥 {kid}") from exc

    return get_key


def _open_stream(src: BinaryIO, dst: BinaryIO, *, get_key) -> dict:
    """解密核心：读容器流、验头、逐块认证解密写入 dst，返回头部字典。"""
    header = _read_and_parse_header(src)
    kid = header["kid"]
    master_key = get_key(kid)

    wrap_nonce = _b64d(header, "wrap_nonce_b64", NONCE_LEN)
    wrapped_dek = _b64d(header, "wrapped_dek_b64", KEY_LEN + TAG_LEN)
    try:
        dek = open_sealed(master_key, wrap_nonce, wrapped_dek, wrap_aad(kid))
    except InvalidTag:
        raise IntegrityError("数据密钥包裹层认证失败（主密钥不匹配或包裹数据被篡改）")

    hdr_nonce = _b64d(header, "hdr_nonce_b64", NONCE_LEN)
    hdr_tag = _b64d(header, "hdr_tag_b64", TAG_LEN)
    try:
        open_sealed(dek, hdr_nonce, hdr_tag, _header_aad(header))
    except InvalidTag:
        raise IntegrityError("文件头认证失败（头部字段被篡改）")

    prefix = _b64d(header, "nonce_prefix_b64", NONCE_PREFIX_LEN)
    file_id = header["file_id"]
    chunk_size = header["chunk_size"]
    chunk_count = header["chunk_count"]
    plaintext_size = header["plaintext_size"]

    for index in range(chunk_count):
        expected_len = min(chunk_size, plaintext_size - index * chunk_size)
        if expected_len <= 0:
            # plaintext_size 与 chunk_count 不自洽（头部虽过认证也做防御性检查）
            raise IntegrityError("头部长度字段自相矛盾")
        nonce = _read_exact(src, NONCE_LEN, f"第 {index} 块 nonce")
        ct = _read_exact(src, expected_len + TAG_LEN, f"第 {index} 块密文")
        # 注意：先按头部声明的长度读全整块再认证。nonce 前缀+计数器必须
        # 与加密时一致，否则 GCM 认证失败。
        aad = chunk_aad(
            file_id=file_id, chunk_index=index, plaintext_len=expected_len
        )
        try:
            block = open_sealed(dek, nonce, ct, aad)
        except InvalidTag:
            raise IntegrityError(
                f"第 {index} 块认证失败：块被篡改、块顺序被调换或来自其他文件"
            )
        dst.write(block)

    # 尾部多余数据也是一种结构异常（防止把两个容器拼接等构造）
    tail = src.read(1)
    if tail:
        raise IntegrityError("容器在声明的最后一块之后仍有多余字节")
    return header


# ====================================================================== 轮换
def rotate_bytes(blob: bytes, keyring, *, new_kid: str | None = None) -> bytes:
    """把容器重包到新主密钥，密文块原样保留，返回新容器字节串。"""
    new_master, header, dek = _prepare_rotate(io.BytesIO(blob), keyring, new_kid)
    if new_master.kid == header["kid"]:
        return blob  # 已由目标主密钥包裹：幂等 no-op
    out = io.BytesIO()
    _write_rotated(io.BytesIO(blob), out, header=header, dek=dek, new_master=new_master)
    return out.getvalue()


def rotate_file(
    path: str,
    keyring,
    *,
    new_kid: str | None = None,
    crash_after: str | None = None,
) -> RotateOutcome:
    """就地轮换磁盘文件的主密钥。

    - 只重包 DEK + 重算头部标签，密文块字节不重读解密、不重新加密；
    - 先写 ``path + .rot.tmp``，fsync 后原子替换，中断不会产生混合状态文件；
    - crash_after="tmp_written" 仅供测试：在临时文件落盘后、改名前模拟崩溃。

    中断语义：
    - 替换前崩溃 → 原文件完好（仍是旧 kid），残留 .rot.tmp 下次自动清理；
    - os.replace 是原子的，不存在"半个轮换"的结果文件。
    """
    tmp_path = path + _ROT_TMP_SUFFIX
    _discard_stale(tmp_path)
    with open(path, "rb") as f:
        new_master, header, dek = _prepare_rotate(f, keyring, new_kid)

    skipped = new_master.kid == header["kid"]
    if not skipped:
        fd = _open_tmp(tmp_path)
        try:
            with os.fdopen(fd, "wb") as dst, open(path, "rb") as src:
                _write_rotated(src, dst, header=header, dek=dek, new_master=new_master)
                dst.flush()
                os.fsync(dst.fileno())
            if crash_after == "tmp_written":
                raise _SimulatedCrash("模拟轮换在临时文件写好后崩溃")
            os.replace(tmp_path, path)
            _fsync_dir_of(path)
        except BaseException:
            _unlink_quiet(tmp_path)
            raise
    return RotateOutcome(old_kid=header["kid"], new_kid=new_master.kid, skipped=skipped)


def rotate_many(
    paths: Iterable[str],
    keyring,
    *,
    new_kid: str | None = None,
) -> list[FileRotateResult]:
    """批量轮换；单个文件失败不影响其余文件，结果逐条返回。

    已用新主密钥包裹的文件记为 skipped（幂等），因此该接口天然可重入：
    轮换中断后再次执行，已完成的文件跳过、未完成的继续。
    """
    results: list[FileRotateResult] = []
    for path in paths:
        try:
            outcome = rotate_file(path, keyring, new_kid=new_kid)
            results.append(FileRotateResult(path=path, outcome=outcome))
        except ContainerError as exc:
            results.append(FileRotateResult(path=path, error=str(exc)))
    return results


def _prepare_rotate(src: BinaryIO, keyring, new_kid: str | None = None):
    """解出 DEK 并取目标主密钥；轮换前强制验证旧头部，拒绝带病轮换。

    只读取容器头部，不触碰密文块（轮换不需要解密数据）。
    """
    if new_kid is None:
        new_master = keyring.active()
    else:
        try:
            new_master = keyring.get_for_unwrap(new_kid)
        except Exception as exc:
            raise KeyNotFoundError(f"目标主密钥不存在：{new_kid}") from exc

    header = _read_and_parse_header(src)
    old_kid = header["kid"]
    try:
        old_key = keyring.get_for_unwrap(old_kid).key
    except Exception as exc:
        raise KeyNotFoundError(f"旧主密钥不存在，无法解包 DEK：{old_kid}") from exc
    wrap_nonce = _b64d(header, "wrap_nonce_b64", NONCE_LEN)
    wrapped_dek = _b64d(header, "wrapped_dek_b64", KEY_LEN + TAG_LEN)
    try:
        dek = open_sealed(old_key, wrap_nonce, wrapped_dek, wrap_aad(old_kid))
    except InvalidTag:
        raise IntegrityError("数据密钥包裹层认证失败，拒绝轮换")
    try:
        open_sealed(
            dek,
            _b64d(header, "hdr_nonce_b64", NONCE_LEN),
            _b64d(header, "hdr_tag_b64", TAG_LEN),
            _header_aad(header),
        )
    except InvalidTag:
        raise IntegrityError("文件头认证失败，拒绝轮换")
    return new_master, header, dek


def _write_rotated(
    src: BinaryIO,
    dst: BinaryIO,
    *,
    header: dict,
    dek: bytes,
    new_master,
) -> None:
    """写出轮换后的容器：新头 + 原样复制的密文块区。"""
    prefix = _b64d(header, "nonce_prefix_b64", NONCE_PREFIX_LEN)
    new_header = dict(header)
    new_header["kid"] = new_master.kid
    new_wrap_nonce = random_nonce()
    new_wrapped = seal(
        new_master.key, new_wrap_nonce, dek, wrap_aad(new_master.kid)
    )
    new_header["wrap_nonce_b64"] = _b64e(new_wrap_nonce)
    new_header["wrapped_dek_b64"] = _b64e(new_wrapped)

    aad = _header_aad_from_fields(
        file_id=new_header["file_id"],
        kid=new_master.kid,
        chunk_size=new_header["chunk_size"],
        plaintext_size=new_header["plaintext_size"],
        chunk_count=new_header["chunk_count"],
        prefix=prefix,
    )
    new_hdr_nonce = random_nonce()
    new_hdr_tag = seal(dek, new_hdr_nonce, b"", aad)
    new_header["hdr_nonce_b64"] = _b64e(new_hdr_nonce)
    new_header["hdr_tag_b64"] = _b64e(new_hdr_tag)

    header_bytes = _canonical_header_bytes(new_header)

    # 跳过源文件的旧头（magic6 + version1 + len4 + 旧头），逐字节复制块区
    _read_exact(src, len(MAGIC), "magic")
    _read_exact(src, 1, "version")
    (old_len,) = _HEADER_LEN.unpack(_read_exact(src, 4, "头部长度"))
    _read_exact(src, old_len, "旧头部")

    dst.write(MAGIC)
    dst.write(bytes([VERSION]))
    dst.write(_HEADER_LEN.pack(len(header_bytes)))
    dst.write(header_bytes)
    while True:
        chunk = src.read(64 * 1024)
        if not chunk:
            break
        dst.write(chunk)


# ====================================================================== 头部
def parse_header(blob: bytes) -> dict:
    """不解密、只读头部（供检查/测试用）；结构非法会抛 IntegrityError。"""
    with io.BytesIO(blob) as src:
        return _read_and_parse_header(src)


def read_header(path: str) -> dict:
    """文件版 :func:`parse_header`。"""
    with open(path, "rb") as f:
        return _read_and_parse_header(f)


def _read_and_parse_header(src: BinaryIO) -> dict:
    magic = _read_exact(src, len(MAGIC), "magic")
    if magic != MAGIC:
        raise IntegrityError("不是 ENVENC 容器（magic 不匹配）")
    version = _read_exact(src, 1, "版本号")
    if version[0] != VERSION:
        raise IntegrityError(f"不支持的容器版本：{version[0]}（支持 {VERSION}）")
    raw_len = _read_exact(src, 4, "头部长度")
    (header_len,) = _HEADER_LEN.unpack(raw_len)
    if header_len == 0 or header_len > _HEADER_MAX:
        raise IntegrityError(f"头部长度越界：{header_len}")
    raw = _read_exact(src, header_len, "头部 JSON")
    try:
        header = json.loads(raw.decode("utf-8"))
    except (UnicodeDecodeError, json.JSONDecodeError):
        raise IntegrityError("头部不是合法 UTF-8 JSON")
    _validate_header(header)
    return header


def _validate_header(header: dict) -> None:
    if not isinstance(header, dict):
        raise IntegrityError("头部 JSON 不是对象")
    required = {
        "v", "file_id", "kid", "chunk_size", "plaintext_size", "chunk_count",
        "nonce_prefix_b64", "wrap_nonce_b64", "wrapped_dek_b64",
        "hdr_nonce_b64", "hdr_tag_b64",
    }
    missing = required - header.keys()
    if missing:
        raise IntegrityError(f"头部缺少字段：{sorted(missing)}")
    if header["v"] != VERSION:
        raise IntegrityError(f"头部版本号不支持：{header['v']}")
    for name in ("file_id", "kid"):
        if not isinstance(header[name], str) or not header[name]:
            raise IntegrityError(f"头部字段 {name} 必须是非空字符串")
    for name in ("chunk_size", "plaintext_size", "chunk_count"):
        value = header[name]
        # bool 是 int 的子类，显式排除
        if not isinstance(value, int) or isinstance(value, bool) or value < 0:
            raise IntegrityError(f"头部字段 {name} 必须是非负整数")
    if header["chunk_size"] < 1:
        raise IntegrityError("chunk_size 必须 >= 1")
    ps, cs = header["plaintext_size"], header["chunk_size"]
    expected_count = 0 if ps == 0 else (ps - 1) // cs + 1
    if expected_count != header["chunk_count"]:
        raise IntegrityError("chunk_count 与 plaintext_size/chunk_size 不自洽")
    # 长度严格的 base64 字段
    _b64d(header, "nonce_prefix_b64", NONCE_PREFIX_LEN)
    _b64d(header, "wrap_nonce_b64", NONCE_LEN)
    _b64d(header, "hdr_nonce_b64", NONCE_LEN)
    _b64d(header, "hdr_tag_b64", TAG_LEN)
    _b64d(header, "wrapped_dek_b64", None)  # 长度应为 KEY_LEN+TAG_LEN，下面校验
    if len(_b64d(header, "wrapped_dek_b64", None)) != KEY_LEN + TAG_LEN:
        raise IntegrityError("wrapped_dek_b64 长度非法")


def _build_header(
    *,
    file_id: str,
    kid: str,
    chunk_size: int,
    plaintext_size: int,
    chunk_count: int,
    prefix: bytes,
    wrap_nonce: bytes,
    wrapped_dek: bytes,
) -> dict:
    return {
        "v": VERSION,
        "file_id": file_id,
        "kid": kid,
        "chunk_size": chunk_size,
        "plaintext_size": plaintext_size,
        "chunk_count": chunk_count,
        "nonce_prefix_b64": _b64e(prefix),
        "wrap_nonce_b64": _b64e(wrap_nonce),
        "wrapped_dek_b64": _b64e(wrapped_dek),
        # hdr_nonce_b64 / hdr_tag_b64 由调用方补齐
    }


def _canonical_header_bytes(header: dict) -> bytes:
    return json.dumps(header, sort_keys=True, separators=(",", ":")).encode("utf-8")


def _header_aad(header: dict) -> bytes:
    return _header_aad_from_fields(
        file_id=header["file_id"],
        kid=header["kid"],
        chunk_size=header["chunk_size"],
        plaintext_size=header["plaintext_size"],
        chunk_count=header["chunk_count"],
        prefix=_b64d(header, "nonce_prefix_b64", NONCE_PREFIX_LEN),
    )


def _header_aad_from_fields(
    *, file_id, kid, chunk_size, plaintext_size, chunk_count, prefix
) -> bytes:
    return header_aad(
        file_id=file_id,
        kid=kid,
        chunk_size=chunk_size,
        plaintext_size=plaintext_size,
        chunk_count=chunk_count,
        nonce_prefix=prefix,
    )


# ====================================================================== 小工具
class _SimulatedCrash(Exception):
    """测试用：模拟轮换在临时文件写好后进程被杀死。"""


def _new_file_id() -> str:
    return f"f-{secrets.token_hex(8)}"


def _b64e(data: bytes) -> str:
    return base64.b64encode(data).decode("ascii")


def _b64d(header: dict, name: str, expected_len: int | None) -> bytes:
    raw = header.get(name)
    if not isinstance(raw, str):
        raise IntegrityError(f"头部字段 {name} 缺失或类型错误")
    try:
        data = base64.b64decode(raw.encode("ascii"), validate=True)
    except Exception as exc:
        raise IntegrityError(f"头部字段 {name} 不是合法 base64") from exc
    if expected_len is not None and len(data) != expected_len:
        raise IntegrityError(f"头部字段 {name} 长度非法：{len(data)}")
    return data


def _read_exact(src: BinaryIO, n: int, what: str) -> bytes:
    data = src.read(n)
    if len(data) != n:
        raise TruncatedContainer(
            f"容器截断：读取{what}需要 {n} 字节，实际只有 {len(data)} 字节"
        )
    return data


def _require_positive_chunk(chunk_size: int) -> None:
    if not isinstance(chunk_size, int) or isinstance(chunk_size, bool) or chunk_size < 1:
        raise ValueError("chunk_size 必须是 >= 1 的整数")


def _open_tmp(path: str) -> int:
    return os.open(
        path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, _FILE_MODE
    )


def _discard_stale(path: str) -> None:
    """上次崩溃遗留的同路径临时文件直接删除（内容均为密文，无明文残留风险）。"""
    _unlink_quiet(path)


def _unlink_quiet(path: str) -> None:
    try:
        os.unlink(path)
    except FileNotFoundError:
        pass


def _fsync_dir_of(path: str) -> None:
    directory = os.path.dirname(os.path.abspath(path)) or "."
    try:
        fd = os.open(directory, os.O_RDONLY)
    except OSError:
        return
    try:
        os.fsync(fd)
    except OSError:
        pass
    finally:
        os.close(fd)
