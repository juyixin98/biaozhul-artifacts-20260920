"""密钥原语与信任库 —— 基于 ``cryptography`` 库的 Ed25519 实现。

不自创加密算法:
  - 签名算法固定 Ed25519 (RFC 8032), 由 cryptography 提供;
  - 密钥 ID = SHA-256(DER 编码的 SubjectPublicKeyInfo), 取十六进制;
  - 测试密钥全部本地生成, 不接入任何生产账户/KMS。
"""

from __future__ import annotations

import hashlib
from dataclasses import dataclass
from pathlib import Path

from cryptography.exceptions import InvalidSignature
from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric.ed25519 import (
    Ed25519PrivateKey,
    Ed25519PublicKey,
)

from .errors import KeyStoreError, SignatureError, UnknownKeyError

ALGORITHM = "ed25519"
_KEY_ID_LEN = 64  # SHA-256 hex


def generate_private_key() -> Ed25519PrivateKey:
    """本地生成一把全新的 Ed25519 私钥 (测试/离线使用)。"""
    return Ed25519PrivateKey.generate()


def public_key_id(public_key: Ed25519PublicKey) -> str:
    """密钥 ID: SHA-256(DER SubjectPublicKeyInfo) 的小写十六进制。"""
    der = public_key.public_bytes(
        encoding=serialization.Encoding.DER,
        format=serialization.PublicFormat.SubjectPublicKeyInfo,
    )
    return hashlib.sha256(der).hexdigest()


def is_valid_key_id(key_id: str) -> bool:
    return (
        isinstance(key_id, str)
        and len(key_id) == _KEY_ID_LEN
        and all(c in "0123456789abcdef" for c in key_id)
    )


# ---------- PEM 序列化 ----------


def private_key_to_pem(private_key: Ed25519PrivateKey) -> bytes:
    """导出 *未加密* PKCS#8 PEM。仅限本地离线测试, 文件权限应收紧为 0600。"""
    return private_key.private_bytes(
        encoding=serialization.Encoding.PEM,
        format=serialization.PrivateFormat.PKCS8,
        encryption_algorithm=serialization.NoEncryption(),
    )


def public_key_to_pem(public_key: Ed25519PublicKey) -> bytes:
    """导出 X.509 SubjectPublicKeyInfo PEM。"""
    return public_key.public_bytes(
        encoding=serialization.Encoding.PEM,
        format=serialization.PublicFormat.SubjectPublicKeyInfo,
    )


def load_private_key(path: str | Path) -> Ed25519PrivateKey:
    try:
        data = Path(path).read_bytes()
    except OSError as exc:
        raise KeyStoreError(f"无法读取私钥文件 {path}: {exc}") from exc
    try:
        key = serialization.load_pem_private_key(data, password=None)
    except Exception as exc:  # cryptography 抛多种异常类型, 统一收口
        raise KeyStoreError(f"私钥解析失败 {path}: {exc}") from exc
    if not isinstance(key, Ed25519PrivateKey):
        raise KeyStoreError(f"{path} 不是 Ed25519 私钥")
    return key


def load_public_key_pem(data: bytes) -> Ed25519PublicKey:
    try:
        key = serialization.load_pem_public_key(data)
    except Exception as exc:
        raise KeyStoreError(f"公钥解析失败: {exc}") from exc
    if not isinstance(key, Ed25519PublicKey):
        raise KeyStoreError("信任库中存在非 Ed25519 公钥")
    return key


# ---------- 签名原语 ----------


def sign(private_key: Ed25519PrivateKey, message: bytes) -> bytes:
    if not isinstance(message, bytes):
        raise SignatureError("待签名消息必须是 bytes")
    return private_key.sign(message)


def verify_signature(
    public_key: Ed25519PublicKey, signature: bytes, message: bytes
) -> None:
    """校验通过静默返回; 失败抛 :class:`SignatureError`。"""
    try:
        public_key.verify(signature, message)
    except InvalidSignature as exc:
        raise SignatureError("Ed25519 签名校验失败") from exc


# ---------- 信任库 ----------


@dataclass(frozen=True)
class TrustedKey:
    key_id: str
    public_key: Ed25519PublicKey
    source: str  # 来源文件, 仅用于日志/报错


class TrustStore:
    """本地可信公钥集合。

    从一个目录 (所有 ``*.pem`` 公钥) 或单个 PEM 文件加载。
    信任完全由本地文件决定 —— 没有网络拉取, 没有隐式信任。
    重复 key_id 视为配置错误直接报错。
    """

    def __init__(self) -> None:
        self._keys: dict[str, TrustedKey] = {}

    @classmethod
    def load(cls, path: str | Path) -> "TrustStore":
        store = cls()
        p = Path(path)
        if p.is_dir():
            pem_files = sorted(
                q for q in p.iterdir() if q.is_file() and q.suffix == ".pem"
            )
            if not pem_files:
                raise KeyStoreError(f"信任库目录中没有 *.pem 公钥: {p}")
            for q in pem_files:
                store._add_pem(q)
        elif p.is_file():
            store._add_pem(p)
        else:
            raise KeyStoreError(f"信任库路径不存在: {p}")
        return store

    def _add_pem(self, path: Path) -> None:
        data = path.read_bytes()
        key = load_public_key_pem(data)
        kid = public_key_id(key)
        if kid in self._keys:
            raise KeyStoreError(
                f"信任库中存在重复的密钥 ID {kid}: "
                f"{self._keys[kid].source} 与 {path}"
            )
        self._keys[kid] = TrustedKey(key_id=kid, public_key=key, source=str(path))

    def get(self, key_id: str) -> TrustedKey:
        if not is_valid_key_id(key_id):
            raise UnknownKeyError(f"密钥 ID 格式非法: {key_id!r}")
        try:
            return self._keys[key_id]
        except KeyError:
            raise UnknownKeyError(
                f"未知/不受信的密钥 ID: {key_id} (本地信任库共 {len(self._keys)} 把)"
            ) from None

    def key_ids(self) -> list[str]:
        return sorted(self._keys)

    def __len__(self) -> int:
        return len(self._keys)
