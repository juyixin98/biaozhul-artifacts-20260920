"""密码学原语：Ed25519 签名 / 验签、规范化消息编码、版本比较。

设计要点
--------
1. 使用成熟库 ``cryptography`` 的 Ed25519（RFC 8032），不自行实现签名算法。
2. 签名绑定三件事：**内容摘要 (SHA-256) + 制品类型 + 版本**，另加一次性 nonce
   防止同一消息被重复登记。
3. 不同用途使用不同的「域分离前缀」(domain separation)：
   - 制品签名与根轮换签名的消息前缀不同，杜绝跨用途复用签名；
   - 类型字符串也参与消息编码，杜绝跨制品类型复用签名。
4. 所有可变长字段使用长度前缀 (8 字节大端) 编码，避免字段拼接歧义。
5. 仅用于学习/测试；切勿把本模块生成的任何密钥用于生产环境。
"""

from __future__ import annotations

import hashlib
import os
import re
from dataclasses import dataclass

from cryptography.exceptions import InvalidSignature
from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric.ed25519 import (
    Ed25519PrivateKey,
    Ed25519PublicKey,
)

# --- 域分离前缀（互不相同的 ASCII 常量） -----------------------------------

DOMAIN_ARTIFACT_SIGNATURE = b"ARTIFACT-SIGNATURE/v1"
DOMAIN_ROOT_ROTATION = b"ROOT-ROTATION/v1"

# 长度前缀宽度：8 字节大端无符号整数
_LEN_PREFIX = 8

# 制品版本：严格的 MAJOR.MINOR.PATCH，纯数字，不允许前导零、预发布后缀。
_SEMVER_RE = re.compile(r"^(0|[1-9][0-9]*)\.(0|[1-9][0-9]*)\.(0|[1-9][0-9]*)$")


class CryptoError(ValueError):
    """密码学相关输入错误（十六进制非法、长度不对等）。"""


# --- 基础工具 ----------------------------------------------------------------


def _lp(data: bytes) -> bytes:
    """长度前缀编码：8 字节大端长度 + 数据。"""

    return len(data).to_bytes(_LEN_PREFIX, "big") + data


def sha256_hex(content: bytes) -> str:
    """计算内容的 SHA-256 摘要（小写十六进制）。"""

    return hashlib.sha256(content).hexdigest()


def generate_nonce() -> str:
    """生成一次性随机 nonce（16 字节 -> 32 个十六进制字符）。"""

    return os.urandom(16).hex()


def key_id(public_key_hex: str) -> str:
    """密钥 ID = SHA-256(原始公钥 32 字节) 的小写十六进制。"""

    raw = unhex(public_key_hex, "public_key")
    if len(raw) != 32:
        raise CryptoError("Ed25519 公钥必须为 32 字节（64 个十六进制字符）")
    return hashlib.sha256(raw).hexdigest()


def unhex(value: str, field: str = "value") -> bytes:
    """解码小写/大写十六进制串，失败抛出 CryptoError。"""

    if not isinstance(value, str):
        raise CryptoError(f"{field} 必须是十六进制字符串")
    try:
        return bytes.fromhex(value)
    except ValueError as exc:
        raise CryptoError(f"{field} 不是合法的十六进制字符串") from exc


# --- 密钥对 ------------------------------------------------------------------


@dataclass(frozen=True)
class KeyPair:
    """一对 Ed25519 测试密钥（均为十六进制；私钥为 32 字节种子）。"""

    private_key_hex: str
    public_key_hex: str

    @property
    def kid(self) -> str:
        return key_id(self.public_key_hex)


def generate_keypair() -> KeyPair:
    """生成新的 Ed25519 密钥对。仅用于测试，不使用任何生产凭据。"""

    priv = Ed25519PrivateKey.generate()
    priv_raw = priv.private_bytes(
        encoding=serialization.Encoding.Raw,
        format=serialization.PrivateFormat.Raw,
        encryption_algorithm=serialization.NoEncryption(),
    )
    pub_raw = priv.public_key().public_bytes(
        encoding=serialization.Encoding.Raw,
        format=serialization.PublicFormat.Raw,
    )
    return KeyPair(priv_raw.hex(), pub_raw.hex())


def _load_private(private_key_hex: str) -> Ed25519PrivateKey:
    raw = unhex(private_key_hex, "private_key")
    if len(raw) != 32:
        raise CryptoError("Ed25519 私钥必须为 32 字节（64 个十六进制字符）")
    return Ed25519PrivateKey.from_private_bytes(raw)


def _load_public(public_key_hex: str) -> Ed25519PublicKey:
    raw = unhex(public_key_hex, "public_key")
    if len(raw) != 32:
        raise CryptoError("Ed25519 公钥必须为 32 字节（64 个十六进制字符）")
    return Ed25519PublicKey.from_public_bytes(raw)


# --- 版本 --------------------------------------------------------------------


def parse_version(version: str) -> tuple[int, int, int]:
    """解析 MAJOR.MINOR.PATCH；非法时抛出 CryptoError。"""

    m = _SEMVER_RE.match(version or "")
    if not m:
        raise CryptoError(f"版本号必须是 MAJOR.MINOR.PATCH 纯数字格式：{version!r}")
    return int(m.group(1)), int(m.group(2)), int(m.group(3))


def is_rollback(candidate: str, current_highest: str | None) -> bool:
    """``candidate`` 是否相对于已见最高版本构成回退（含相等）。

    语义：版本必须严格递增。相等也视为拒绝（防止重复登记同一版本）。
    """

    cand = parse_version(candidate)
    if current_highest is None:
        return False
    return cand <= parse_version(current_highest)


