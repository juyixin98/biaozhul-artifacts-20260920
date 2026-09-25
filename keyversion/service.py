"""密钥服务层：把状态机权限与加解密绑定。

权限规则（验收核心）：
- encrypt：只用 active 版本。不允许指定 generated/retired/destroyed 版本加密。
- decrypt：按密文信封里的版本号找历史密钥；active/retired 可解密，
  destroyed 明确拒绝（材料已删除，不可恢复），未知版本拒绝。
- 被拒绝的请求同样写审计日志（result=denied），但只记原因，绝不记明文。
"""

from __future__ import annotations

import base64
import dataclasses
import uuid
from typing import Any

from . import crypto
from .errors import (
    DecryptVersionDestroyed,
    DecryptVersionNotUsable,
    EncryptVersionNotActive,
    InvalidEnvelope,
    NoActiveVersion,
    VersionNotFound,
)
from .store import (
    ACTIVE,
    DESTROYED,
    RETIRED,
    AuditEntry,
    KeyStore,
)


@dataclasses.dataclass(frozen=True)
class VersionInfo:
    """对外暴露的版本信息（不含密钥材料）。"""

    version_id: str
    seq: int
    state: str
    active: bool
    created_ts: str
    fingerprint_sha256: str
    has_material: bool

    def to_dict(self) -> dict[str, Any]:
        return dataclasses.asdict(self)


