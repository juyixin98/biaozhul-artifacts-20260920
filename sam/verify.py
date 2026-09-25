"""离线验证: 可信密钥 ID -> 签名 -> 路径安全 -> 逐文件摘要/大小。

验证是 *只读* 操作, 永不执行制品。验证结果以 :class:`VerifyReport`
返回 (而不是靠异常做正常控制流); 仅当封套连解析都做不到时,
报告里才会只有一条结构性问题。

执行制品 (sam.runner) 必须走 :func:`verify_or_raise`, 任何一条
问题都意味着绝不执行。
"""

from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path
import os

from . import canonical
from .errors import (
    CanonicalJSONError,
    ManifestError,
    SAMError,
    SignatureError,
    UnknownKeyError,
    UnsafePathError,
    UnsupportedAlgorithmError,
    UnsupportedFileTypeError,
)
from .keys import ALGORITHM, TrustStore, is_valid_key_id, verify_signature
from .manifest import (
    SAM_VERSION,
    decode_signature,
    is_sha256_hex,
    manifest_signing_bytes,
    sha256_file,
)
from .safepaths import iter_artifact_files, resolve_within, validate_relative

# ---- 问题代码 (稳定字符串, 便于程序化消费) ----
P_ENVELOPE_MALFORMED = "ENVELOPE_MALFORMED"
P_SCHEMA = "SCHEMA_VIOLATION"
P_UNKNOWN_ALGORITHM = "UNKNOWN_ALGORITHM"
P_UNKNOWN_KEY = "UNKNOWN_KEY"
P_SIGNATURE = "SIGNATURE_INVALID"
P_UNSAFE_PATH = "UNSAFE_PATH"
P_FILE_MISSING = "FILE_MISSING"
P_SIZE_MISMATCH = "SIZE_MISMATCH"
P_DIGEST_MISMATCH = "DIGEST_MISMATCH"
P_EXTRA_FILE = "EXTRA_FILE"
P_ENTRYPOINT = "ENTRYPOINT_INVALID"
P_ENTRYPOINT_EXEC = "ENTRYPOINT_NOT_EXECUTABLE"
P_UNSUPPORTED_FILE = "UNSUPPORTED_FILE_TYPE"

_ENVELOPE_KEYS = {"sam_version", "algorithm", "manifest", "signature"}
_MANIFEST_KEYS = {
    "sam_version",
    "algorithm",
    "key_id",
    "created_at",
    "entrypoint",
    "files",
}
_FILE_KEYS = {"path", "sha256", "size"}


@dataclass(frozen=True)
class VerifyProblem:
    code: str
    message: str
    path: str | None = None


@dataclass
class VerifyReport:
    ok: bool
    problems: list[VerifyProblem] = field(default_factory=list)
    key_id: str | None = None
    algorithm: str | None = None
    file_count: int = 0
    entrypoint: str | None = None

    def to_dict(self) -> dict:
        return {
            "ok": self.ok,
            "key_id": self.key_id,
            "algorithm": self.algorithm,
            "file_count": self.file_count,
            "entrypoint": self.entrypoint,
            "problems": [
                {"code": p.code, "message": p.message, "path": p.path}
                for p in self.problems
            ],
        }


def _is_nonneg_int(v) -> bool:
    return isinstance(v, int) and not isinstance(v, bool) and v >= 0


