"""本地测试密钥管理。

仅用于本地/测试：密钥文件权限 0600，缺失时自动生成。
- sign_key：Ed25519 私钥，用于导出包签名
- transform_secret：32 字节随机数，HMAC 泛化原语的根密钥

不接入任何生产账号或 KMS；文件格式是纯 JSON（私钥以 hex 存储），
便于检查、删除和重新生成。
"""

from __future__ import annotations

import json
import os
from dataclasses import dataclass
from pathlib import Path

from cryptography.hazmat.primitives.asymmetric.ed25519 import (
    Ed25519PrivateKey,
    Ed25519PublicKey,
)
from cryptography.hazmat.primitives.serialization import (
    Encoding,
    PublicFormat,
)

KEYS_VERSION = "mde/test-keys@v1"


class KeyError_(RuntimeError):
    pass


@dataclass
class LocalKeys:
    sign_private: Ed25519PrivateKey
    transform_secret: bytes
    key_id: str

    @property
    def sign_public(self) -> Ed25519PublicKey:
        return self.sign_private.public_key()

    def public_spki_hex(self) -> str:
        return self.sign_public.public_bytes(
            Encoding.DER, PublicFormat.SubjectPublicKeyInfo
        ).hex()


def default_key_dir() -> Path:
    return Path(os.environ.get("MDE_KEY_DIR", ".mde-keys"))


def generate_keys() -> LocalKeys:
    """生成全新本地测试密钥。"""
    priv = Ed25519PrivateKey.generate()
    secret = os.urandom(32)
    # key_id 取公钥前 8 字节，仅用于识别，不承担安全属性
    pub_raw = priv.public_key().public_bytes(Encoding.Raw, PublicFormat.Raw)
    return LocalKeys(priv, secret, key_id="kid-" + pub_raw[:8].hex())


def save_keys(keys: LocalKeys, key_dir: Path) -> Path:
    key_dir.mkdir(parents=True, exist_ok=True)
    path = key_dir / "test-keys.json"
    payload = {
        "version": KEYS_VERSION,
        "key_id": keys.key_id,
        "sign_private_seed_hex": keys.sign_private.private_bytes_raw().hex(),
        "transform_secret_hex": keys.transform_secret.hex(),
        "note": "LOCAL TEST KEYS ONLY — do not use in production",
    }
    tmp = path.with_suffix(".tmp")
    tmp.write_text(json.dumps(payload, indent=2), encoding="utf-8")
    os.chmod(tmp, 0o600)
    os.replace(tmp, path)
    os.chmod(path, 0o600)
    return path


def load_or_create(key_dir: Path | None = None) -> tuple[LocalKeys, bool]:
    """返回 ``(keys, created)``；目录/文件缺失则生成。"""
    key_dir = key_dir or default_key_dir()
    path = key_dir / "test-keys.json"
    if not path.exists():
        keys = generate_keys()
        save_keys(keys, key_dir)
        return keys, True
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except (json.JSONDecodeError, OSError) as exc:
        raise KeyError_(f"密钥文件无法读取: {path}: {exc}") from exc
    if payload.get("version") != KEYS_VERSION:
        raise KeyError_(f"密钥文件版本不受支持: {payload.get('version')!r}")
    priv = Ed25519PrivateKey.from_private_bytes(
        bytes.fromhex(payload["sign_private_seed_hex"])
    )
    secret = bytes.fromhex(payload["transform_secret_hex"])
    if len(secret) != 32:
        raise KeyError_("transform_secret 必须是 32 字节")
    return LocalKeys(priv, secret, payload["key_id"]), False
