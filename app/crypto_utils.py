"""Ed25519 签名原语与多签阈值校验。

只依赖 ``cryptography``：
- 私钥/公钥均以 32 字节裸字节的 hex 表示；
- keyid 定义为 ``sha256(公钥裸字节)`` 的十六进制；
- 每个元数据文件可携带多个签名，按 root 中声明的 threshold 校验。
"""

from __future__ import annotations

import hashlib

from cryptography.exceptions import InvalidSignature
from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey
from cryptography.hazmat.primitives.serialization import (
    Encoding,
    NoEncryption,
    PrivateFormat,
    PublicFormat,
)

from .errors import BadFormatError, BadSignatureError

KEY_TYPE = "ed25519"
SCHEME = "ed25519"


def generate_keypair() -> tuple[str, str]:
    """返回 (私钥 hex, 公钥 hex)。"""
    private_key = Ed25519PrivateKey.generate()
    priv_bytes = private_key.private_bytes(Encoding.Raw, PrivateFormat.Raw, NoEncryption())
    pub_bytes = private_key.public_key().public_bytes(Encoding.Raw, PublicFormat.Raw)
    return priv_bytes.hex(), pub_bytes.hex()


def public_from_private(priv_hex: str) -> str:
    priv = _load_private(priv_hex)
    return priv.public_key().public_bytes(Encoding.Raw, PublicFormat.Raw).hex()


def keyid_for(pub_hex: str) -> str:
    return hashlib.sha256(_pub_bytes(pub_hex)).hexdigest()


def make_key_object(pub_hex: str) -> dict:
    return {
        "keytype": KEY_TYPE,
        "scheme": SCHEME,
        "keyval": {"public": pub_hex},
        "keyid": keyid_for(pub_hex),
    }


def sign(priv_hex: str, data: bytes) -> str:
    return _load_private(priv_hex).sign(data).hex()


def verify(pub_hex: str, sig_hex: str, data: bytes) -> bool:
    try:
        _load_public(pub_hex).verify(bytes.fromhex(sig_hex), data)
        return True
    except (InvalidSignature, ValueError):
        return False


def verify_threshold(
    envelope: dict,
    authorized_keyids: list[str],
    threshold: int,
    keys: dict,
    *,
    role: str,
    signed_bytes: bytes,
) -> None:
    """按 root 元数据声明校验一个 envelope 的签名阈值。

    - authorized_keyids 中每个 key 对 envelope 的合法签名至多计数一次；
    - 授权 key 给出的签名若无法通过验证，直接判定为伪造，而不是静默忽略；
    - 去重后合法签名数 >= threshold 才通过。
    """
    signatures = envelope.get("signatures")
    if not isinstance(signatures, list) or not signatures:
        raise BadSignatureError("NO_SIGNATURE", f"角色 {role} 没有任何签名", {"role": role})
    if threshold < 1:
        raise BadFormatError("BAD_THRESHOLD", f"角色 {role} 的 threshold 必须 >= 1", {"role": role})

    authorized = set(authorized_keyids)
    good_keyids: set[str] = set()
    for sig in signatures:
        if not isinstance(sig, dict):
            raise BadFormatError("BAD_SIGNATURE_BLOCK", "签名块格式错误", {"role": role})
        kid = sig.get("keyid")
        sig_hex = sig.get("sig")
        if not isinstance(kid, str) or not isinstance(sig_hex, str):
            raise BadFormatError("BAD_SIGNATURE_BLOCK", "签名块缺少 keyid/sig", {"role": role})
        if kid not in authorized:
            # 非授权 key 的签名按 TUF 惯例忽略
            continue
        key_obj = keys.get(kid)
        if key_obj is None or key_obj.get("keyval", {}).get("public") is None:
            raise BadSignatureError("UNKNOWN_KEY", f"签名 keyid {kid} 未在 root 中公布", {"keyid": kid})
        if not verify(key_obj["keyval"]["public"], sig_hex, signed_bytes):
            raise BadSignatureError(
                "BAD_SIGNATURE",
                f"角色 {role} 的签名无法通过 keyid {kid} 验证",
                {"role": role, "keyid": kid},
            )
        good_keyids.add(kid)

    if len(good_keyids) < threshold:
        raise BadSignatureError(
            "THRESHOLD_NOT_MET",
            f"角色 {role} 需要 {threshold} 个合法签名，实际 {len(good_keyids)} 个",
            {"role": role, "required": threshold, "found": len(good_keyids)},
        )


def _load_private(priv_hex: str) -> Ed25519PrivateKey:
    try:
        return Ed25519PrivateKey.from_private_bytes(bytes.fromhex(priv_hex))
    except ValueError as exc:
        raise BadFormatError("BAD_KEY", f"无法加载私钥: {exc}") from exc


def _load_public(pub_hex: str):
    from cryptography.hazmat.primitives.serialization import load_der_public_key

    try:
        # Ed25519PublicKey 没有 from_raw_bytes，用 Raw 公钥构造 SPKI DER
        raw = _pub_bytes(pub_hex)
        # SPKI prefix for Ed25519 (RFC 8410): 30 2a 30 05 06 03 2b 65 70 03 21 00
        der = bytes.fromhex("302a300506032b6570032100") + raw
        return load_der_public_key(der)
    except ValueError as exc:
        raise BadFormatError("BAD_KEY", f"无法加载公钥: {exc}") from exc


def _pub_bytes(pub_hex: str) -> bytes:
    try:
        raw = bytes.fromhex(pub_hex)
    except ValueError as exc:
        raise BadFormatError("BAD_KEY", "公钥不是合法 hex") from exc
    if len(raw) != 32:
        raise BadFormatError("BAD_KEY", f"Ed25519 公钥必须为 32 字节，实际 {len(raw)} 字节")
    return raw
