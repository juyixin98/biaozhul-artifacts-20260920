"""制品清单的构建、签名与读取。

清单信封（envelope）结构::

    {
      "signed": { ... 受签名字段 ... },
      "signatures": [
        {"key_id": "ed25519-sha256:...", "algorithm": "ed25519", "signature": "<b64>"}
      ]
    }

被签名的是 ``signed`` 对象独立规范化（canonical JSON）后的 UTF-8 字节，
因此磁盘上的信封可以任意排版/字段重排，不影响验签。
"""

from __future__ import annotations

import hashlib
import json
import os
import secrets
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Iterable

from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey

from . import DIGEST_ALGORITHM, SIGNATURE_ALGORITHM
from .canonjson import canonical_bytes, parse
from .errors import ManifestError, ManifestSchemaError
from .keys import StoredPublicKey, b64e, public_from_private
from .paths import resolve_within, walk_artifact_files

# 信封固定字段，严格 schema：多字段或少字段都拒绝。
_ENVELOPE_FIELDS = frozenset({"signed", "signatures"})
_SIGNATURE_FIELDS = frozenset({"key_id", "algorithm", "signature"})
_SIGNED_ALLOWED = frozenset(
    {
        "manifest_version",
        "type",
        "artifact_name",
        "created_at",
        "nonce",
        "files",
        "entrypoint",
    }
)
_SIGNED_REQUIRED = frozenset(
    {
        "manifest_version",
        "type",
        "artifact_name",
        "created_at",
        "nonce",
        "files",
    }
)
_FILE_FIELDS = frozenset({"path", "size", "digest_algorithm", "digest"})

_MAX_NAME_LEN = 256
_MAX_ENTRYPOINT_PARTS = 1024


@dataclass(frozen=True)
class FileEntry:
    path: str
    size: int
    digest: str  # 十六进制

    def to_dict(self) -> dict[str, Any]:
        return {
            "path": self.path,
            "size": self.size,
            "digest_algorithm": DIGEST_ALGORITHM,
            "digest": self.digest,
        }


def sha256_file(root: str | os.PathLike[str], rel_path: str) -> tuple[int, str, bytes]:
    """返回 (size, sha256hex, 全部字节)。路径必须先通过安全解析。"""
    resolved = resolve_within(root, rel_path)
    if not resolved.is_file():
        raise ManifestError(f"制品条目不是普通文件（或不存在）: {rel_path}")
    hasher = hashlib.sha256()
    size = 0
    chunks: list[bytes] = []
    with resolved.open("rb") as fh:
        while True:
            chunk = fh.read(1024 * 1024)
            if not chunk:
                break
            hasher.update(chunk)
            size += len(chunk)
            chunks.append(chunk)
    return size, hasher.hexdigest(), b"".join(chunks)


def utc_now_iso() -> str:
    """秒级 UTC 时间，Z 后缀，明确时区。"""
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def _validate_name(name: str) -> str:
    if not isinstance(name, str) or not name:
        raise ManifestSchemaError("artifact_name 必须是非空字符串")
    if len(name) > _MAX_NAME_LEN:
        raise ManifestSchemaError(f"artifact_name 长度不能超过 {_MAX_NAME_LEN}")
    if any(ord(c) < 0x20 for c in name):
        raise ManifestSchemaError("artifact_name 不能包含控制字符")
    return name


def collect_files(root: str | os.PathLike[str]) -> list[FileEntry]:
    """枚举并摘要制品根目录下的全部普通文件。"""
    entries: list[FileEntry] = []
    for rel in walk_artifact_files(root):
        size, digest, _ = sha256_file(root, rel)
        entries.append(FileEntry(path=rel, size=size, digest=digest))
    # 再次按路径排序（walk 已排序，这里是确定性双保险）。
    entries.sort(key=lambda e: e.path)
    return entries


def build_signed(
    artifact_root: str | os.PathLike[str],
    artifact_name: str,
    *,
    entrypoint: list[str] | None = None,
    created_at: str | None = None,
    nonce: str | None = None,
) -> dict[str, Any]:
    """构造受签名的 ``signed`` 对象（尚未签名）。"""
    _validate_name(artifact_name)
    if entrypoint is not None:
        entrypoint = _validate_entrypoint(entrypoint)

    files = [entry.to_dict() for entry in collect_files(artifact_root)]

    if entrypoint is not None:
        # 入口必须指向清单内的一个实际文件，防止"签了一堆文件但跑的是别的"。
        file_paths = {f["path"] for f in files}
        target = entrypoint[0]
        if target not in file_paths:
            raise ManifestSchemaError(
                f"entrypoint 目标 {target!r} 不在制品文件清单中"
            )

    signed: dict[str, Any] = {
        "manifest_version": 1,
        "type": "artifact-manifest/v1",
        "artifact_name": artifact_name,
        "created_at": created_at or utc_now_iso(),
        "nonce": nonce or secrets.token_hex(16),
        "files": files,
    }
    if entrypoint is not None:
        signed["entrypoint"] = entrypoint
    return signed


