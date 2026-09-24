"""信任库：根引导、制品验证/登记、阈值根轮换与防回退。

角色分离（TUF 风格）：
- root 角色：持有「信任根轮换批准权」，不签制品；
- artifact 角色：持有「制品签名权」，不批准轮换。

所有状态保存在内存，可选通过 TrustStore.load/save 落盘（JSON，仅测试用，
不含任何私钥）。
"""
from __future__ import annotations

import base64
import binascii
import hashlib
import json
import os
from dataclasses import dataclass
from pathlib import Path

from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PublicKey

from . import canonical, keys
from .errors import ServiceError
from .models import (
    ArtifactEnvelope,
    RegisterResponse,
    RootBody,
    RootView,
    SignatureBlock,
    SignatureBlockResult,
    SignerView,
    VerifyResponse,
)


def _b64e(raw: bytes) -> str:
    return base64.b64encode(raw).decode("ascii")


def _b64d(value: str, what: str) -> bytes:
    try:
        return base64.b64decode(value, validate=True)
    except (binascii.Error, ValueError) as exc:
        raise ServiceError("invalid_encoding", f"{what} 不是合法 base64") from exc


def _sha256(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


@dataclass(frozen=True)
class Root:
    version: int
    root_threshold: int
    artifact_threshold: int
    # key_id -> 公钥
    root_signers: dict[str, Ed25519PublicKey]
    artifact_signers: dict[str, Ed25519PublicKey]

    def descriptor_bytes(self) -> bytes:
        return canonical.root_message(
            self.version,
            self.root_threshold,
            [keys.public_raw(k) for k in self.root_signers.values()],
            self.artifact_threshold,
            [keys.public_raw(k) for k in self.artifact_signers.values()],
        )

    def to_view(self) -> RootView:
        return RootView(
            version=self.version,
            root_threshold=self.root_threshold,
            artifact_threshold=self.artifact_threshold,
            root_signers=[
                SignerView(key_id=kid, public_key_pem=keys.public_pem(k))
                for kid, k in sorted(self.root_signers.items())
            ],
            artifact_signers=[
                SignerView(key_id=kid, public_key_pem=keys.public_pem(k))
                for kid, k in sorted(self.artifact_signers.items())
            ],
        )


def build_root(body: RootBody) -> Root:
    def load_role(pem_list: list[str], role: str) -> dict[str, Ed25519PublicKey]:
        out: dict[str, Ed25519PublicKey] = {}
        for i, pem in enumerate(pem_list):
            try:
                pub = keys.load_public_pem(pem)
            except Exception as exc:
                raise ServiceError(
                    "invalid_public_key", f"{role}[{i}] 不是合法 Ed25519 PEM 公钥"
                ) from exc
            kid = keys.key_id(pub)
            if kid in out:
                raise ServiceError(
                    "duplicate_signer", f"{role} 中存在重复公钥（key_id={kid[:12]}…）"
                )
            out[kid] = pub
        return out

    root_signers = load_role(body.root_signers, "root_signers")
    artifact_signers = load_role(body.artifact_signers, "artifact_signers")
    overlap = sorted(set(root_signers) & set(artifact_signers))
    if overlap:
        raise ServiceError(
            "role_separation_violation",
            "根角色与制品角色的公钥不得重叠（最小权限/角色分离）",
        )
    if body.root_threshold > len(root_signers):
        raise ServiceError(
            "threshold_too_large",
            f"root_threshold={body.root_threshold} 超过根签名者数量 {len(root_signers)}",
        )
    if body.artifact_threshold > len(artifact_signers):
        raise ServiceError(
            "threshold_too_large",
            f"artifact_threshold={body.artifact_threshold} 超过制品签名者数量 "
            f"{len(artifact_signers)}",
        )
    return Root(
        version=body.version,
        root_threshold=body.root_threshold,
        artifact_threshold=body.artifact_threshold,
        root_signers=root_signers,
        artifact_signers=artifact_signers,
    )


@dataclass
class _Registration:
    artifact_type: str
    version: str
    root_version: int
    signer_key_ids: list[str]


class TrustStore:
    def __init__(self, persist_path: str | os.PathLike[str] | None = None):
        self._root: Root | None = None
        # (digest, artifact_type, version) -> _Registration
        self._registry: dict[tuple[str, str, str], _Registration] = {}
        self._persist_path = Path(persist_path) if persist_path else None

    # ---------- 根 ----------

    @property
    def root(self) -> Root | None:
        return self._root

    def bootstrap(self, body: RootBody) -> RootView:
        if self._root is not None:
            raise ServiceError(
                "already_bootstrapped",
                f"信任根已存在（版本 {self._root.version}），不能再次引导；请走轮换接口",
                status_code=409,
            )
        if body.version != 1:
            raise ServiceError(
                "invalid_root_version", "初始信任根版本必须为 1"
            )
        self._root = build_root(body)
        self._persist()
        return self._root.to_view()

    def rotate(self, body: RootBody, approvals: list[SignatureBlock]) -> RootView:
        if self._root is None:
            raise ServiceError(
                "not_bootstrapped", "尚未引导初始信任根，请先调用 /roots/bootstrap"
            )
        old = self._root

        # 1) 拒绝版本回退/跳跃：新版本必须恰好 +1
        expected = old.version + 1
        if body.version <= old.version:
            raise ServiceError(
                "version_rollback_rejected",
                f"拒绝版本回退：当前根版本 {old.version}，请求版本 {body.version}",
                status_code=403,
            )
        if body.version != expected:
            raise ServiceError(
                "invalid_root_version",
                f"新版本必须恰好为 {expected}（不允许跳号），收到 {body.version}",
            )

        # 2) 先构造新根（校验阈值/公钥/角色分离）
        new_root = build_root(body)

        # 3) 用「旧根」的 root 角色公钥校验批准签名
        message = new_root.descriptor_bytes()
        seen: set[str] = set()
        approved_by: list[str] = []
        for i, block in enumerate(approvals):
            try:
                pub = keys.load_public_pem(block.public_key)
            except Exception:
                continue  # 无法解析的公钥直接忽略，不计入
            kid = keys.key_id(pub)
            if kid in seen:
                # 同一签名者多签只能算一票（防重复签名凑阈值）
                continue
            seen.add(kid)
            if kid not in old.root_signers:
                continue  # 旧根未授权：制品角色或新根密钥均无批准权
            sig = _b64d(block.signature, f"approvals[{i}].signature")
            nonce = _b64d(block.nonce, f"approvals[{i}].nonce")
            if not nonce:
                continue
            # 批准签名绑定新根描述符 + 一次性 nonce
            if keys.verify(
                pub, sig, canonical.root_approval_message(message, nonce)
            ):
                approved_by.append(kid)

        if len(approved_by) < old.root_threshold:
            raise ServiceError(
                "rotation_unauthorized",
                f"轮换批准不足：旧根阈值 {old.root_threshold}，有效批准 {len(approved_by)}",
                status_code=403,
            )

        # 4) 生效：旧根即刻失效（所有制品必须以新根的制品角色重新验证/登记）
        self._root = new_root
        self._persist()
        return new_root.to_view()

    # ---------- 制品 ----------

    def _check_blocks(
        self, envelope: ArtifactEnvelope
    ) -> tuple[list[SignatureBlockResult], set[str]]:
        assert self._root is not None
        results: list[SignatureBlockResult] = []
        valid_signers: set[str] = set()

        # 先按公钥指纹对签名块去重（同一块/同一签名者多签只计一次）
        seen_pub: dict[str, int] = {}
        digest = bytes.fromhex(envelope.digest)
        for i, block in enumerate(envelope.signatures):
            try:
                pub = keys.load_public_pem(block.public_key)
            except Exception:
                results.append(
                    SignatureBlockResult(index=i, ok=False, reason="invalid_public_key")
                )
                continue
            kid = keys.key_id(pub)
            if kid in seen_pub:
                results.append(
                    SignatureBlockResult(
                        index=i,
                        key_id=kid,
                        ok=False,
                        reason="duplicate_signer_block",
                    )
                )
                continue
            seen_pub[kid] = i

            sig = _b64d(block.signature, f"signatures[{i}].signature")
            nonce = _b64d(block.nonce, f"signatures[{i}].nonce")
            if not nonce:
                results.append(
                    SignatureBlockResult(
                        index=i, key_id=kid, ok=False, reason="empty_nonce"
                    )
                )
                continue
            if len(sig) != 64:
                results.append(
                    SignatureBlockResult(
                        index=i, key_id=kid, ok=False, reason="invalid_signature_length"
                    )
                )
                continue

            message = canonical.artifact_message(
                digest, envelope.artifact_type, envelope.version, nonce
            )
            if not keys.verify(pub, sig, message):
                results.append(
                    SignatureBlockResult(
                        index=i, key_id=kid, ok=False, reason="signature_mismatch"
                    )
                )
                continue
            if kid not in self._root.artifact_signers:
                results.append(
                    SignatureBlockResult(
                        index=i,
                        key_id=kid,
                        ok=False,
                        reason="signer_not_authorized",
                    )
                )
                continue
            valid_signers.add(kid)
            results.append(SignatureBlockResult(index=i, key_id=kid, ok=True))

        return results, valid_signers

    def verify(
        self, envelope: ArtifactEnvelope, content_base64: str | None
    ) -> VerifyResponse:
        if self._root is None:
            return VerifyResponse(
                accepted=False,
                reason="not_bootstrapped",
                threshold=0,
                blocks=[],
            )

        # 若提供原文：重算摘要，正文篡改会在这里被抓出
        if content_base64 is not None:
            content = _b64d(content_base64, "content_base64")
            actual = _sha256(content)
            if actual != envelope.digest:
                return VerifyResponse(
                    accepted=False,
                    root_version=self._root.version,
                    reason="digest_mismatch_content_tampered",
                    threshold=self._root.artifact_threshold,
                    blocks=[],
                )

        results, valid_signers = self._check_blocks(envelope)
        accepted = len(valid_signers) >= self._root.artifact_threshold
        reason = None if accepted else "threshold_not_met_or_invalid_signature"
        return VerifyResponse(
            accepted=accepted,
            root_version=self._root.version,
            reason=reason,
            valid_signatures=len(valid_signers),
            threshold=self._root.artifact_threshold,
            blocks=results,
        )

    def register(
        self, envelope: ArtifactEnvelope, content_base64: str | None
    ) -> RegisterResponse:
        if self._root is None:
            raise ServiceError("not_bootstrapped", "尚未引导初始信任根")

        if content_base64 is not None:
            content = _b64d(content_base64, "content_base64")
            if _sha256(content) != envelope.digest:
                raise ServiceError(
                    "digest_mismatch_content_tampered",
                    "正文重算摘要与 envelope.digest 不一致，拒绝登记",
                    status_code=403,
                )

        results, valid_signers = self._check_blocks(envelope)
        if len(valid_signers) < self._root.artifact_threshold:
            raise ServiceError(
                "signature_threshold_not_met",
                f"有效制品签名 {len(valid_signers)} 个，未达阈值 "
                f"{self._root.artifact_threshold}",
                status_code=403,
            )

        key = (envelope.digest, envelope.artifact_type, envelope.version)

        # 同一制品只能登记一次：这也覆盖了「同一签名者对同一制品重复签名」
        if key in self._registry:
            raise ServiceError(
                "artifact_already_registered",
                "该（digest, artifact_type, version）制品已登记，拒绝重复登记",
                status_code=409,
            )

        signer_ids = sorted(valid_signers)
        self._registry[key] = _Registration(
            artifact_type=envelope.artifact_type,
            version=envelope.version,
            root_version=self._root.version,
            signer_key_ids=signer_ids,
        )

        self._persist()
        return RegisterResponse(
            digest=envelope.digest,
            artifact_type=envelope.artifact_type,
            version=envelope.version,
            root_version=self._root.version,
            signer_key_ids=signer_ids,
        )

    def get_registration(
        self, digest: str, artifact_type: str, version: str
    ) -> _Registration | None:
        return self._registry.get((digest.lower(), artifact_type, version))

    # ---------- 落盘（仅状态，绝不包含私钥） ----------

    def _persist(self) -> None:
        if self._persist_path is None:
            return
        state = {
            "root": None
            if self._root is None
            else {
                "version": self._root.version,
                "root_threshold": self._root.root_threshold,
                "artifact_threshold": self._root.artifact_threshold,
                "root_signers": {
                    kid: keys.public_pem(pub)
                    for kid, pub in self._root.root_signers.items()
                },
                "artifact_signers": {
                    kid: keys.public_pem(pub)
                    for kid, pub in self._root.artifact_signers.items()
                },
            },
            "registry": [
                {
                    "digest": k[0],
                    "artifact_type": k[1],
                    "version": k[2],
                    "root_version": v.root_version,
                    "signer_key_ids": v.signer_key_ids,
                }
                for k, v in self._registry.items()
            ],
        }
        self._persist_path.parent.mkdir(parents=True, exist_ok=True)
        tmp = self._persist_path.with_suffix(".tmp")
        tmp.write_text(json.dumps(state, indent=2, ensure_ascii=False), encoding="utf-8")
        tmp.replace(self._persist_path)

    def load(self) -> "TrustStore":
        if self._persist_path is None or not self._persist_path.exists():
            return self
        state = json.loads(self._persist_path.read_text(encoding="utf-8"))
        r = state.get("root")
        if r is not None:
            self._root = Root(
                version=r["version"],
                root_threshold=r["root_threshold"],
                artifact_threshold=r["artifact_threshold"],
                root_signers={
                    kid: keys.load_public_pem(pem)
                    for kid, pem in r["root_signers"].items()
                },
                artifact_signers={
                    kid: keys.load_public_pem(pem)
                    for kid, pem in r["artifact_signers"].items()
                },
            )
        for item in state.get("registry", []):
            key = (item["digest"], item["artifact_type"], item["version"])
            self._registry[key] = _Registration(
                artifact_type=item["artifact_type"],
                version=item["version"],
                root_version=item["root_version"],
                signer_key_ids=item["signer_key_ids"],
            )
        return self
