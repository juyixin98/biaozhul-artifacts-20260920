"""密钥版本管理服务：状态机 + 加解密 + 审计。

密码原语全部来自 cryptography 库：
  * AES-256-GCM（认证加密，密钥 32 字节，nonce 12 字节，由 os.urandom 生成）
  * 密钥由 secrets.token_bytes(32)（CSPRNG）本地生成
密文信封格式（JSON）：
  {"v": <版本号>, "nonce": <b64>, "ct": <b64>}
"""

from __future__ import annotations

import base64
import json
import secrets
import threading
from datetime import datetime, timezone
from pathlib import Path
from typing import Optional

from cryptography.exceptions import InvalidTag
from cryptography.hazmat.primitives.ciphers.aead import AESGCM

from .audit import AuditLog, sha256_hex
from .errors import (
    DecryptError,
    InvalidStateTransitionError,
    KeyDestroyedError,
    KeyNotFoundError,
    NoActiveKeyError,
    WrongVersionError,
)
from .models import (
    ALLOWED_TRANSITIONS,
    DECRYPT_ALLOWED,
    KeyMetadata,
    KeyState,
)
from .store import KeyStore

NONCE_SIZE = 12
KEY_SIZE = 32  # AES-256


def _utcnow() -> str:
    return datetime.now(timezone.utc).isoformat()


def _b64e(b: bytes) -> str:
    return base64.b64encode(b).decode("ascii")


def _b64d(s: str) -> bytes:
    return base64.b64decode(s.encode("ascii"), validate=True)