class KeyService:
    """线程安全的密钥版本服务，封装一个 :class:`KeyStore`。"""

    def __init__(self, store: KeyStore):
        self.store = store

    # ------------------------------------------------------------------
    # 生命周期
    # ------------------------------------------------------------------
    def generate(self, request_id: str | None = None) -> VersionInfo:
        vid, _entry = self.store.generate(request_id=request_id)
        return self.get_version(vid)

    def rotate(self, request_id: str | None = None) -> VersionInfo:
        vid, _entry = self.store.rotate(request_id=request_id)
        return self.get_version(vid)

    def activate(self, version_id: str, request_id: str | None = None) -> VersionInfo:
        self.store.activate(version_id, request_id=request_id)
        return self.get_version(version_id)

    def deactivate(self, version_id: str, request_id: str | None = None) -> VersionInfo:
        self.store.deactivate(version_id, request_id=request_id)
        return self.get_version(version_id)

    def destroy(self, version_id: str, request_id: str | None = None) -> VersionInfo:
        self.store.destroy(version_id, request_id=request_id)
        return self.get_version(version_id)

    def list_versions(self) -> list[VersionInfo]:
        return [VersionInfo(**rec) for rec in self.store.list_versions()]

    def get_version(self, version_id: str) -> VersionInfo:
        for rec in self.store.list_versions():
            if rec["version_id"] == version_id:
                return VersionInfo(**rec)
        raise VersionNotFound(f"version {version_id!r} not found")

    def active_version(self) -> VersionInfo | None:
        aid = self.store.active_version_id()
        return self.get_version(aid) if aid else None

    def list_audit(self) -> list[AuditEntry]:
        return self.store.list_audit()

    # ------------------------------------------------------------------
    # 加解密
    # ------------------------------------------------------------------
    @staticmethod
    def _new_request_id() -> str:
        return uuid.uuid4().hex

    def _audit_denied(
        self, action: str, version_id: str, reason: str, request_id: str, extra: dict | None = None
    ) -> None:
        """记录被拒绝的加解密尝试。detail 只放长度/原因，绝不放明文。"""
        with self.store.locked():
            # 直接用存储层的锁保护内部追加；状态未知时用 None
            state = None
            try:
                state = self.store.version_state(version_id) if version_id else None
            except VersionNotFound:
                state = "unknown"
            self.store._append_audit_locked(  # noqa: SLF001 - 同包受控调用
                action=action,
                version_id=version_id or "-",
                old_state=state,
                new_state=state,
                result="denied",
                detail={"reason": reason, **(extra or {})},
                request_id=request_id,
            )

    def encrypt(
        self,
        plaintext: bytes,
        version_id: str | None = None,
        request_id: str | None = None,
    ) -> tuple[bytes, VersionInfo]:
        """加密。默认使用 active 版本；显式指定非 active 版本一律拒绝。

        返回 (二进制信封, 实际使用的版本信息)。
        """
        if not isinstance(plaintext, (bytes, bytearray)):
            raise TypeError("plaintext must be bytes")
        rid = request_id or self._new_request_id()
        with self.store.locked():
            active_id = self.store.active_version_id()
            target = version_id or active_id
            if target is None:
                self._audit_denied(
                    "encrypt", "-", "no_active_version", rid,
                    extra={"plaintext_len": len(plaintext)},
                )
                raise NoActiveVersion("no active key version exists; generate/rotate first")
            if version_id is not None and version_id != active_id:
                state = self.store.version_state(version_id) if self._exists(version_id) else "unknown"
                self._audit_denied(
                    "encrypt", version_id, f"version_not_active(state={state})", rid,
                    extra={"plaintext_len": len(plaintext), "active_version": active_id},
                )
                raise EncryptVersionNotActive(
                    f"version {version_id!r} is {state}; only the active version may encrypt"
                )
            dek = self.store.load_dek(target)  # active 必然有材料
            envelope = crypto.encrypt(dek, target, bytes(plaintext))
            self.store._append_audit_locked(  # noqa: SLF001
                action="encrypt",
                version_id=target,
                old_state=ACTIVE,
                new_state=ACTIVE,
                result="success",
                detail={"plaintext_len": len(plaintext), "ciphertext_len": len(envelope)},
                request_id=rid,
            )
            return envelope, self.get_version(target)

    def decrypt(self, envelope: bytes, request_id: str | None = None) -> tuple[bytes, str]:
        """解密信封：版本号取自信封本身，按历史版本权限放行/拒绝。

        返回 (明文, 版本号)。destroyed / 未知版本拒绝；认证失败拒绝。
        """
        rid = request_id or self._new_request_id()
        if not isinstance(envelope, (bytes, bytearray)):
            raise TypeError("envelope must be bytes")
        # 先解析版本号（不访问密钥材料）
        try:
            version_id = crypto.parse_envelope(bytes(envelope))
        except InvalidEnvelope:
            with self.store.locked():
                self.store._append_audit_locked(  # noqa: SLF001
                    action="decrypt",
                    version_id="-",
                    old_state=None,
                    new_state=None,
                    result="denied",
                    detail={"reason": "invalid_envelope", "ciphertext_len": len(envelope)},
                    request_id=rid,
                )
            raise

        with self.store.locked():
            try:
                state = self.store.version_state(version_id)
            except VersionNotFound:
                self.store._append_audit_locked(  # noqa: SLF001
                    action="decrypt",
                    version_id=version_id,
                    old_state="unknown",
                    new_state="unknown",
                    result="denied",
                    detail={"reason": "version_not_found", "ciphertext_len": len(envelope)},
                    request_id=rid,
                )
                raise
            if state == DESTROYED:
                self.store._append_audit_locked(  # noqa: SLF001
                    action="decrypt",
                    version_id=version_id,
                    old_state=DESTROYED,
                    new_state=DESTROYED,
                    result="denied",
                    detail={"reason": "version_destroyed", "ciphertext_len": len(envelope)},
                    request_id=rid,
                )
                raise DecryptVersionDestroyed(
                    f"version {version_id!r} is destroyed; key material purged, unrecoverable"
                )
            if state not in (ACTIVE, RETIRED):
                # generated（生成后从未激活）不属于“历史加密版本”
                self.store._append_audit_locked(  # noqa: SLF001
                    action="decrypt",
                    version_id=version_id,
                    old_state=state,
                    new_state=state,
                    result="denied",
                    detail={"reason": f"version_state_not_decryptable({state})"},
                    request_id=rid,
                )
                raise DecryptVersionNotUsable(
                    f"version {version_id!r} in state {state} cannot decrypt"
                )
            dek = self.store.load_dek(version_id)
            try:
                plaintext = crypto.decrypt(dek, bytes(envelope))
            except InvalidEnvelope:
                self.store._append_audit_locked(  # noqa: SLF001
                    action="decrypt",
                    version_id=version_id,
                    old_state=state,
                    new_state=state,
                    result="denied",
                    detail={"reason": "authentication_failed", "ciphertext_len": len(envelope)},
                    request_id=rid,
                )
                raise
            self.store._append_audit_locked(  # noqa: SLF001
                action="decrypt",
                version_id=version_id,
                old_state=state,
                new_state=state,
                result="success",
                detail={"plaintext_len": len(plaintext), "ciphertext_len": len(envelope)},
                request_id=rid,
            )
            return plaintext, version_id

    def _exists(self, version_id: str) -> bool:
        try:
            self.store.version_state(version_id)
            return True
        except VersionNotFound:
            return False

    # ------------------------------------------------------------------
    # 便捷的 base64 编解码（HTTP 层使用）
    # ------------------------------------------------------------------
    @staticmethod
    def b64e(data: bytes) -> str:
        return base64.b64encode(data).decode("ascii")

    @staticmethod
    def b64d(text: str) -> bytes:
        try:
            return base64.b64decode(text.encode("ascii"), validate=True)
        except Exception as exc:
            raise InvalidEnvelope("payload is not valid base64") from exc