def _validate_entrypoint(entrypoint: Any) -> list[str]:
    if not isinstance(entrypoint, list) or not entrypoint:
        raise ManifestSchemaError("entrypoint 必须是非空字符串数组（argv 形式）")
    if len(entrypoint) > _MAX_ENTRYPOINT_PARTS:
        raise ManifestSchemaError("entrypoint 参数数量过多")
    for i, part in enumerate(entrypoint):
        if not isinstance(part, str) or not part:
            raise ManifestSchemaError(f"entrypoint 第 {i} 项必须是非空字符串")
        if "\x00" in part:
            raise ManifestSchemaError("entrypoint 不能包含 NUL 字符")
    # 入口可执行文件必须是制品内安全相对路径。
    # 延迟导入避免与 paths 模块形成概念循环。
    from .paths import validate_relative

    validate_relative(entrypoint[0])
    return list(entrypoint)


def sign_signed(
    signed: dict[str, Any],
    private_key: Ed25519PrivateKey,
) -> dict[str, Any]:
    """对 ``signed`` 做规范化并签名，返回完整信封。"""
    payload = canonical_bytes(signed)
    signature = private_key.sign(payload)
    pub: StoredPublicKey = public_from_private(private_key)
    return {
        "signed": signed,
        "signatures": [
            {
                "key_id": pub.key_id,
                "algorithm": SIGNATURE_ALGORITHM,
                "signature": b64e(signature),
            }
        ],
    }


def sign_artifact(
    artifact_root: str | os.PathLike[str],
    artifact_name: str,
    private_key: Ed25519PrivateKey,
    *,
    entrypoint: list[str] | None = None,
) -> dict[str, Any]:
    """一步完成：收集文件 → 构造 signed → 签名。"""
    signed = build_signed(artifact_root, artifact_name, entrypoint=entrypoint)
    return sign_signed(signed, private_key)


def write_envelope(envelope: dict[str, Any], path: str | os.PathLike[str]) -> Path:
    """把信封写成人类可读的缩进 JSON（不影响验签，验签以规范化字节为准）。"""
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(
        json.dumps(envelope, ensure_ascii=False, indent=2, sort_keys=False) + "\n",
        encoding="utf-8",
    )
    return path


def signed_payload_bytes(signed: dict[str, Any]) -> bytes:
    """供验证侧复用的规范化签名载荷。"""
    return canonical_bytes(signed)


# ---------------------------------------------------------------------------
# 严格读取与 schema 校验（验证侧使用）
# ---------------------------------------------------------------------------


def _require_fields(
    obj: dict[str, Any],
    allowed: frozenset[str],
    what: str,
    required: frozenset[str] | None = None,
) -> None:
    keys = set(obj.keys())
    missing = (required if required is not None else allowed) - keys
    extra = keys - allowed
    if missing:
        raise ManifestSchemaError(f"{what} 缺少字段: {sorted(missing)}")
    if extra:
        raise ManifestSchemaError(f"{what} 存在未知字段: {sorted(extra)}")


def _is_plain_int(value: Any) -> bool:
    # bool 是 int 的子类，必须先排除。
    return isinstance(value, int) and not isinstance(value, bool)


def _check_iso_utc(value: Any) -> None:
    if not isinstance(value, str):
        raise ManifestSchemaError("created_at 必须是字符串")
    try:
        dt = datetime.strptime(value, "%Y-%m-%dT%H:%M:%SZ").replace(tzinfo=timezone.utc)
    except ValueError:
        raise ManifestSchemaError("created_at 必须是 YYYY-MM-DDTHH:MM:SSZ 形式的 UTC 时间") from None
    if not (2000 <= dt.year <= 9999):
        raise ManifestSchemaError("created_at 年份超出合理范围")