def _validate_manifest_shape(manifest: object, problems: list[VerifyProblem]) -> bool:
    """结构校验, 尽量多收集问题。返回 manifest 是否 *基本* 可继续处理。"""
    usable = True
    if not isinstance(manifest, dict):
        problems.append(
            VerifyProblem(P_SCHEMA, "envelope.manifest 必须是对象")
        )
        return False

    extra = set(manifest) - _MANIFEST_KEYS
    if extra:
        problems.append(
            VerifyProblem(P_SCHEMA, f"manifest 含未知字段: {sorted(extra)}")
        )

    if manifest.get("sam_version") != SAM_VERSION:
        problems.append(
            VerifyProblem(
                P_SCHEMA, f"manifest.sam_version 必须等于 {SAM_VERSION}"
            )
        )
        usable = False
    if manifest.get("algorithm") != ALGORITHM:
        problems.append(
            VerifyProblem(
                P_UNKNOWN_ALGORITHM,
                f"manifest.algorithm 不支持: {manifest.get('algorithm')!r}",
            )
        )
        usable = False

    kid = manifest.get("key_id")
    if not is_valid_key_id(kid):
        problems.append(
            VerifyProblem(P_SCHEMA, "manifest.key_id 不是 64 位小写十六进制")
        )
    if not isinstance(manifest.get("created_at"), str) or not manifest.get(
        "created_at"
    ):
        problems.append(VerifyProblem(P_SCHEMA, "manifest.created_at 必须是非空字符串"))

    ep = manifest.get("entrypoint", _MISSING)
    if ep is not None and not isinstance(ep, str):
        problems.append(
            VerifyProblem(P_SCHEMA, "manifest.entrypoint 必须是字符串或 null")
        )
    elif isinstance(ep, str):
        try:
            validate_relative(ep)
        except UnsafePathError as exc:
            problems.append(VerifyProblem(P_ENTRYPOINT, str(exc), path=ep))

    files = manifest.get("files", _MISSING)
    if not isinstance(files, list):
        problems.append(VerifyProblem(P_SCHEMA, "manifest.files 必须是数组"))
        return False

    seen: set[str] = set()
    for i, item in enumerate(files):
        where = f"files[{i}]"
        if not isinstance(item, dict):
            problems.append(VerifyProblem(P_SCHEMA, "文件条目必须是对象", path=where))
            usable = False
            continue
        if set(item) != _FILE_KEYS:
            problems.append(
                VerifyProblem(
                    P_SCHEMA,
                    f"文件条目字段必须恰为 {sorted(_FILE_KEYS)}",
                    path=where,
                )
            )
        rel = item.get("path")
        if not isinstance(rel, str):
            problems.append(
                VerifyProblem(P_SCHEMA, "文件条目 path 必须是字符串", path=where)
            )
            continue
        if not is_sha256_hex(item.get("sha256")):
            problems.append(
                VerifyProblem(
                    P_SCHEMA, "文件条目 sha256 不是 64 位小写十六进制", path=rel
                )
            )
        if not _is_nonneg_int(item.get("size")):
            problems.append(
                VerifyProblem(
                    P_SCHEMA, "文件条目 size 必须是非负整数", path=rel
                )
            )
        # 路径词法/解析安全不在此处报告, 交给逐文件检查阶段统一处理
        # (避免对同一路径重复报 UNSAFE_PATH)。
        if rel in seen:
            problems.append(
                VerifyProblem(P_SCHEMA, "files 中路径重复", path=rel)
            )
        seen.add(rel)

    return usable


_MISSING = object()


