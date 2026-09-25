"""密码学原语封装。

明确不做的事
============
* 不自创加密/哈希算法，全部使用 ``cryptography`` 提供的标准原语。
* 不连接任何生产账号 / KMS / 云服务；密钥仅本地生成与读写。
* 不对抗本地恶意进程；私钥文件权限由调用方/部署方负责（CLI 默认 0600）。

提供的能力
==========
* Ed25519 签名密钥对生成、PEM 读写、签名、验签；
* 导出信封加密：Fernet（AES-128-CBC + HMAC-SHA256 的认证加密封装，
  cryptography 官方高级 API），密钥本地随机生成；
* 任务级随机盐（用于 hash 泛化器的 HMAC）。
"""

from __future__ import annotations

import os
import stat

from cryptography.fernet import Fernet, InvalidToken
from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric.ed25519 import (
    Ed25519PrivateKey,
    Ed25519PublicKey,
)

from .canonical import canonical_dumps
from .errors import VerificationError

# 封装格式版本，便于将来区分算法族。
ENVELOPE_VERSION = "fernet-v1"
SIGNING_ALG = "Ed25519"


# ---------- 任务盐 ----------

def new_hash_salt(nbytes: int = 32) -> bytes:
    """生成任务级随机盐（默认 256 bit）。"""
    return os.urandom(nbytes)


# ---------- Ed25519 签名 ----------

def generate_signing_key() -> Ed25519PrivateKey:
    return Ed25519PrivateKey.generate()


def public_key_hex(priv: Ed25519PrivateKey) -> str:
    return priv.public_key().public_bytes_raw().hex()


def private_to_pem(priv: Ed25519PrivateKey) -> bytes:
    return priv.private_bytes(
        encoding=serialization.Encoding.PEM,
        format=serialization.PrivateFormat.PKCS8,
        encryption_algorithm=serialization.NoEncryption(),
    )


def public_to_pem(pub: Ed25519PublicKey) -> bytes:
    return pub.public_bytes(
        encoding=serialization.Encoding.PEM,
        format=serialization.PublicFormat.SubjectPublicKeyInfo,
    )


def save_private_key(priv: Ed25519PrivateKey, path: str) -> None:
    """写私钥文件并强制权限 0600（已存在时也收紧权限）。"""
    with open(os.open(path, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600),
              "wb") as f:
        f.write(private_to_pem(priv))
    os.chmod(path, stat.S_IRUSR | stat.S_IWUSR)


def save_public_key(pub: Ed25519PublicKey, path: str) -> None:
    with open(path, "wb") as f:
        f.write(public_to_pem(pub))


def load_private_key(path: str) -> Ed25519PrivateKey:
    with open(path, "rb") as f:
        key = serialization.load_pem_private_key(f.read(), password=None)
    if not isinstance(key, Ed25519PrivateKey):
        raise VerificationError(f"{path}: expected Ed25519 private key")
    return key


def load_public_key(path_or_pem: str) -> Ed25519PublicKey:
    """从 PEM 文件路径加载公钥。"""
    with open(path_or_pem, "rb") as f:
        data = f.read()
    return _load_public_pem(data)


def load_public_key_bytes(data: bytes) -> Ed25519PublicKey:
    return _load_public_pem(data)


def _load_public_pem(data: bytes) -> Ed25519PublicKey:
    key = serialization.load_pem_public_key(data)
    if not isinstance(key, Ed25519PublicKey):
        raise VerificationError("expected Ed25519 public key")
    return key


def sign_object(priv: Ed25519PrivateKey, obj: object) -> str:
    """对 canonical JSON 字节签名，返回 hex。"""
    return priv.sign(canonical_dumps(obj)).hex()


def verify_object(pub: Ed25519PublicKey, obj: object, signature_hex: str) -> None:
    """验签；失败抛 VerificationError。"""
    try:
        sig = bytes.fromhex(signature_hex)
    except ValueError:
        raise VerificationError("signature is not valid hex") from None
    try:
        pub.verify(sig, canonical_dumps(obj))
    except Exception as exc:
        raise VerificationError(f"signature verification failed: {exc}") from None


# ---------- Fernet 信封加密 ----------

def generate_fernet_key() -> bytes:
    """生成 urlsafe-base64 的 Fernet 密钥（32 字节随机数）。"""
    return Fernet.generate_key()


def save_key_file(key: bytes, path: str) -> None:
    """写对称密钥文件，权限 0600。"""
    with open(os.open(path, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600),
              "wb") as f:
        f.write(key)
    os.chmod(path, stat.S_IRUSR | stat.S_IWUSR)


def load_key_file(path: str) -> bytes:
    with open(path, "rb") as f:
        return f.read().strip()


def encrypt_blob(plaintext: bytes, key: bytes) -> dict[str, object]:
    """返回加密信封 dict（含格式版本）。"""
    token = Fernet(key).encrypt(plaintext)
    return {
        "envelope": ENVELOPE_VERSION,
        "ciphertext": token.decode("ascii"),
    }


def decrypt_blob(envelope: dict[str, object], key: bytes) -> bytes:
    """解密信封；格式不符或认证失败抛 VerificationError。"""
    if not isinstance(envelope, dict) or envelope.get("envelope") != ENVELOPE_VERSION:
        raise VerificationError("unsupported or missing envelope version")
    token = envelope.get("ciphertext")
    if not isinstance(token, str):
        raise VerificationError("envelope missing ciphertext")
    try:
        return Fernet(key).decrypt(token.encode("ascii"))
    except InvalidToken:
        raise VerificationError("decryption failed: invalid key or tampered "
                                "ciphertext") from None
