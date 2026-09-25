"""导出服务：把策略引擎、策略存储与签名/加密串成可核验的导出包。

导出包（明文，JSON）结构::

    {
      "schema_version": "mde-bundle/v1",
      "manifest": { ... 决策依据与摘要, 签名 ... },
      "policy_snapshot": { ... 本次任务实际使用的不可变策略版本全文 ... },
      "output": <泛化后的记录>,
      "decisions": [ <逐字段决策记录> ]
    }

加密包则把上述明文整体经 Fernet 加密，外面只留不含敏感内容的头部。

核验（verify）分三层，全部通过才算 OK：
1. 清单签名有效（Ed25519，且验签公钥与清单中记录的公钥一致）；
2. output / decisions 的 SHA-256 与清单一致（防篡改）；
3. 若提供原始数据，则用包内策略快照、用途、盐**重跑引擎**，
   比对 output 与决策记录是否逐字节一致（可复算性）。
"""

from __future__ import annotations

import json
import os
import tempfile
import uuid
from typing import Any

from .canonical import stable_json_dumps
from .crypto import (
    SIGNING_ALG,
    decrypt_blob,
    encrypt_blob,
    generate_signing_key,
    load_public_key_bytes,
    new_hash_salt,
    public_key_hex,
    sign_object,
    verify_object,
)
from .engine import apply_policy, decisions_hash, value_hash
from .errors import MdeError, VerificationError
from .models import Policy, now_iso
from .policy import PolicyStore

BUNDLE_SCHEMA = "mde-bundle/v1"
ENCRYPTED_SCHEMA = "mde-encrypted-bundle/v1"

# 每个导出包都必须携带的声明：本系统只做字段级披露控制，不是匿名化。
DISCLAIMER = (
    "Field-level disclosure control only. This output is NOT anonymized and "
    "no anonymization guarantee is made. Generalized/pseudonymized values may "
    "be identifying when combined with other data."
)

# 参与签名的清单字段（signature 本身除外）。
_MANIFEST_FIELDS = (
    "schema_version", "export_id", "created_at", "purpose", "disclaimer",
    "policy", "input_sha256", "output_sha256", "decisions_sha256",
    "hash_salt", "stats", "signing_alg", "verifying_key_hex",
)