def verify(
    artifact_root: str | Path,
    envelope_bytes: bytes,
    trust_store: TrustStore,
    strict_extra: bool = True,
    require_executable_entrypoint: bool = False,
) -> VerifyReport:
    """执行完整验证链。永远不抛业务异常, 结果全部在报告中。"""
    problems: list[VerifyProblem] = []

    # 0) 严格解析封套 (重复键在这里就会被拒)
    try:
        envelope = canonical.parse_strict(envelope_bytes)
    except CanonicalJSONError as exc:
        return VerifyReport(
            ok=False,
            problems=[VerifyProblem(P_ENVELOPE_MALFORMED, str(exc))],
        )

    if not isinstance(envelope, dict):
        return VerifyReport(
            ok=False,
            problems=[
                VerifyProblem(P_ENVELOPE_MALFORMED, "封套顶层必须是 JSON 对象")
            ],
        )

    extra = set(envelope) - _ENVELOPE_KEYS
    if extra:
        problems.append(
            VerifyProblem(P_SCHEMA, f"封套含未知字段: {sorted(extra)}")
        )

    if envelope.get("sam_version") != SAM_VERSION:
        problems.append(
            VerifyProblem(P_SCHEMA, f"envelope.sam_version 必须等于 {SAM_VERSION}")
        )

    env_alg = envelope.get("algorithm")
    if env_alg != ALGORITHM:
        problems.append(
            VerifyProblem(
                P_UNKNOWN_ALGORITHM,
                f"envelope.algorithm 不支持: {env_alg!r} (本版本仅支持 {ALGORITHM})",
            )
        )

    try:
        raw_signature = decode_signature(envelope.get("signature", ""))
    except ManifestError as exc:
        raw_signature = b""
        problems.append(VerifyProblem(P_SIGNATURE, str(exc)))

    # 1) manifest 结构
    manifest = envelope.get("manifest")
    shape_ok = _validate_manifest_shape(manifest, problems)

    report_meta = {}
    if isinstance(manifest, dict):
        report_meta = {
            "key_id": manifest.get("key_id") if is_valid_key_id(
                manifest.get("key_id")
            )
            else None,
            "algorithm": manifest.get("algorithm"),
            "file_count": len(manifest.get("files", []))
            if isinstance(manifest.get("files"), list)
            else 0,
            "entrypoint": manifest.get("entrypoint")
            if isinstance(manifest.get("entrypoint"), str)
            else None,
        }

    # 2) 可信密钥 ID
    trusted = None
    kid = manifest.get("key_id") if isinstance(manifest, dict) else None
    if is_valid_key_id(kid):
        try:
            trusted = trust_store.get(kid)
        except UnknownKeyError as exc:
            problems.append(VerifyProblem(P_UNKNOWN_KEY, str(exc)))

    # 3) 签名 (针对 canonical(manifest); 与封套字段顺序/空白无关)
    if (
        shape_ok
        and trusted is not None
        and raw_signature
        and env_alg == ALGORITHM
        and isinstance(manifest, dict)
        and manifest.get("algorithm") == ALGORITHM
    ):
        try:
            verify_signature(
                trusted.public_key,
                raw_signature,
                manifest_signing_bytes(manifest),
            )
        except SignatureError as exc:
            problems.append(
                VerifyProblem(
                    P_SIGNATURE,
                    f"{exc} (key_id={kid})",
                )
            )

    # 4) 逐文件: 路径安全 + 存在性 + 大小 + 摘要
    listed: set[str] = set()
    if isinstance(manifest, dict) and isinstance(manifest.get("files"), list):
        for item in manifest["files"]:
            if not isinstance(item, dict) or not isinstance(item.get("path"), str):
                continue
            rel = item["path"]
            listed.add(rel)  # 即便词法不安全也加入, 避免后续噪声
            try:
                real = resolve_within(artifact_root, rel)
            except UnsafePathError as exc:
                problems.append(VerifyProblem(P_UNSAFE_PATH, str(exc), path=rel))
                continue

            if not real.is_file():
                problems.append(
                    VerifyProblem(
                        P_FILE_MISSING,
                        f"制品中缺失该文件 (或不是普通文件): {rel}",
                        path=rel,
                    )
                )
                continue

            if _is_nonneg_int(item.get("size")):
                actual_size = real.stat().st_size
                if actual_size != item["size"]:
                    problems.append(
                        VerifyProblem(
                            P_SIZE_MISMATCH,
                            f"大小不符: 清单 {item['size']} 字节, "
                            f"实际 {actual_size} 字节",
                            path=rel,
                        )
                    )

            if is_sha256_hex(item.get("sha256")):
                actual_digest, _ = sha256_file(real)
                if actual_digest != item["sha256"]:
                    problems.append(
                        VerifyProblem(
                            P_DIGEST_MISMATCH,
                            f"SHA-256 不符: 清单 {item['sha256'][:16]}…, "
                            f"实际 {actual_digest[:16]}…",
                            path=rel,
                        )
                    )

    # 5) 多余文件 (签名时不存在、验证时混进来的文件)
    try:
        actual_files = set(iter_artifact_files(artifact_root))
    except (UnsafePathError, UnsupportedFileTypeError, OSError) as exc:
        problems.append(VerifyProblem(P_UNSUPPORTED_FILE, str(exc)))
        actual_files = set()
    extras = sorted(actual_files - listed)
    if strict_extra and extras:
        for rel in extras:
            problems.append(
                VerifyProblem(
                    P_EXTRA_FILE,
                    "文件存在于制品目录但不在已签名清单中",
                    path=rel,
                )
            )

    # 6) entrypoint 必须在清单内 (且随上面的文件检查被验证过存在性/摘要)
    if isinstance(manifest, dict) and isinstance(manifest.get("entrypoint"), str):
        ep = manifest["entrypoint"]
        try:
            validate_relative(ep)
            ep_real = resolve_within(artifact_root, ep)
            if ep not in listed:
                problems.append(
                    VerifyProblem(
                        P_ENTRYPOINT,
                        "entrypoint 不在已签名 files 清单中",
                        path=ep,
                    )
                )
            elif require_executable_entrypoint and not (
                ep_real.is_file() and os.access(ep_real, os.X_OK)
            ):
                problems.append(
                    VerifyProblem(
                        P_ENTRYPOINT_EXEC,
                        "entrypoint 存在但不可执行 (缺少 x 权限)",
                        path=ep,
                    )
                )
        except UnsafePathError as exc:
            problems.append(VerifyProblem(P_ENTRYPOINT, str(exc), path=ep))

    return VerifyReport(
        ok=not problems,
        problems=problems,
        **report_meta,
    )


# 问题代码 -> 异常类型 (供 verify_or_raise / runner 使用)
_PROBLEM_EXCEPTIONS = {
    P_ENVELOPE_MALFORMED: ManifestError,
    P_SCHEMA: ManifestError,
    P_UNKNOWN_ALGORITHM: UnsupportedAlgorithmError,
    P_UNKNOWN_KEY: UnknownKeyError,
    P_SIGNATURE: SignatureError,
    P_UNSAFE_PATH: UnsafePathError,
    P_FILE_MISSING: ManifestError,
    P_SIZE_MISMATCH: ManifestError,
    P_DIGEST_MISMATCH: ManifestError,
    P_EXTRA_FILE: ManifestError,
    P_ENTRYPOINT: ManifestError,
    P_ENTRYPOINT_EXEC: ManifestError,
    P_UNSUPPORTED_FILE: UnsupportedFileTypeError,
}


def verify_or_raise(**kwargs) -> VerifyReport:
    """:func:`verify` 的失败即抛异常版本; runner 必须使用本函数。"""
    report = verify(**kwargs)
    if not report.ok:
        first = report.problems[0]
        exc_cls = _PROBLEM_EXCEPTIONS.get(first.code, SAMError)
        raise exc_cls(f"[{first.code}] {first.message}" + (f" ({first.path})" if first.path else ""))
    return report
