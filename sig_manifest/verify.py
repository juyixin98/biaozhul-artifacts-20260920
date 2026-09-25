"""制品清单验证。

验证顺序（任何一步出问题都判定整体失败，且 :mod:`runner` 只能在
``ok=True`` 时才可能被调用）：

1. 信封严格解析与 schema 校验（含重复 JSON 键检测、路径词法校验）；
2. 签名：规范化 signed → 用信任库中的公钥验签；
   - 没有任何一个签名来自"已知且受信"密钥 → 失败（未知密钥）；
   - 受信密钥存在但其签名值错误 → 失败；
   - 受信签名有效但同时存在未知密钥签名 → 仅告警；
3. 文件：逐个安全解析路径（防逃逸）、必须存在且为普通文件、
   大小与 SHA-256 摘要必须全部匹配；
4. 多余文件：制品目录中不得出现清单未记录的普通文件（可显式放宽）；
5. 入口：entrypoint 目标必须存在；可执行位缺失只告警（解释器脚本不需要）。
"""

from __future__ import annotations

import hashlib
import os
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from cryptography.exceptions import InvalidSignature

from .errors import (
    DigestMismatchError,
    ManifestError,
    ManifestFileError,
    ManifestSchemaError,
    SignatureError,
    UnknownKeyError,
    UnsafePathError,
)
from .keys import TrustStore, b64d
from .manifest import (
    parse_envelope,
    signed_payload_bytes,
    validate_envelope,
)
from .paths import resolve_within, walk_artifact_files


@dataclass
class Problem:
    code: str
    message: str
    path: str | None = None
    key_id: str | None = None


@dataclass
class VerificationReport:
    ok: bool = True
    errors: list[Problem] = field(default_factory=list)
    warnings: list[Problem] = field(default_factory=list)
    # 实际验证通过的受信密钥 ID（至少一个才能 ok）。
    verified_key_ids: list[str] = field(default_factory=list)

    def fail(self, exc: ManifestError, **extra: Any) -> None:
        self.ok = False
        self.errors.append(
            Problem(code=exc.error_code, message=str(exc), **extra)
        )

    def warn(self, code: str, message: str, **extra: Any) -> None:
        self.warnings.append(Problem(code=code, message=message, **extra))

    def summary(self) -> str:
        lines = ["验证通过" if self.ok else "验证失败"]
        for p in self.errors:
            lines.append(f"  [错误][{p.code}] {p.message}")
        for p in self.warnings:
            lines.append(f"  [警告][{p.code}] {p.message}")
        if self.ok and self.verified_key_ids:
            lines.append(f"  受信签名密钥: {', '.join(self.verified_key_ids)}")
        return "\n".join(lines)


def _verify_signatures(
    envelope: dict[str, Any],
    trust_store: TrustStore,
    report: VerificationReport,
) -> None:
    signed = envelope["signed"]
    try:
        payload = signed_payload_bytes(signed)
    except ManifestError as exc:
        report.fail(exc)
        return

    valid_trusted: list[str] = []
    unknown: list[str] = []
    bad: list[tuple[str, str]] = []

    for sig in envelope["signatures"]:
        key_id = sig["key_id"]
        stored = trust_store.get(key_id)
        if stored is None:
            unknown.append(key_id)
            continue
        try:
            stored.public_key.verify(b64d(sig["signature"]), payload)
        except InvalidSignature:
            bad.append((key_id, "签名值与规范化后的 signed 内容不匹配"))
        except Exception as exc:  # noqa: BLE001 - 任何验签异常都视为签名失败
            bad.append((key_id, f"验签过程异常: {exc}"))
        else:
            valid_trusted.append(key_id)

    for key_id, msg in bad:
        report.fail(SignatureError(msg), key_id=key_id)
    for key_id in unknown:
        # 未知密钥仅在"没有任何受信有效签名"时才是致命错误；
        # 若有受信签名，未知签名只作为告警（多签场景）。
        if not valid_trusted:
            report.fail(UnknownKeyError(f"签名密钥不在信任库中: {key_id}"), key_id=key_id)
        else:
            report.warn("unknown_key", f"忽略不在信任库中的额外签名密钥: {key_id}",
                        key_id=key_id)
    if not valid_trusted and not unknown and not bad:
        report.fail(SignatureError("清单没有任何可验证的签名"))
    report.verified_key_ids = sorted(valid_trusted)


def _hash_file(path: Path) -> tuple[int, str]:
    hasher = hashlib.sha256()
    size = 0
    with path.open("rb") as fh:
        while True:
            chunk = fh.read(1024 * 1024)
            if not chunk:
                break
            hasher.update(chunk)
            size += len(chunk)
    return size, hasher.hexdigest()