def validate_envelope(envelope: Any) -> dict[str, Any]:
    """对已解析的信封做完整 schema 校验（严格字段集合），原样返回信封。"""
    if not isinstance(envelope, dict):
        raise ManifestSchemaError("清单信封顶层必须是 JSON 对象")
    _require_fields(envelope, _ENVELOPE_FIELDS, "清单信封")

    signed = envelope["signed"]
    sigs = envelope["signatures"]
    if not isinstance(signed, dict):
        raise ManifestSchemaError("signed 必须是对象")
    if not isinstance(sigs, list) or not sigs:
        raise ManifestSchemaError("signatures 必须是非空数组")

    _require_fields(
        signed, _SIGNED_ALLOWED, "signed", required=_SIGNED_REQUIRED
    )
    if signed.get("manifest_version") != 1:
        raise ManifestSchemaError("manifest_version 必须为 1")
    if signed.get("type") != "artifact-manifest/v1":
        raise ManifestSchemaError("type 必须为 artifact-manifest/v1")
    _validate_name(signed.get("artifact_name", ""))
    _check_iso_utc(signed.get("created_at"))
    nonce = signed.get("nonce")
    if not isinstance(nonce, str) or not nonce or len(nonce) > 128:
        raise ManifestSchemaError("nonce 必须是长度 1..128 的非空字符串")

    files = signed.get("files")
    if not isinstance(files, list):
        raise ManifestSchemaError("files 必须是数组")
    seen_paths: set[str] = set()
    for i, f in enumerate(files):
        if not isinstance(f, dict):
            raise ManifestSchemaError(f"files[{i}] 必须是对象")
        _require_fields(f, _FILE_FIELDS, f"files[{i}]")
        path = f["path"]
        if not isinstance(path, str):
            raise ManifestSchemaError(f"files[{i}].path 必须是字符串")
        # 路径词法校验（../、绝对路径等在这里被拒）。
        from .paths import validate_relative

        validate_relative(path)
        if path in seen_paths:
            raise ManifestSchemaError(f"files 中路径重复: {path}")
        seen_paths.add(path)
        if not _is_plain_int(f["size"]) or f["size"] < 0:
            raise ManifestSchemaError(f"files[{i}].size 必须是非负整数")
        if f["digest_algorithm"] != DIGEST_ALGORITHM:
            raise ManifestSchemaError(
                f"files[{i}].digest_algorithm 只支持 {DIGEST_ALGORITHM}"
            )
        digest = f["digest"]
        if not isinstance(digest, str) or len(digest) != 64:
            raise ManifestSchemaError(f"files[{i}].digest 必须是 64 位十六进制字符串")
        if any(c not in "0123456789abcdef" for c in digest):
            raise ManifestSchemaError(f"files[{i}].digest 必须是小写十六进制")

    ep = signed.get("entrypoint")
    if ep is not None:
        _validate_entrypoint(ep)
        if ep[0] not in seen_paths:
            raise ManifestSchemaError("entrypoint 目标不在 files 清单中")

    for i, sig in enumerate(sigs):
        if not isinstance(sig, dict):
            raise ManifestSchemaError(f"signatures[{i}] 必须是对象")
        _require_fields(sig, _SIGNATURE_FIELDS, f"signatures[{i}]")
        if not isinstance(sig["key_id"], str) or not sig["key_id"].startswith("ed25519-"):
            raise ManifestSchemaError(f"signatures[{i}].key_id 非法")
        if sig["algorithm"] != SIGNATURE_ALGORITHM:
            raise ManifestSchemaError(f"signatures[{i}].algorithm 必须为 {SIGNATURE_ALGORITHM}")
        if not isinstance(sig["signature"], str) or not sig["signature"]:
            raise ManifestSchemaError(f"signatures[{i}].signature 必须是非空 base64 字符串")
        from .keys import b64d

        raw_sig = b64d(sig["signature"])
        if len(raw_sig) != 64:
            raise ManifestSchemaError(
                f"signatures[{i}].signature 解码后必须为 64 字节（Ed25519），得到 {len(raw_sig)}"
            )

    return envelope


def parse_envelope(text: str | bytes) -> dict[str, Any]:
    """严格解析信封：重复键检测 + 完整 schema 校验。"""
    envelope = parse(text)  # canonjson.parse：重复键/BOM/NaN 等在此被拒
    return validate_envelope(envelope)

    return envelope


def load_envelope(path: str | os.PathLike[str]) -> dict[str, Any]:
    path = Path(path)
    try:
        data = path.read_bytes()
    except OSError as exc:
        raise ManifestError(f"无法读取清单文件 {path}: {exc}") from exc
    return parse_envelope(data)


def file_entries(signed: dict[str, Any]) -> Iterable[dict[str, Any]]:
    return signed["files"]
