"""离线签名: 构建清单 -> Ed25519 签名 -> 输出封套。"""

from __future__ import annotations

from pathlib import Path

from . import canonical
from .keys import (
    generate_private_key,
    load_private_key,
    private_key_to_pem,
    public_key_id,
    public_key_to_pem,
    sign,
)
from .manifest import build_manifest, make_envelope, manifest_signing_bytes


def sign_artifact(
    root: str | Path,
    private_key,
    entrypoint: str | None = None,
    created_at: str | None = None,
) -> dict:
    """对制品目录签名, 返回 envelope dict。"""
    key_id = public_key_id(private_key.public_key())
    manifest = build_manifest(root, key_id, entrypoint=entrypoint, created_at=created_at)
    raw_signature = sign(private_key, manifest_signing_bytes(manifest))
    return make_envelope(manifest, raw_signature)


def generate_keypair_files(private_key_path: str | Path) -> str:
    """生成一把测试用 Ed25519 密钥, 落盘私钥并返回公钥 PEM。

    私钥文件权限收紧为 0600; 返回值为公钥 PEM 文本。
    """
    private_key = generate_private_key()
    pk_path = Path(private_key_path)
    pk_path.parent.mkdir(parents=True, exist_ok=True)
    pk_path.write_bytes(private_key_to_pem(private_key))
    pk_path.chmod(0o600)
    return public_key_to_pem(private_key.public_key()).decode("ascii")


def sign_path(
    root: str | Path,
    private_key_path: str | Path,
    entrypoint: str | None = None,
) -> tuple[dict, bytes]:
    """便捷入口: 读私钥 -> 签名 -> 返回 (envelope dict, 落盘用 pretty bytes)。"""
    private_key = load_private_key(private_key_path)
    envelope = sign_artifact(root, private_key, entrypoint=entrypoint)
    return envelope, canonical.pretty_json(envelope)