class ExportService:
    """无状态编排器（除持有的策略存储与签名密钥外）。"""

    def __init__(self, store: PolicyStore,
                 signing_private_key: Any | None = None,
                 bundle_dir: str | None = None) -> None:
        self.store = store
        self._priv = signing_private_key or generate_signing_key()
        self._key_is_ephemeral = signing_private_key is None
        if self._key_is_ephemeral:
            # 内存中临时密钥：服务重启后旧包仍可用包内公钥核验，
            # 但无法通过本地私钥索引找到它——CLI 会落盘测试密钥。
            pass
        self.bundle_dir = bundle_dir
        if bundle_dir:
            os.makedirs(bundle_dir, exist_ok=True)

    @property
    def verifying_key_hex(self) -> str:
        return public_key_hex(self._priv)

    @property
    def key_is_ephemeral(self) -> bool:
        return self._key_is_ephemeral

    # ------------------------------------------------------------------ export

    def export(self, data: Any, policy_id: str, purpose: str, *,
               version: int | None = None,
               encryption_key: bytes | None = None,
               save: bool | None = None) -> dict[str, Any]:
        """执行一次导出。

        :param version: 固定的策略版本；None 表示解析为“当前最新版本”，
                        但解析到的具体版本会写入清单并随包快照保存。
        :param encryption_key: 提供则输出 Fernet 加密包。
        :param save: 是否落盘到 bundle_dir（配置了目录时默认 True）。
        """
        # 1) 解析并固定策略版本（不可变快照）。
        policy = self.store.get(policy_id, version)
        export_id = uuid.uuid4().hex
        created_at = now_iso()
        salt = new_hash_salt()

        # 2) 引擎执行。
        result = apply_policy(data, policy, purpose, salt)

        # 3) 构造并签名清单。
        manifest = {
            "schema_version": BUNDLE_SCHEMA,
            "export_id": export_id,
            "created_at": created_at,
            "purpose": purpose,
            "disclaimer": DISCLAIMER,
            "policy": {
                "policy_id": policy.policy_id,
                "version": policy.version,
                "fingerprint": policy.fingerprint,
            },
            "input_sha256": value_hash(data),
            "output_sha256": value_hash(result.output),
            "decisions_sha256": decisions_hash(result.decisions),
            "hash_salt": result.hash_salt_hex,
            "stats": result.stats,
            "signing_alg": SIGNING_ALG,
            "verifying_key_hex": self.verifying_key_hex,
        }
        manifest["signature"] = sign_object(self._priv,
                                            self._signing_target(manifest))

        bundle = {
            "schema_version": BUNDLE_SCHEMA,
            "manifest": manifest,
            "policy_snapshot": policy.to_dict(),
            "output": result.output,
            "decisions": result.decisions,
        }

        out_bundle: dict[str, Any]
        if encryption_key is not None:
            out_bundle = self._wrap_encrypted(bundle, encryption_key)
        else:
            out_bundle = bundle

        should_save = save if save is not None else bool(self.bundle_dir)
        if should_save:
            self.save_bundle(out_bundle, export_id)
        return out_bundle

    @staticmethod
    def _signing_target(manifest: dict[str, Any]) -> dict[str, Any]:
        return {k: manifest[k] for k in _MANIFEST_FIELDS}

    def _wrap_encrypted(self, bundle: dict[str, Any], key: bytes) -> dict[str, Any]:
        plaintext = stable_json_dumps(bundle).encode("utf-8")
        env = encrypt_blob(plaintext, key)
        m = bundle["manifest"]
        # 外层头部允许在不解密时识别任务与策略版本，但不含记录内容。
        return {
            "schema_version": ENCRYPTED_SCHEMA,
            "envelope": env["envelope"],
            "ciphertext": env["ciphertext"],
            "header": {
                "export_id": m["export_id"],
                "created_at": m["created_at"],
                "purpose": m["purpose"],
                "policy": m["policy"],
                "signing_alg": SIGNING_ALG,
                "verifying_key_hex": m["verifying_key_hex"],
            },
        }

    # ------------------------------------------------------------------ verify

    def verify_bundle(self, bundle: dict[str, Any], *,
                      encryption_key: bytes | None = None,
                      source_data: Any = None) -> dict[str, Any]:
        """核验导出包，返回检查报告；任何一项失败抛 VerificationError。"""
        plain = self._unwrap_if_encrypted(bundle, encryption_key)
        self._check_schema(plain)
        manifest = plain["manifest"]

        checks: list[dict[str, Any]] = []

        def add(name: str, ok: bool, detail: str) -> None:
            checks.append({"check": name, "ok": ok, "detail": detail})
            if not ok:
                raise VerificationError(
                    f"verification failed at {name}: {detail}")

        # 1) 签名。
        pub = load_public_key_bytes_from_hex(manifest["verifying_key_hex"])
        sig = manifest["signature"]
        try:
            verify_object(pub, self._signing_target(manifest), sig)
            add("manifest_signature", True,
                f"{SIGNING_ALG} signature valid")
        except VerificationError as exc:
            add("manifest_signature", False, str(exc))

        # 策略快照指纹须与清单引用一致。
        snap = Policy.from_dict(plain["policy_snapshot"])
        if (snap.policy_id, snap.version, snap.fingerprint) != (
                manifest["policy"]["policy_id"],
                manifest["policy"]["version"],
                manifest["policy"]["fingerprint"]):
            add("policy_snapshot", False,
                "snapshot id/version/fingerprint does not match manifest")
        else:
            add("policy_snapshot", True,
                f"pinned to {snap.policy_id} v{snap.version} "
                f"({snap.fingerprint[:16]}…)")

        # 2) 摘要复算。
        out_hash = value_hash(plain["output"])
        add("output_hash", out_hash == manifest["output_sha256"],
            f"recomputed {out_hash[:16]}… vs manifest "
            f"{manifest['output_sha256'][:16]}…")
        dec_hash = decisions_hash(plain["decisions"])
        add("decisions_hash", dec_hash == manifest["decisions_sha256"],
            f"recomputed {dec_hash[:16]}… vs manifest "
            f"{manifest['decisions_sha256'][:16]}…")

        if source_data is not None:
            # 3) 全量重算比对（确定性引擎 + canonical 编码）。
            salt = bytes.fromhex(manifest["hash_salt"])
            rerun = apply_policy(source_data, snap, manifest["purpose"], salt)
            same_out = value_hash(rerun.output) == manifest["output_sha256"]
            same_dec = decisions_hash(rerun.decisions) == manifest[
                "decisions_sha256"]
            add("recomputed_output", same_out,
                "engine re-run reproduces identical output" if same_out
                else "engine re-run output differs")
            add("recomputed_decisions", same_dec,
                "engine re-run reproduces identical decision records" if same_dec
                else "engine re-run decisions differ")

        # 声明存在性检查（防止声明字段在重打包时被悄悄去掉——它已被签名覆盖，
        # 这里额外给出显式结果）。
        has_disclaimer = "not anonymized" in manifest.get("disclaimer", "").lower()
        add("anonymization_disclaimer_present", has_disclaimer,
            "bundle explicitly states it is not anonymized")

        return {
            "ok": True,
            "export_id": manifest["export_id"],
            "policy": manifest["policy"],
            "purpose": manifest["purpose"],
            "checks": checks,
        }

    def _unwrap_if_encrypted(self, bundle: dict[str, Any],
                             key: bytes | None) -> dict[str, Any]:
        schema = bundle.get("schema_version")
        if schema == BUNDLE_SCHEMA:
            return bundle
        if schema == ENCRYPTED_SCHEMA:
            if key is None:
                raise VerificationError(
                    "bundle is encrypted; encryption_key is required to verify")
            plaintext = decrypt_blob(
                {"envelope": bundle["envelope"],
                 "ciphertext": bundle["ciphertext"]}, key)
            return json.loads(plaintext.decode("utf-8"))
        raise VerificationError(f"unknown bundle schema_version: {schema!r}")

    @staticmethod
    def _check_schema(plain: dict[str, Any]) -> None:
        for k in ("manifest", "policy_snapshot", "output", "decisions"):
            if k not in plain:
                raise VerificationError(f"bundle missing field {k!r}")
        for k in ("signature", "verifying_key_hex", "output_sha256",
                  "decisions_sha256", "policy"):
            if k not in plain["manifest"]:
                raise VerificationError(f"manifest missing field {k!r}")

    # ------------------------------------------------------------------- files

    def save_bundle(self, bundle: dict[str, Any], export_id: str) -> str:
        """原子落盘；返回文件路径。"""
        if not self.bundle_dir:
            raise MdeError("bundle_dir not configured")
        suffix = ".enc.json" if bundle.get("schema_version") == ENCRYPTED_SCHEMA \
            else ".json"
        path = os.path.join(self.bundle_dir, f"export-{export_id}{suffix}")
        data = stable_json_dumps(bundle).encode("utf-8")
        fd, tmp = tempfile.mkstemp(prefix=".export-", suffix=".tmp",
                                   dir=self.bundle_dir)
        try:
            with os.fdopen(fd, "wb") as f:
                f.write(data)
                f.write(b"\n")
            os.replace(tmp, path)
        except BaseException:
            if os.path.exists(tmp):
                os.unlink(tmp)
            raise
        return path


def load_public_key_bytes_from_hex(pub_hex: str):
    import binascii
    from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PublicKey
    try:
        raw = bytes.fromhex(pub_hex)
    except (ValueError, binascii.Error):
        raise VerificationError("verifying_key_hex is not valid hex") from None
    if len(raw) != 32:
        raise VerificationError("verifying key is not 32 bytes (Ed25519)")
    return Ed25519PublicKey.from_public_bytes(raw)


def load_bundle_file(path: str) -> dict[str, Any]:
    with open(path, "r", encoding="utf-8") as f:
        return json.load(f)


def load_data_file(path: str) -> Any:
    with open(path, "r", encoding="utf-8") as f:
        return json.load(f)
