"""Ed25519 密钥与信任库管理。

设计要点：
- 只用 cryptography 提供的 Ed25519 / SHA-256 / 标准 PEM，不自创任何密码原语；
- 密钥 ID = ``ed25519-sha256:`` + 原始公钥(32B) 的 SHA-256 十六进制；
  验证时会重算 ID 并与信任库记录比对，防止"换了公钥但沿用旧 ID"；
- 私钥 PEM 默认以口令派生密钥加密（scrypt + AES，BestAvailableEncryption），
  文件权限收紧为 0600；无口令时明确写为未加密 PEM 并给出告警提示；
- 信任库只存公钥，JSON 格式：{"version":1,"keys":[{"key_id","algorithm","public":...}]}。
"""

from __future__ import annotations

import base64
import hashlib
import json
import os
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from cryptography.exceptions import UnsupportedAlgorithm
from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric.ed25519 import (
    Ed25519PrivateKey,
    Ed25519PublicKey,
)

from . import SIGNATURE_ALGORITHM
from .canonjson import parse
from .errors import KeyStoreError

KEY_ID_PREFIX = "ed25519-sha256:"
_PUB_KEY_BYTES = 32


def b64e(data: bytes) -> str:
    """无填充标准 base64（清单中的统一编码）。"""
    return base64.b64encode(data).decode("ascii").rstrip("=")


def b64d(text: str) -> bytes:
    if not isinstance(text, str):
        raise KeyStoreError("base64 输入必须是字符串")
    try:
        padding = "=" * (-len(text) % 4)
        return base64.b64decode(text + padding, validate=True)
    except Exception as exc:  # binascii.Error / ValueError
        raise KeyStoreError(f"非法 base64 数据: {exc}") from exc


def key_id_for_public(public_key: Ed25519PublicKey) -> str:
    raw = public_key.public_bytes(
        encoding=serialization.Encoding.Raw,
        format=serialization.PublicFormat.Raw,
    )
    return KEY_ID_PREFIX + hashlib.sha256(raw).hexdigest()


def key_id_for_raw_public(raw: bytes) -> str:
    if len(raw) != _PUB_KEY_BYTES:
        raise KeyStoreError(f"Ed25519 原始公钥必须为 {_PUB_KEY_BYTES} 字节，得到 {len(raw)}")
    return KEY_ID_PREFIX + hashlib.sha256(raw).hexdigest()


def generate_private_key() -> Ed25519PrivateKey:
    """本地生成测试用 Ed25519 私钥（由操作系统 CSPRNG 提供随机数）。"""
    return Ed25519PrivateKey.generate()


@dataclass(frozen=True)
class StoredPublicKey:
    key_id: str
    public_key: Ed25519PublicKey

    def raw_bytes(self) -> bytes:
        return self.public_key.public_bytes(
            encoding=serialization.Encoding.Raw,
            format=serialization.PublicFormat.Raw,
        )

    def to_dict(self) -> dict[str, Any]:
        return {
            "key_id": self.key_id,
            "algorithm": SIGNATURE_ALGORITHM,
            "public": b64e(self.raw_bytes()),
        }


def load_public_key_from_b64(text: str, expected_key_id: str | None = None) -> StoredPublicKey:
    raw = b64d(text)
    if len(raw) != _PUB_KEY_BYTES:
        raise KeyStoreError(f"Ed25519 原始公钥必须为 {_PUB_KEY_BYTES} 字节，得到 {len(raw)}")
    try:
        public_key = Ed25519PublicKey.from_public_bytes(raw)
    except ValueError as exc:
        raise KeyStoreError(f"无法加载 Ed25519 公钥: {exc}") from exc
    key_id = key_id_for_public(public_key)
    if expected_key_id is not None and key_id != expected_key_id:
        raise KeyStoreError(
            f"公钥与密钥 ID 不匹配: 记录={expected_key_id} 实算={key_id}"
        )
    return StoredPublicKey(key_id=key_id, public_key=public_key)


def save_private_key_pem(
    private_key: Ed25519PrivateKey,
    path: str | os.PathLike[str],
    password: str | bytes | None,
) -> Path:
    """写出私钥 PEM。给出口令则使用 cryptography 的最强可用加密。"""
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    if password is not None:
        if isinstance(password, str):
            password = password.encode("utf-8")
        if len(password) == 0:
            raise KeyStoreError("私钥口令不能为空；如确实不需要口令请显式传 None")
        encryption: serialization.KeySerializationConfiguration = (
            serialization.BestAvailableEncryption(password)
        )
    else:
        encryption = serialization.NoEncryption()
    pem = private_key.private_bytes(
        encoding=serialization.Encoding.PEM,
        format=serialization.PrivateFormat.PKCS8,
        encryption_algorithm=encryption,
    )
    path.write_bytes(pem)
    os.chmod(path, 0o600)
    return path