def _verify_files(
    artifact_root: str | os.PathLike[str],
    envelope: dict[str, Any],
    report: VerificationReport,
) -> None:
    for entry in envelope["signed"]["files"]:
        rel = entry["path"]
        try:
            resolved = resolve_within(artifact_root, rel)
        except UnsafePathError as exc:
            report.fail(exc, path=rel)
            continue
        if not resolved.exists():
            report.fail(ManifestFileError(f"清单记录的文件缺失: {rel}"), path=rel)
            continue
        if not resolved.is_file():
            report.fail(ManifestFileError(f"制品条目不是普通文件: {rel}"), path=rel)
            continue
        try:
            actual_size, actual_digest = _hash_file(resolved)
        except OSError as exc:
            report.fail(ManifestFileError(f"无法读取文件 {rel}: {exc}"), path=rel)
            continue
        if actual_size != entry["size"] or actual_digest != entry["digest"]:
            report.fail(
                DigestMismatchError(
                    f"文件摘要/大小不匹配: {rel} "
                    f"(清单 size={entry['size']}, digest={entry['digest'][:16]}...; "
                    f"实际 size={actual_size}, digest={actual_digest[:16]}...)"
                ),
                path=rel,
            )


def _verify_extras(
    artifact_root: str | os.PathLike[str],
    envelope: dict[str, Any],
    report: VerificationReport,
) -> None:
    listed = {f["path"] for f in envelope["signed"]["files"]}
    try:
        actual = set(walk_artifact_files(artifact_root))
    except UnsafePathError as exc:
        report.fail(exc)
        return
    extras = sorted(actual - listed)
    for rel in extras:
        report.fail(
            ManifestFileError(f"制品目录存在清单未记录的多余文件: {rel}"),
            path=rel,
        )


def _verify_entrypoint(
    artifact_root: str | os.PathLike[str],
    envelope: dict[str, Any],
    report: VerificationReport,
) -> None:
    ep = envelope["signed"].get("entrypoint")
    if not ep:
        return
    target = ep[0]
    try:
        resolved = resolve_within(artifact_root, target)
    except UnsafePathError as exc:
        report.fail(exc, path=target)
        return
    if not resolved.is_file():
        report.fail(ManifestFileError(f"entrypoint 目标不存在或不是普通文件: {target}"),
                    path=target)
        return
    # 解释器脚本（python script.sh 风格）不需要可执行位；仅对直接执行的程序告警。
    if not os.access(resolved, os.X_OK):
        report.warn(
            "not_executable",
            f"entrypoint 目标缺少可执行位（若由解释器启动可忽略）: {target}",
            path=target,
        )


def verify_artifact(
    artifact_root: str | os.PathLike[str],
    manifest_text_or_envelope: str | bytes | dict[str, Any],
    trust_store: TrustStore,
    *,
    allow_extra_files: bool = False,
) -> VerificationReport:
    """完整验证并返回报告（收集所有问题，不做异常上抛）。

    各阶段独立收集错误；签名阶段失败后仍继续检查文件，便于一次性暴露全部问题，
    但 ``ok`` 只要有任一错误即为 False。
    """
    report = VerificationReport()

    if isinstance(manifest_text_or_envelope, dict):
        # 调用者直接传入已解析 dict 时只能做 schema 复验；
        # dict 无法检测重复键（Python 解析时已被合并），验签应优先传原始
        # 字节/文本（HTTP 接口与 CLI 均如此）。
        try:
            envelope = validate_envelope(manifest_text_or_envelope)
        except ManifestError as exc:
            report.fail(exc)
            return report
    else:
        try:
            envelope = parse_envelope(manifest_text_or_envelope)
        except ManifestError as exc:
            report.fail(exc)
            return report

    _verify_signatures(envelope, trust_store, report)
    _verify_files(artifact_root, envelope, report)
    if not allow_extra_files:
        _verify_extras(artifact_root, envelope, report)
    _verify_entrypoint(artifact_root, envelope, report)
    return report


def verify_artifact_or_raise(
    artifact_root: str | os.PathLike[str],
    manifest_text_or_envelope: str | bytes | dict[str, Any],
    trust_store: TrustStore,
    *,
    allow_extra_files: bool = False,
) -> VerificationReport:
    """验证失败时抛出对应的类型化异常（供程序化调用 / runner 门禁使用）。"""
    report = verify_artifact(
        artifact_root,
        manifest_text_or_envelope,
        trust_store,
        allow_extra_files=allow_extra_files,
    )
    if not report.ok:
        # 抛出第一个错误，保留精确异常类型。
        first = report.errors[0]
        mapping = {
            cls.error_code: cls
            for cls in (
                UnsafePathError,
                ManifestSchemaError,
                UnknownKeyError,
                SignatureError,
                DigestMismatchError,
                ManifestFileError,
            )
        }
        exc_cls = mapping.get(first.code, ManifestError)
        raise exc_cls(first.message)
    return report