# --- 制品签名 ----------------------------------------------------------------


def encode_artifact_message(
    *,
    digest_hex: str,
    artifact_type: str,
    version: str,
    nonce_hex: str,
) -> bytes:
    """构造被签名的规范化制品消息。

    布局::

        DOMAIN_ARTIFACT_SIGNATURE
        | len(digest)  | digest
        | len(type)    | type(UTF-8)
        | len(version) | version(UTF-8)
        | len(nonce)   | nonce(raw bytes)
    """

    digest = unhex(digest_hex, "digest")
    if len(digest) != 32:
        raise CryptoError("摘要必须为 SHA-256 的 32 字节（64 个十六进制字符）")
    nonce = unhex(nonce_hex, "nonce")
    if len(nonce) != 16:
        raise CryptoError("nonce 必须为 16 字节（32 个十六进制字符）")
    if not artifact_type:
        raise CryptoError("artifact_type 不能为空")
    # 提前校验版本格式，保证签名/验签两边对格式的认识一致。
    parse_version(version)

    return b"".join(
        [
            _lp(DOMAIN_ARTIFACT_SIGNATURE),
            _lp(digest),
            _lp(artifact_type.encode("utf-8")),
            _lp(version.encode("utf-8")),
            _lp(nonce),
        ]
    )


def sign_artifact(
    *,
    private_key_hex: str,
    digest_hex: str,
    artifact_type: str,
    version: str,
    nonce_hex: str,
) -> str:
    """用制品签名私钥对 (摘要, 类型, 版本, nonce) 签名，返回十六进制签名。"""

    message = encode_artifact_message(
        digest_hex=digest_hex,
        artifact_type=artifact_type,
        version=version,
        nonce_hex=nonce_hex,
    )
    return _load_private(private_key_hex).sign(message).hex()


def verify_artifact_signature(
    *,
    public_key_hex: str,
    signature_hex: str,
    digest_hex: str,
    artifact_type: str,
    version: str,
    nonce_hex: str,
) -> bool:
    """验签。任何不匹配（含被篡改的正文重建出的摘要）都返回 False。"""

    try:
        message = encode_artifact_message(
            digest_hex=digest_hex,
            artifact_type=artifact_type,
            version=version,
            nonce_hex=nonce_hex,
        )
        signature = unhex(signature_hex, "signature")
        _load_public(public_key_hex).verify(signature, message)
        return True
    except (CryptoError, InvalidSignature, ValueError):
        return False


# --- 根信任轮换 --------------------------------------------------------------


def encode_root_rotation_message(
    *,
    new_root_version: int,
    new_threshold: int,
    new_threshold_keys: list[str],
    new_signer_keys: list[str],
) -> bytes:
    """构造根轮换批准签名所覆盖的规范化消息。

    待批准的新根内容 = 新版本号 + 新阈值 + 新阈值公钥集合 + 新制品签名公钥集合。
    公钥先排序再编码，消除顺序歧义；重复公钥直接拒绝。
    """

    if new_root_version < 1:
        raise CryptoError("root_version 必须 >= 1")
    if new_threshold < 1:
        raise CryptoError("threshold 必须 >= 1")

    def _normalize(keys: list[str], name: str) -> list[bytes]:
        normalized: list[bytes] = []
        for k in keys:
            raw = unhex(k, name)
            if len(raw) != 32:
                raise CryptoError(f"{name} 中的公钥必须为 32 字节")
            normalized.append(raw)
        if len(set(normalized)) != len(normalized):
            raise CryptoError(f"{name} 中存在重复公钥")
        return sorted(normalized)

    threshold_keys = _normalize(new_threshold_keys, "threshold_key")
    signer_keys = _normalize(new_signer_keys, "signer_key")
    if new_threshold > len(threshold_keys):
        raise CryptoError("threshold 不能超过阈值公钥数量")

    out = bytearray()
    out += _lp(DOMAIN_ROOT_ROTATION)
    out += new_root_version.to_bytes(8, "big")
    out += new_threshold.to_bytes(8, "big")
    out += len(threshold_keys).to_bytes(8, "big")
    for k in threshold_keys:
        out += _lp(k)
    out += len(signer_keys).to_bytes(8, "big")
    for k in signer_keys:
        out += _lp(k)
    return bytes(out)


def sign_root_rotation(
    *,
    private_key_hex: str,
    new_root_version: int,
    new_threshold: int,
    new_threshold_keys: list[str],
    new_signer_keys: list[str],
) -> str:
    """旧根阈值成员对新根内容签名，返回十六进制签名。"""

    message = encode_root_rotation_message(
        new_root_version=new_root_version,
        new_threshold=new_threshold,
        new_threshold_keys=new_threshold_keys,
        new_signer_keys=new_signer_keys,
    )
    return _load_private(private_key_hex).sign(message).hex()


def verify_root_rotation_approval(
    *,
    public_key_hex: str,
    signature_hex: str,
    new_root_version: int,
    new_threshold: int,
    new_threshold_keys: list[str],
    new_signer_keys: list[str],
) -> bool:
    """验证单个旧根成员的轮换批准签名。不匹配返回 False。"""

    try:
        message = encode_root_rotation_message(
            new_root_version=new_root_version,
            new_threshold=new_threshold,
            new_threshold_keys=new_threshold_keys,
            new_signer_keys=new_signer_keys,
        )
        signature = unhex(signature_hex, "approval_signature")
        _load_public(public_key_hex).verify(signature, message)
        return True
    except (CryptoError, InvalidSignature, ValueError):
        return False