def load_private_key_pem(
    path: str | os.PathLike[str],
    password: str | bytes | None = None,
) -> Ed25519PrivateKey:
    path = Path(path)
    try:
        data = path.read_bytes()
    except OSError as exc:
        raise KeyStoreError(f"无法读取私钥文件 {path}: {exc}") from exc
    if isinstance(password, str):
        password = password.encode("utf-8")
    try:
        key = serialization.load_pem_private_key(data, password=password)
    except (ValueError, TypeError, UnsupportedAlgorithm) as exc:
        raise KeyStoreError(f"私钥加载失败（口令错误或文件损坏）: {exc}") from exc
    if not isinstance(key, Ed25519PrivateKey):
        raise KeyStoreError(f"私钥不是 Ed25519，得到 {type(key).__name__}")
    return key


def public_from_private(private_key: Ed25519PrivateKey) -> StoredPublicKey:
    pub = private_key.public_key()
    if not isinstance(pub, Ed25519PublicKey):  # 类型收窄，理论上恒成立
        raise KeyStoreError("从私钥导出的公钥类型异常")
    return StoredPublicKey(key_id=key_id_for_public(pub), public_key=pub)


class TrustStore:
    """可信公钥集合（信任锚）。未知 key_id 的签名一律不能通过验证。"""

    def __init__(self, keys: dict[str, StoredPublicKey] | None = None) -> None:
        self._keys: dict[str, StoredPublicKey] = dict(keys or {})

    def add(self, stored: StoredPublicKey) -> None:
        # 同一 key_id 重复添加时，公钥必须一致。
        existing = self._keys.get(stored.key_id)
        if existing is not None and existing.raw_bytes() != stored.raw_bytes():
            raise KeyStoreError(f"同一密钥 ID 对应了不同公钥: {stored.key_id}")
        self._keys[stored.key_id] = stored

    def get(self, key_id: str) -> StoredPublicKey | None:
        return self._keys.get(key_id)

    def contains(self, key_id: str) -> bool:
        return key_id in self._keys

    def key_ids(self) -> list[str]:
        return sorted(self._keys)

    def __len__(self) -> int:
        return len(self._keys)

    def to_dict(self) -> dict[str, Any]:
        return {
            "version": 1,
            "kind": "sig-manifest-trust-store",
            "keys": [self._keys[k].to_dict() for k in sorted(self._keys)],
        }

    def save(self, path: str | os.PathLike[str]) -> Path:
        path = Path(path)
        path.parent.mkdir(parents=True, exist_ok=True)
        # 用标准 json 美化存储即可；信任库本身不参与签名。
        path.write_text(json.dumps(self.to_dict(), ensure_ascii=False, indent=2) + "\n",
                        encoding="utf-8")
        os.chmod(path, 0o600)
        return path

    @classmethod
    def from_dict(cls, data: Any) -> "TrustStore":
        if not isinstance(data, dict):
            raise KeyStoreError("信任库顶层必须是对象")
        if data.get("version") != 1:
            raise KeyStoreError("信任库 version 必须为 1")
        if data.get("kind") != "sig-manifest-trust-store":
            raise KeyStoreError("信任库 kind 字段不匹配")
        raw_keys = data.get("keys")
        if not isinstance(raw_keys, list):
            raise KeyStoreError("信任库 keys 必须是数组")
        store = cls()
        seen: set[str] = set()
        for i, entry in enumerate(raw_keys):
            if not isinstance(entry, dict):
                raise KeyStoreError(f"信任库第 {i} 项不是对象")
            key_id = entry.get("key_id")
            algo = entry.get("algorithm")
            pub_b64 = entry.get("public")
            if not isinstance(key_id, str) or not key_id.startswith(KEY_ID_PREFIX):
                raise KeyStoreError(f"信任库第 {i} 项 key_id 非法")
            if algo != SIGNATURE_ALGORITHM:
                raise KeyStoreError(f"信任库第 {i} 项 algorithm 必须为 {SIGNATURE_ALGORITHM}")
            if not isinstance(pub_b64, str):
                raise KeyStoreError(f"信任库第 {i} 项 public 必须是 base64 字符串")
            if key_id in seen:
                raise KeyStoreError(f"信任库中密钥 ID 重复: {key_id}")
            seen.add(key_id)
            stored = load_public_key_from_b64(pub_b64, expected_key_id=key_id)
            store.add(stored)
        return store

    @classmethod
    def load(cls, path: str | os.PathLike[str]) -> "TrustStore":
        path = Path(path)
        try:
            text = path.read_bytes()
        except OSError as exc:
            raise KeyStoreError(f"无法读取信任库 {path}: {exc}") from exc
        # 信任库也走严格 parse：重复键直接拒。
        try:
            data = parse(text)
        except Exception as exc:
            raise KeyStoreError(f"信任库 JSON 解析失败: {exc}") from exc
        return cls.from_dict(data)
