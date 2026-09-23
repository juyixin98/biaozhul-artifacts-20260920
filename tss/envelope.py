"""份额信封格式（带版本号）。

版本 1（``TSS1``）二进制布局（大端）::

    偏移  长度  字段
    0     4    魔数 b"TSS1"
    4     1    格式版本（= 1）
    5     1    有限域标签（0x01 = GF(2^8), Rijndael 多项式 0x11B）
    6     1    标志位：bit0=1 表示标签为 HMAC-SHA256，否则为纯 SHA256 校验和
    7     1    阈值 t
    8     1    份额总数 n
    9     1    本份额横坐标 x（1..255）
    10    16   split_id（同一次 split 的所有份额相同，用于拒绝混批）
    26    4    载荷长度 L
    30    L    载荷（该点的纵坐标 y，长度等于秘密长度）
    30+L  32   完整性标签（对前 30+L 字节计算）

文本承载使用标准 base64（RFC 4648，带 padding）。

设计说明：
- SHA256 校验和只能发现**意外损坏**（传输/编码错误），持有份额的攻击者可以
  在篡改后自行重算校验和，因此它**不能**识别恶意份额；
- 提供独立 auth_key 时标签换成 HMAC-SHA256，没有密钥的攻击者无法伪造，
  这才是可识别恶意篡改的机制。auth_key 必须带外分发，不能随份额传递。
"""

import base64
import hashlib
import hmac as hmac_mod
import secrets
from dataclasses import dataclass

from cryptography.hazmat.primitives import hashes, hmac

from .errors import FormatError, IntegrityError, ParameterError

TSS1_MAGIC = b"TSS1"
FORMAT_VERSION = 1
FIELD_RIJNDAEL = 0x01
_FLAG_AUTHENTICATED = 0x01
_RESERVED_FLAGS = 0xFE

_MAGIC_OFF, _MAGIC_LEN = 0, 4
_VERSION_OFF = 4
_FIELD_OFF = 5
_FLAGS_OFF = 6
_T_OFF = 7
_N_OFF = 8
_X_OFF = 9
_SPLIT_ID_OFF, _SPLIT_ID_LEN = 10, 16
_LEN_OFF, _LEN_LEN = 26, 4
_HEADER_LEN = 30
_TAG_LEN = 32

MIN_AUTH_KEY_BYTES = 16


@dataclass(frozen=True)
class RawShare:
    """信封解析后的份额内容。"""

    threshold: int
    total: int
    x: int
    split_id: bytes
    y: bytes
    authenticated: bool


def new_split_id() -> bytes:
    """为一次 split 生成 16 字节随机实例标识（CSPRNG）。"""
    return secrets.token_bytes(_SPLIT_ID_LEN)


def generate_auth_key(length: int = 32) -> bytes:
    """生成 HMAC 认证密钥（默认 32 字节），应通过带外渠道分发给恢复方。"""
    if length < MIN_AUTH_KEY_BYTES:
        raise ParameterError(f"auth key must be at least {MIN_AUTH_KEY_BYTES} bytes")
    return secrets.token_bytes(length)


def _build_header(threshold: int, total: int, x: int, split_id: bytes,
                  payload_len: int, authenticated: bool) -> bytes:
    if len(split_id) != _SPLIT_ID_LEN:
        raise ParameterError("split_id must be 16 bytes")
    flags = _FLAG_AUTHENTICATED if authenticated else 0
    return (
        TSS1_MAGIC
        + bytes([FORMAT_VERSION, FIELD_RIJNDAEL, flags, threshold, total, x])
        + split_id
        + payload_len.to_bytes(_LEN_LEN, "big")
    )


def _compute_tag(authenticated: bool, auth_key, tagged: bytes) -> bytes:
    if authenticated:
        if not auth_key:
            raise ParameterError("share is authenticated but no auth_key was provided")
        if len(auth_key) < MIN_AUTH_KEY_BYTES:
            raise ParameterError(
                f"auth_key too short (need >= {MIN_AUTH_KEY_BYTES} bytes)"
            )
        signer = hmac.HMAC(auth_key, hashes.SHA256())
        signer.update(tagged)
        return signer.finalize()
    return hashlib.sha256(tagged).digest()