class KeyService:
    """线程安全的密钥版本管理服务。"""

    def __init__(self, data_dir: str | Path):
        self._lock = threading.RLock()
        self.store = KeyStore(data_dir)
        self.audit = AuditLog(Path(data_dir) / "audit.log")
        problems = self.store.check_consistency()
        if problems:
            self.audit.append("CONSISTENCY_WARNING", problems=problems)
        self.audit.append("SERVICE_START", data_dir=str(data_dir))

    # ---------- 状态机 ----------

    def _transition(self, version_id: str, target: KeyState) -> KeyMetadata:
        meta = self.store.get_metadata(version_id)
        if meta is None:
            raise KeyNotFoundError(f"版本不存在: {version_id}")
        if target not in ALLOWED_TRANSITIONS[meta.state]:
            raise InvalidStateTransitionError(
                f"不允许 {meta.state.value} -> {target.value} (版本 {version_id})"
            )
        return meta

    def generate_key(self) -> KeyMetadata:
        """生成新版本（GENERATED 状态，尚不能加解密）。"""
        with self._lock:
            vid = self.store.allocate_version_id()
            key = secrets.token_bytes(KEY_SIZE)
            self.store.save_key_material(vid, key)
            meta = KeyMetadata(
                version_id=vid,
                state=KeyState.GENERATED,
                created_at=_utcnow(),
                key_fingerprint=sha256_hex(key),
            )
            self.store.put_metadata(meta)
            self.audit.append(
                "KEY_GENERATED", version=vid, key_fingerprint=meta.key_fingerprint
            )
            return meta

    def activate(self, version_id: str) -> KeyMetadata:
        """激活版本；原激活版本自动转为 DEACTIVATED。"""
        with self._lock:
            meta = self._transition(version_id, KeyState.ACTIVE)
            prev_active = self.store.get_active_version()
            if prev_active and prev_active != version_id:
                prev = self.store.get_metadata(prev_active)
                prev.state = KeyState.DEACTIVATED
                prev.deactivated_at = _utcnow()
                self.store.put_metadata(prev)
                self.audit.append(
                    "KEY_DEACTIVATED", version=prev_active, reason="superseded"
                )
            meta.state = KeyState.ACTIVE
            meta.activated_at = _utcnow()
            self.store.put_metadata(meta)
            self.store.set_active_version(version_id)
            self.audit.append("KEY_ACTIVATED", version=version_id)
            return meta

    def deactivate(self, version_id: str) -> KeyMetadata:
        """停用激活版本（历史密文仍可解密，但不可再用于加密）。"""
        with self._lock:
            meta = self._transition(version_id, KeyState.DEACTIVATED)
            meta.state = KeyState.DEACTIVATED
            meta.deactivated_at = _utcnow()
            self.store.put_metadata(meta)
            if self.store.get_active_version() == version_id:
                self.store.set_active_version(None)
            self.audit.append("KEY_DEACTIVATED", version=version_id, reason="manual")
            return meta

    def destroy(self, version_id: str) -> KeyMetadata:
        """销毁版本：擦除密钥材料，密文明确不可恢复。激活版本须先停用。"""
        with self._lock:
            meta = self._transition(version_id, KeyState.DESTROYED)
            self.store.destroy_key_material(version_id)
            meta.state = KeyState.DESTROYED
            meta.destroyed_at = _utcnow()
            self.store.put_metadata(meta)
            self.audit.append("KEY_DESTROYED", version=version_id)
            return meta

    # ---------- 加解密 ----------

    def _load_key(self, version_id: str) -> bytes:
        key = self.store.load_key_material(version_id)
        if key is None:
            raise KeyDestroyedError(f"版本 {version_id} 的密钥材料不可用（已销毁）")
        return key

    def encrypt(self, plaintext: bytes, aad: bytes = b"") -> dict:
        """仅允许使用当前激活版本加密。"""
        with self._lock:
            active = self.store.get_active_version()
            if active is None:
                self.audit.append("ENCRYPT_DENIED", reason="no_active_key")
                raise NoActiveKeyError("没有激活的密钥版本，无法加密")
            key = self._load_key(active)
            nonce = secrets.token_bytes(NONCE_SIZE)
            ct = AESGCM(key).encrypt(nonce, plaintext, aad)
            envelope = {"v": active, "nonce": _b64e(nonce), "ct": _b64e(ct)}
            self.audit.append(
                "ENCRYPT",
                version=active,
                plaintext_sha256=sha256_hex(plaintext),
                ciphertext_sha256=sha256_hex(ct),
            )
            return envelope

    def decrypt(
        self, envelope: dict, aad: bytes = b"", expect_version: Optional[str] = None
    ) -> bytes:
        """按信封中的版本解密。

        权限规则：ACTIVE / DEACTIVATED 可解密；GENERATED 与 DESTROYED 拒绝。
        若指定 expect_version 且与信封版本不一致，拒绝（错误版本拒绝）。
        """
        with self._lock:
            vid = envelope.get("v")
            meta = self.store.get_metadata(vid) if vid else None
            if meta is None:
                self.audit.append("DECRYPT_DENIED", version=vid, reason="unknown_version")
                raise KeyNotFoundError(f"版本不存在: {vid}")
            if expect_version is not None and expect_version != vid:
                self.audit.append(
                    "DECRYPT_DENIED",
                    version=vid,
                    reason="wrong_version",
                    expected=expect_version,
                )
                raise WrongVersionError(
                    f"密文由 {vid} 加密，与指定版本 {expect_version} 不符"
                )
            if meta.state == KeyState.DESTROYED:
                self.audit.append("DECRYPT_DENIED", version=vid, reason="destroyed")
                raise KeyDestroyedError(f"版本 {vid} 已销毁，密文不可恢复")
            if meta.state not in DECRYPT_ALLOWED:
                self.audit.append(
                    "DECRYPT_DENIED", version=vid, reason=f"state_{meta.state.value}"
                )
                raise DecryptError(f"版本 {vid} 状态为 {meta.state.value}，不允许解密")
            try:
                key = self._load_key(vid)
                pt = AESGCM(key).decrypt(
                    _b64d(envelope["nonce"]), _b64d(envelope["ct"]), aad
                )
            except (InvalidTag, ValueError, KeyError) as e:
                self.audit.append("DECRYPT_DENIED", version=vid, reason="invalid_ciphertext")
                raise DecryptError(f"密文校验失败（损坏或被篡改）: {e}") from e
            self.audit.append(
                "DECRYPT", version=vid, plaintext_sha256=sha256_hex(pt)
            )
            return pt

    # ---------- 查询 ----------

    def list_keys(self) -> list[dict]:
        with self._lock:
            metas = self.store.list_metadata()
            return [metas[k].to_dict() for k in sorted(metas, key=lambda s: int(s[1:]))]

    def status(self) -> dict:
        with self._lock:
            return {
                "active_version": self.store.get_active_version(),
                "versions": self.list_keys(),
                "consistency": self.store.check_consistency(),
            }


def envelope_to_json(envelope: dict) -> str:
    return json.dumps(envelope, ensure_ascii=False)


def envelope_from_json(s: str) -> dict:
    return json.loads(s)
