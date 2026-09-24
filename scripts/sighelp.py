"""本地签名辅助：测试密钥生成、制品签名、根轮换批准签名。

注意：私钥只存在于调用方本地（scripts/ 与测试），服务端从不接收私钥。
"""
from __future__ import annotations

import base64
import hashlib
import json
import secrets
from pathlib import Path

from cryptography.hazmat.primitives.asymmetric.ed25519 import (
    Ed25519PrivateKey,
    Ed25519PublicKey,
)

from app import canonical, keys
from app.models import ArtifactEnvelope, RootBody, SignatureBlock
from app.store import build_root


def b64e(raw: bytes) -> str:
    return base64.b64encode(raw).decode("ascii")


def sha256_hex(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


# ---------- 密钥生成 / 读写（仅测试用） ----------

def generate_party(
    out_dir: str | Path, name: str
) -> tuple[Ed25519PrivateKey, Ed25519PublicKey]:
    priv, pub = keys.generate_keypair()
    d = Path(out_dir)
    d.mkdir(parents=True, exist_ok=True)
    (d / f"{name}.priv.pem").write_text(keys.private_pem(priv), encoding="ascii")
    (d / f"{name}.pub.pem").write_text(keys.public_pem(pub), encoding="ascii")
    return priv, pub


def load_private(path: str | Path) -> Ed25519PrivateKey:
    return keys.load_private_pem(Path(path).read_text(encoding="ascii"))


def load_public(path: str | Path) -> Ed25519PublicKey:
    return keys.load_public_pem(Path(path).read_text(encoding="ascii"))


# ---------- 制品签名 ----------

def sign_artifact_block(
    private_key: Ed25519PrivateKey,
    digest_hex: str,
    artifact_type: str,
    version: str,
    nonce: bytes | None = None,
) -> SignatureBlock:
    nonce = nonce if nonce is not None else secrets.token_bytes(16)
    message = canonical.artifact_message(
        bytes.fromhex(digest_hex), artifact_type, version, nonce
    )
    signature = keys.sign(private_key, message)
    return SignatureBlock(
        public_key=keys.public_pem(private_key.public_key()),
        signature=b64e(signature),
        nonce=b64e(nonce),
    )


def make_envelope(
    content: bytes,
    artifact_type: str,
    version: str,
    private_keys: list[Ed25519PrivateKey],
) -> ArtifactEnvelope:
    digest = sha256_hex(content)
    blocks = [
        sign_artifact_block(priv, digest, artifact_type, version)
        for priv in private_keys
    ]
    return ArtifactEnvelope(
        digest=digest,
        artifact_type=artifact_type,
        version=version,
        signatures=blocks,
    )


# ---------- 信任根 ----------

def make_root_body(
    version: int,
    root_threshold: int,
    artifact_threshold: int,
    root_public: list[Ed25519PublicKey],
    artifact_public: list[Ed25519PublicKey],
) -> RootBody:
    return RootBody(
        version=version,
        root_threshold=root_threshold,
        artifact_threshold=artifact_threshold,
        root_signers=[keys.public_pem(k) for k in root_public],
        artifact_signers=[keys.public_pem(k) for k in artifact_public],
    )


def approval_block(
    private_key: Ed25519PrivateKey, new_root: RootBody, nonce: bytes | None = None
) -> SignatureBlock:
    """旧根角色密钥对新根描述符的批准签名。"""
    nonce = nonce if nonce is not None else secrets.token_bytes(16)
    descriptor = build_root(new_root).descriptor_bytes()
    message = canonical.root_approval_message(descriptor, nonce)
    signature = keys.sign(private_key, message)
    return SignatureBlock(
        public_key=keys.public_pem(private_key.public_key()),
        signature=b64e(signature),
        nonce=b64e(nonce),
    )


def dump_json(obj, path: str | Path) -> None:
    Path(path).write_text(
        json.dumps(obj, indent=2, ensure_ascii=False), encoding="utf-8"
    )