def encode_share(threshold: int, total: int, x: int, split_id: bytes,
                 y: bytes, auth_key=None) -> str:
    """把单个份额点编码为 base64 文本信封。"""
    authenticated = auth_key is not None
    header = _build_header(threshold, total, x, split_id, len(y), authenticated)
    tagged = header + y
    tag = _compute_tag(authenticated, auth_key, tagged)
    return base64.standard_b64encode(tagged + tag).decode("ascii")


def decode_share(text: str, auth_key=None) -> RawShare:
    """解码并完整校验一个份额信封。

    对任何不符合本版本格式的输入抛 :class:`FormatError`；
    标签不匹配抛 :class:`IntegrityError`。未知版本/域一律按失败处理
    （前向兼容采取 fail-closed，而不是猜测解析）。
    """
    if not isinstance(text, str):
        raise FormatError("share must be a base64 string")
    try:
        # validate=True 时仅允许标准字母表 [A-Za-z0-9+/] 与末尾至多两个 '='
        raw = base64.b64decode(text.encode("ascii"), validate=True)
    except Exception as exc:
        raise FormatError(f"invalid standard base64: {exc}") from exc

    if len(raw) < _HEADER_LEN + _TAG_LEN:
        raise FormatError(
            f"share too short: {len(raw)} bytes, need >= {_HEADER_LEN + _TAG_LEN}"
        )

    if raw[_MAGIC_OFF:_MAGIC_OFF + _MAGIC_LEN] != TSS1_MAGIC:
        raise FormatError("bad magic: not a TSS share envelope")
    if raw[_VERSION_OFF] != FORMAT_VERSION:
        raise FormatError(
            f"unsupported format version: {raw[_VERSION_OFF]} (supported: {FORMAT_VERSION})"
        )
    if raw[_FIELD_OFF] != FIELD_RIJNDAEL:
        raise FormatError(f"unsupported finite field tag: 0x{raw[_FIELD_OFF]:02x}")

    flags = raw[_FLAGS_OFF]
    if flags & _RESERVED_FLAGS:
        raise FormatError(f"reserved/unknown flag bits set: 0x{flags:02x}")
    authenticated = bool(flags & _FLAG_AUTHENTICATED)

    threshold, total, x = raw[_T_OFF], raw[_N_OFF], raw[_X_OFF]
    if threshold < 2:
        raise FormatError(f"threshold in envelope out of range: {threshold}")
    if total < threshold or total > 255:
        raise FormatError(f"total in envelope out of range: {total}")
    if not 1 <= x <= 255:
        raise FormatError(f"share index in envelope out of range: {x}")

    payload_len = int.from_bytes(raw[_LEN_OFF:_LEN_OFF + _LEN_LEN], "big")
    expected_len = _HEADER_LEN + payload_len + _TAG_LEN
    if len(raw) != expected_len:
        raise FormatError(
            f"length mismatch: header says {payload_len} bytes payload, "
            f"actual envelope is {len(raw) - _HEADER_LEN - _TAG_LEN} bytes"
        )

    tagged = raw[:_HEADER_LEN + payload_len]
    tag = raw[_HEADER_LEN + payload_len:]
    if authenticated and not auth_key:
        # 信封要求 HMAC 认证却没有提供密钥：fail-closed，拒绝采信
        raise IntegrityError(
            "share is HMAC-authenticated but no auth_key was provided; "
            "integrity cannot be verified"
        )
    expected_tag = _compute_tag(authenticated, auth_key if authenticated else None, tagged)
    # 恒定时间比较，避免标签比较引入计时侧信道
    if not hmac_mod.compare_digest(tag, expected_tag):
        if authenticated:
            raise IntegrityError("HMAC-SHA256 verification failed (tampered or wrong auth_key)")
        raise IntegrityError("SHA256 checksum failed (share payload is corrupted or tampered)")

    return RawShare(
        threshold=threshold,
        total=total,
        x=x,
        split_id=bytes(raw[_SPLIT_ID_OFF:_SPLIT_ID_OFF + _SPLIT_ID_LEN]),
        y=bytes(raw[_HEADER_LEN:_HEADER_LEN + payload_len]),
        authenticated=authenticated,
    )
