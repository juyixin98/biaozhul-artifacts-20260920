"""策略包完整性保护：Ed25519 签名 + 规范化 JSON（RFC 8785 子集）。

为什么需要它：解释器本身只接受声明式 JSON（无 eval/exec），
签名进一步保证“被求值的策略确实是持有者签发的版本”，防止策略在
离线分发链路上被增删规则或篡改 effect。

规范化序列化（与 RFC 8785 等价的安全子集）
------------------------------------------
* dict 键按 UTF-8 / 码点排序；
* 不做任何多余空白；
* 仅允许 JSON 原生类型，禁止 NaN/Infinity、字节、元组、集合；
* 浮点按最短往返表示（repr），键重排不影响签名。

签名包格式（``SignedBundle``）::

    {
      "version": 1,
      "alg": "Ed25519",
      "kid": "2026-09-dev",
      "canonical": "https://openpolicy.local/canonical/v1",
      "policies": [ ... ],              # 待签名的策略集
      "signature": "base64(Ed25519(canonicalize(policies)))"
    }

签名只覆盖 ``policies`` 的规范化字节，签名字段本身不参与。
"""
from __future__ import annotations

import base64
import json
import os
from typing import Any, Dict

from cryptography.exceptions import InvalidSignature
from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric.ed25519 import (
    Ed25519PrivateKey,
    Ed25519PublicKey,
)

from .errors import BundleError, SignatureError

ALG = "Ed25519"
VERSION = 1
CANONICAL_ID = "https://openpolicy.local/canonical/v1"
SIGNED_FIELDS = {"version", "alg", "kid", "canonical", "policies", "signature"}


# ---- 规范化序列化 -------------------------------------------------------

def canonicalize(value: Any) -> bytes:
    return _encode(value).encode("utf-8")


def _encode(value: Any) -> str:
    if value is None or isinstance(value, bool):
        return "true" if value is True else ("false" if value is False else "null")
    if isinstance(value, int):
        return str(value)
    if isinstance(value, float):
        if value != value or value in (float("inf"), float("-inf")):
            raise ValueError("禁止 NaN/Infinity 参与签名")
        return repr(value)
    if isinstance(value, str):
        return json.dumps(value, ensure_ascii=False)
    if isinstance(value, list):
        return "[" + ",".join(_encode(v) for v in value) + "]"
    if isinstance(value, dict):
        items = sorted(value.items(), key=lambda kv: kv[0].encode("utf-8"))
        return (
            "{"
            + ",".join(json.dumps(k, ensure_ascii=False) + ":" + _encode(v) for k, v in items)
            + "}"
        )
    raise TypeError(f"不可规范化的类型: {type(value).__name__}")


# ---- 签名包构造 / 验签 --------------------------------------------------

def sign_policies(
    policies: Any,
    private_key: Ed25519PrivateKey,
    *,
    kid: str,
) -> Dict[str, Any]:
    # 先验证可规范化（类型/NaN 检查）
    signing_bytes = canonicalize(policies)
    sig = private_key.sign(signing_bytes)
    return {
        "version": VERSION,
        "alg": ALG,
        "kid": kid,
        "canonical": CANONICAL_ID,
        "policies": policies,
        "signature": base64.b64encode(sig).decode("ascii"),
    }


def verify_bundle(bundle: Any, trusted_public_key: Ed25519PublicKey) -> Any:
    """校验签名包，返回其中的策略集（未通过则抛错，绝不返回部分结果）。"""
    if not isinstance(bundle, dict):
        raise BundleError("签名包必须是 JSON 对象")

    extra = set(bundle.keys()) - SIGNED_FIELDS
    if extra:
        raise BundleError(f"签名包含未知字段: {sorted(extra)}（防止签名覆盖范围外注入）")
    if bundle.get("version") != VERSION:
        raise BundleError(f"不支持的签名包版本: {bundle.get('version')!r}")
    if bundle.get("alg") != ALG:
        raise BundleError(f"不支持的算法: {bundle.get('alg')!r}（仅允许 {ALG}）")
    if bundle.get("canonical") != CANONICAL_ID:
        raise BundleError("规范化方案标识不匹配")
    if not isinstance(bundle.get("kid"), str) or not bundle["kid"].strip():
        raise BundleError("kid 必须是非空字符串")
    if "policies" not in bundle:
        raise BundleError("签名包缺少 policies")
    sig_b64 = bundle.get("signature")
    if not isinstance(sig_b64, str):
        raise BundleError("signature 必须是 base64 字符串")
    try:
        signature = base64.b64decode(sig_b64, validate=True)
    except Exception as exc:
        raise BundleError("signature 不是合法 base64") from exc

    try:
        signing_bytes = canonicalize(bundle["policies"])
        trusted_public_key.verify(signature, signing_bytes)
    except InvalidSignature as exc:
        raise SignatureError("签名校验失败：策略内容与签名不一致，或公钥不被信任") from exc
    except (TypeError, ValueError) as exc:
        raise BundleError(f"策略内容无法规范化: {exc}") from exc

    return bundle["policies"]


# ---- PEM 密钥读写 -------------------------------------------------------

def load_private_key_pem(data: bytes) -> Ed25519PrivateKey:
    try:
        key = serialization.load_pem_private_key(data, password=None)
    except Exception as exc:
        raise ValueError(f"无法加载 Ed25519 私钥 PEM: {exc}") from exc
    if not isinstance(key, Ed25519PrivateKey):
        raise ValueError("私钥不是 Ed25519")
    return key


def load_public_key_pem(data: bytes) -> Ed25519PublicKey:
    try:
        key = serialization.load_pem_public_key(data)
    except Exception as exc:
        raise ValueError(f"无法加载 Ed25519 公钥 PEM: {exc}") from exc
    if not isinstance(key, Ed25519PublicKey):
        raise ValueError("公钥不是 Ed25519")
    return key


def generate_keypair() -> tuple[Ed25519PrivateKey, Ed25519PublicKey]:
    priv = Ed25519PrivateKey.generate()
    return priv, priv.public_key()


def private_key_to_pem(key: Ed25519PrivateKey) -> bytes:
    return key.private_bytes(
        encoding=serialization.Encoding.PEM,
        format=serialization.PrivateFormat.PKCS8,
        encryption_algorithm=serialization.NoEncryption(),
    )


def public_key_to_pem(key: Ed25519PublicKey) -> bytes:
    return key.public_bytes(
        encoding=serialization.Encoding.PEM,
        format=serialization.PublicFormat.SubjectPublicKeyInfo,
    )


def load_public_key(path: str | None) -> Ed25519PublicKey | None:
    if not path or not os.path.exists(path):
        return None
    with open(path, "rb") as fh:
        return load_public_key_pem(fh.read())
