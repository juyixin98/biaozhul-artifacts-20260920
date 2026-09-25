"""制品清单 (manifest) 与签名封套 (envelope) 的构建。

数据模型 (均以 JSON 表示)::

    manifest = {
      "sam_version": 1,
      "algorithm": "ed25519",
      "key_id": "<64 hex>",
      "created_at": "2026-09-24T15:15:47Z",
      "entrypoint": "bin/app.sh" | None,
      "files": [
        {"path": "relative/path", "sha256": "<64 hex>", "size": 123},
        ...
      ]
    }

    envelope = {
      "sam_version": 1,
      "algorithm": "ed25519",
      "manifest": <manifest 对象原样内嵌>,
      "signature": "<base64 编码的 Ed25519 签名>"
    }

签名精确覆盖 ``canonical_bytes(envelope["manifest"])``。
封套自身的空白、字段顺序不影响验签 (字段重排安全)。
"""

from __future__ import annotations

import base64
import datetime as _dt
import hashlib
from pathlib import Path

from . import canonical
from .errors import ManifestError
from .keys import ALGORITHM
from .safepaths import iter_artifact_files, resolve_within

SAM_VERSION = 1
_HASH_CHUNK = 1024 * 1024


def utc_now_stamp() -> str:
    """UTC 时间戳, 精确到秒, 末尾 Z (ISO 8601)。"""
    return (
        _dt.datetime.now(tz=_dt.timezone.utc)
        .replace(microsecond=0)
        .strftime("%Y-%m-%dT%H:%M:%SZ")
    )


def sha256_file(path: Path) -> tuple[str, int]:
    """流式计算文件 SHA-256 (hex) 与字节数; 以二进制读取, 不做文本转换。"""
    digest = hashlib.sha256()
    size = 0
    with open(path, "rb") as fh:
        while True:
            chunk = fh.read(_HASH_CHUNK)
            if not chunk:
                break
            digest.update(chunk)
            size += len(chunk)
    return digest.hexdigest(), size


def is_sha256_hex(value: object) -> bool:
    return (
        isinstance(value, str)
        and len(value) == 64
        and all(c in "0123456789abcdef" for c in value)
    )


def build_manifest(
    root: str | Path,
    key_id: str,
    entrypoint: str | None = None,
    created_at: str | None = None,
) -> dict:
    """枚举制品根并构建待签名的 manifest 对象。"""
    if entrypoint is not None:
        # 提前词法检查; 是否实际存在/在 files 中由验证端再判定
        from .safepaths import validate_relative

        validate_relative(entrypoint)

    files: list[dict] = []
    for rel in iter_artifact_files(root):
        real = resolve_within(root, rel)
        if not real.is_file():
            raise ManifestError(f"制品条目不是普通文件: {rel!r}")
        digest, size = sha256_file(real)
        files.append({"path": rel, "sha256": digest, "size": size})

    # iter_artifact_files 已排序且唯一, 这里再防御性确认
    paths = [f["path"] for f in files]
    if len(set(paths)) != len(paths):
        raise ManifestError("内部错误: 文件列表出现重复路径")

    return {
        "sam_version": SAM_VERSION,
        "algorithm": ALGORITHM,
        "key_id": key_id,
        "created_at": created_at or utc_now_stamp(),
        "entrypoint": entrypoint,
        "files": files,
    }


def manifest_signing_bytes(manifest: dict) -> bytes:
    """签名覆盖的精确字节: 规范化后的 manifest。"""
    return canonical.canonical_bytes(manifest)


def encode_signature(raw_signature: bytes) -> str:
    return base64.b64encode(raw_signature).decode("ascii")


def decode_signature(text: str) -> bytes:
    if not isinstance(text, str):
        raise ManifestError("signature 必须是 base64 字符串")
    try:
        raw = base64.b64decode(text, validate=True)
    except Exception as exc:
        raise ManifestError("signature 不是合法 base64") from exc
    if not raw:
        raise ManifestError("signature 为空")
    return raw


def make_envelope(manifest: dict, raw_signature: bytes) -> dict:
    return {
        "sam_version": SAM_VERSION,
        "algorithm": manifest["algorithm"],
        "manifest": manifest,
        "signature": encode_signature(raw_signature),
    }
