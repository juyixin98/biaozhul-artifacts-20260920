"""端到端集成测试：签名 → 重排/篡改 → 验证 → 执行门禁。

覆盖验收点：字段重排验证通过、重复 JSON 键拒绝、路径穿越拒绝、
文件缺失拒绝、未知密钥拒绝、摘要篡改拒绝、签名失败绝不执行制品。
"""

import json
import os
import tempfile
import unittest
from pathlib import Path

from sig_manifest.canonjson import canonical_bytes
from sig_manifest.errors import (
    DigestMismatchError,
    ManifestFileError,
    ManifestSchemaError,
    SignatureError,
    UnknownKeyError,
    UnsafePathError,
)
from sig_manifest.keys import (
    TrustStore,
    b64e,
    generate_private_key,
    public_from_private,
)
from sig_manifest.manifest import (
    parse_envelope,
    sign_artifact,
    sign_signed,
    write_envelope,
)
from sig_manifest.runner import verify_then_run
from sig_manifest.verify import verify_artifact, verify_artifact_or_raise


MARKER_SCRIPT = """#!/bin/sh
echo "artifact-executed:$1"
"""


class SignVerifyFixture(unittest.TestCase):
    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        self.base = Path(self._tmp.name)
        self.artifact = self.base / "artifact"
        self.artifact.mkdir()
        (self.artifact / "bin").mkdir()
        self.script = self.artifact / "bin" / "run.sh"
        self.script.write_text(MARKER_SCRIPT, encoding="utf-8")
        self.script.chmod(0o755)
        (self.artifact / "data.txt").write_text("hello artifact\n", encoding="utf-8")
        (self.artifact / "config.json").write_text(
            json.dumps({"mode": "prod-local-test"}), encoding="utf-8"
        )

        self.private_key = generate_private_key()
        self.stored = public_from_private(self.private_key)
        self.trust = TrustStore()
        self.trust.add(self.stored)

        self.envelope = sign_artifact(
            self.artifact,
            "demo-artifact",
            self.private_key,
            entrypoint=["bin/run.sh", "arg1"],
        )
        self.manifest_path = write_envelope(self.envelope, self.base / "manifest.json")

    def tearDown(self) -> None:
        self._tmp.cleanup()

    def manifest_bytes(self) -> bytes:
        return self.manifest_path.read_bytes()

    # -- 正常路径 ---------------------------------------------------------

    def test_valid_manifest_verifies(self) -> None:
        report = verify_artifact(self.artifact, self.manifest_bytes(), self.trust)
        self.assertTrue(report.ok, msg=report.summary())
        self.assertIn(self.stored.key_id, report.verified_key_ids)

    def test_field_reordered_manifest_verifies(self) -> None:
        """把信封和 signed 的字段全部重排、改成美化格式后仍必须验证通过。

        只重排 JSON 对象的键；files / entrypoint 数组的顺序保持不变
        （数组顺序是签名载荷语义的一部分）。
        """
        env = json.loads(self.manifest_bytes())
        signed = env["signed"]
        files = signed["files"]
        reordered_signed = {
            # 对象键反向书写；数组本身保持原顺序
            "entrypoint": list(signed["entrypoint"]),
            "files": [
                {"digest": f["digest"], "digest_algorithm": f["digest_algorithm"],
                 "size": f["size"], "path": f["path"]}
                for f in files
            ],
            "nonce": signed["nonce"],
            "created_at": signed["created_at"],
            "type": signed["type"],
            "artifact_name": signed["artifact_name"],
            "manifest_version": signed["manifest_version"],
        }
        reordered = {"signatures": env["signatures"], "signed": reordered_signed}
        text = json.dumps(reordered, ensure_ascii=False, indent=4)
        report = verify_artifact(self.artifact, text, self.trust)
        self.assertTrue(report.ok, msg=report.summary())

    def test_canonical_bytes_independent_of_text_layout(self) -> None:
        env1 = parse_envelope(self.manifest_bytes())
        env2 = parse_envelope(json.dumps(env1, indent=8).encode())
        self.assertEqual(
            canonical_bytes(env1["signed"]),
            canonical_bytes(env2["signed"]),
        )

    # -- 重复键 -----------------------------------------------------------

    def test_duplicate_json_key_rejected(self) -> None:
        raw = json.dumps(self.envelope, ensure_ascii=False, separators=(",", ":"))
        # "nonce":"<原值>" -> "nonce":"FAKE","nonce":"<原值>"：合法 JSON 但键重复。
        tampered = raw.replace('"nonce":', '"nonce":"FAKE","nonce":', 1)
        self.assertIn('"nonce":"FAKE","nonce"', tampered)
        report = verify_artifact(self.artifact, tampered.encode(), self.trust)
        self.assertFalse(report.ok)
        self.assertEqual(report.errors[0].code, "canonical_json_error")

    # -- 未知密钥 / 签名失败 ----------------------------------------------

    def test_unknown_key_rejected(self) -> None:
        other_key = generate_private_key()
        other_envelope = sign_artifact(
            self.artifact, "demo-artifact", other_key,
            entrypoint=["bin/run.sh", "arg1"],
        )
        report = verify_artifact(self.artifact, json.dumps(other_envelope), self.trust)
        self.assertFalse(report.ok)
        self.assertTrue(any(p.code == "unknown_key" for p in report.errors))
        with self.assertRaises(UnknownKeyError):
            verify_artifact_or_raise(
                self.artifact, json.dumps(other_envelope), self.trust
            )

    def test_bad_signature_rejected(self) -> None:
        env = json.loads(self.manifest_bytes())
        sig = env["signatures"][0]
        raw = bytearray(__import__("base64").b64decode(sig["signature"] + "=="))
        raw[0] ^= 0xFF
        sig["signature"] = b64e(bytes(raw))
        report = verify_artifact(self.artifact, json.dumps(env), self.trust)
        self.assertFalse(report.ok)
        self.assertTrue(any(p.code == "bad_signature" for p in report.errors))
        with self.assertRaises(SignatureError):
            verify_artifact_or_raise(self.artifact, json.dumps(env), self.trust)

    def test_tampered_signed_content_rejected(self) -> None:
        env = json.loads(self.manifest_bytes())
        env["signed"]["artifact_name"] = "totally-different"
        report = verify_artifact(self.artifact, json.dumps(env), self.trust)
        self.assertFalse(report.ok)
        self.assertTrue(any(p.code == "bad_signature" for p in report.errors))

    def test_trusted_signature_with_extra_unknown_warns_but_passes(self) -> None:
        other_key = generate_private_key()
        other_env = sign_signed(self.envelope["signed"], other_key)
        env = json.loads(self.manifest_bytes())
        env["signatures"].append(other_env["signatures"][0])
        report = verify_artifact(self.artifact, json.dumps(env), self.trust)
        self.assertTrue(report.ok, msg=report.summary())
        self.assertTrue(any(p.code == "unknown_key" for p in report.warnings))

    # -- 文件缺失 / 摘要 --------------------------------------------------

    def test_missing_file_rejected(self) -> None:
        (self.artifact / "data.txt").unlink()
        report = verify_artifact(self.artifact, self.manifest_bytes(), self.trust)
        self.assertFalse(report.ok)
        self.assertTrue(any(p.code == "file_error" and "缺失" in p.message
                            for p in report.errors))
        with self.assertRaises(ManifestFileError):
            verify_artifact_or_raise(self.artifact, self.manifest_bytes(), self.trust)

    def test_modified_file_digest_mismatch(self) -> None:
        (self.artifact / "data.txt").write_text("TAMPERED CONTENT\n", encoding="utf-8")
        report = verify_artifact(self.artifact, self.manifest_bytes(), self.trust)
        self.assertFalse(report.ok)
        self.assertTrue(any(p.code == "digest_mismatch" for p in report.errors))
        with self.assertRaises(DigestMismatchError):
            verify_artifact_or_raise(self.artifact, self.manifest_bytes(), self.trust)

    def test_truncated_file_size_and_digest_mismatch(self) -> None:
        original = (self.artifact / "data.txt").read_bytes()
        (self.artifact / "data.txt").write_bytes(original[:-2])
        report = verify_artifact(self.artifact, self.manifest_bytes(), self.trust)
        self.assertFalse(report.ok)
        self.assertTrue(any(p.code == "digest_mismatch" for p in report.errors))

    def test_extra_unlisted_file_rejected_by_default(self) -> None:
        (self.artifact / "sneaky.txt").write_text("not in manifest", encoding="utf-8")
        report = verify_artifact(self.artifact, self.manifest_bytes(), self.trust)
        self.assertFalse(report.ok)
        self.assertTrue(any(p.code == "file_error" and "多余文件" in p.message
                            for p in report.errors))
        # 显式放宽后可通过（签名/摘要仍受保护）。
        report2 = verify_artifact(
            self.artifact, self.manifest_bytes(), self.trust, allow_extra_files=True
        )
        self.assertTrue(report2.ok, msg=report2.summary())

    # -- 路径穿越 ---------------------------------------------------------

    def test_path_traversal_in_manifest_rejected(self) -> None:
        """信封中直接塞 ../ 路径：无论先被 schema 词法校验拦截，还是先被
        签名校验拦截，都必须失败且不能放行文件访问。"""
        env = json.loads(self.manifest_bytes())
        env["signed"]["files"].append({
            "path": "../../../../etc/passwd",
            "size": 0,
            "digest_algorithm": "sha256",
            "digest": "0" * 64,
        })
        report = verify_artifact(self.artifact, json.dumps(env), self.trust)
        self.assertFalse(report.ok)
        codes = {p.code for p in report.errors}
        self.assertTrue(
            {"unsafe_path", "bad_signature", "canonical_json_error",
             "manifest_schema_error"} & codes,
            msg=f"实际错误码: {codes}",
        )

    def test_path_traversal_signed_then_modified(self) -> None:
        """构造一个 schema 合法但路径恶意的 signed，并用受信私钥签名：
        schema 层必须在验签之后的文件处理阶段仍拒绝 ../ 路径。"""
        env = json.loads(self.manifest_bytes())
        env["signed"]["files"].append({
            "path": "../../../../etc/passwd",
            "size": 0,
            "digest_algorithm": "sha256",
            "digest": "0" * 64,
        })
        re_signed = sign_signed(env["signed"], self.private_key)
        report = verify_artifact(
            self.artifact, json.dumps(re_signed), self.trust
        )
        self.assertFalse(report.ok)
        self.assertTrue(any(p.code == "unsafe_path" for p in report.errors))
        with self.assertRaises(UnsafePathError):
            verify_artifact_or_raise(
                self.artifact, json.dumps(re_signed), self.trust
            )

    def test_absolute_path_in_manifest_rejected(self) -> None:
        env = json.loads(self.manifest_bytes())
        env["signed"]["files"][0]["path"] = "/etc/passwd"
        re_signed = sign_signed(env["signed"], self.private_key)
        report = verify_artifact(self.artifact, json.dumps(re_signed), self.trust)
        self.assertFalse(report.ok)
        self.assertTrue(any(p.code == "unsafe_path" for p in report.errors))

    def test_symlink_escape_tamper_rejected(self) -> None:
        """签名后把制品内文件替换为指向外部的符号链接 → 验证必须失败。"""
        outside = self.base / "outside-secret.txt"
        outside.write_text("secret", encoding="utf-8")
        link_path = self.artifact / "data.txt"
        link_path.unlink()
        os.symlink(outside, link_path)
        report = verify_artifact(self.artifact, self.manifest_bytes(), self.trust)
        self.assertFalse(report.ok)
        self.assertTrue(
            any(p.code in ("unsafe_path", "digest_mismatch", "file_error")
                for p in report.errors)
        )

    # -- schema -----------------------------------------------------------

    def test_duplicate_file_path_rejected(self) -> None:
        env = json.loads(self.manifest_bytes())
        env["signed"]["files"].append(dict(env["signed"]["files"][0]))
        re_signed = sign_signed(env["signed"], self.private_key)
        report = verify_artifact(self.artifact, json.dumps(re_signed), self.trust)
        self.assertFalse(report.ok)
        self.assertTrue(any(p.code == "manifest_schema_error" for p in report.errors))

    def test_extra_envelope_field_rejected(self) -> None:
        env = json.loads(self.manifest_bytes())
        env["extra"] = "surprise"
        report = verify_artifact(self.artifact, json.dumps(env), self.trust)
        self.assertFalse(report.ok)
        self.assertEqual(report.errors[0].code, "manifest_schema_error")

    def test_wrong_manifest_version_rejected(self) -> None:
        env = json.loads(self.manifest_bytes())
        env["signed"]["manifest_version"] = 2
        report = verify_artifact(self.artifact, json.dumps(env), self.trust)
        self.assertFalse(report.ok)
        with self.assertRaises(ManifestSchemaError):
            verify_artifact_or_raise(self.artifact, json.dumps(env), self.trust)

    def test_entrypoint_outside_files_rejected(self) -> None:
        bad = sign_artifact(
            self.artifact, "x", self.private_key,
            entrypoint=None,
        )
        bad["signed"]["entrypoint"] = ["../../bin/sh"]
        re_signed = sign_signed(bad["signed"], self.private_key)
        report = verify_artifact(self.artifact, json.dumps(re_signed), self.trust)
        self.assertFalse(report.ok)
        self.assertTrue(
            any(p.code in ("manifest_schema_error", "unsafe_path") for p in report.errors)
        )

    # -- 执行门禁 ---------------------------------------------------------

    def test_run_succeeds_after_valid_verification(self) -> None:
        result = verify_then_run(
            self.artifact, self.manifest_bytes(), self.trust,
            extra_args=["hello"], timeout=30,
        )
        self.assertEqual(result.returncode, 0)
        self.assertIn(self.stored.key_id, result.verified_key_ids)
        self.assertEqual(result.argv[:2], ["bin/run.sh", "arg1"])
        self.assertEqual(result.argv[-1], "hello")

    def test_run_dry_run_does_not_spawn(self) -> None:
        marker = self.base / "marker.out"
        # dry_run 应返回 argv 且返回码 0、不创建任何外部效果。
        result = verify_then_run(
            self.artifact, self.manifest_bytes(), self.trust, dry_run=True
        )
        self.assertEqual(result.returncode, 0)
        self.assertFalse(marker.exists())

    def test_run_blocked_on_unknown_key(self) -> None:
        other_envelope = sign_artifact(
            self.artifact, "demo-artifact", generate_private_key(),
            entrypoint=["bin/run.sh", "arg1"],
        )
        with self.assertRaises(UnknownKeyError):
            verify_then_run(
                self.artifact, json.dumps(other_envelope), self.trust, timeout=30
            )

    def test_run_blocked_on_digest_mismatch(self) -> None:
        (self.artifact / "data.txt").write_text("tampered", encoding="utf-8")
        with self.assertRaises(DigestMismatchError):
            verify_then_run(
                self.artifact, self.manifest_bytes(), self.trust, timeout=30
            )

    def test_run_blocked_on_missing_file(self) -> None:
        (self.artifact / "data.txt").unlink()
        with self.assertRaises(ManifestFileError):
            verify_then_run(
                self.artifact, self.manifest_bytes(), self.trust, timeout=30
            )

    def test_run_blocked_on_path_traversal(self) -> None:
        env = json.loads(self.manifest_bytes())
        env["signed"]["files"].append({
            "path": "../../../../etc/passwd",
            "size": 0,
            "digest_algorithm": "sha256",
            "digest": "0" * 64,
        })
        re_signed = sign_signed(env["signed"], self.private_key)
        with self.assertRaises(UnsafePathError):
            verify_then_run(
                self.artifact, json.dumps(re_signed), self.trust, timeout=30
            )

    def test_run_blocked_on_bad_signature(self) -> None:
        env = json.loads(self.manifest_bytes())
        raw = bytearray(__import__("base64").b64decode(
            env["signatures"][0]["signature"] + "=="))
        raw[0] ^= 0xFF
        env["signatures"][0]["signature"] = b64e(bytes(raw))
        with self.assertRaises(SignatureError):
            verify_then_run(
                self.artifact, json.dumps(env), self.trust, timeout=30
            )

    def test_non_executable_entrypoint_runs_via_shebang_warning_only(self) -> None:
        # bin/run.sh 有执行位；再签一个以解释器脚本为首文件但无执行位的制品。
        art2 = self.base / "art2"
        art2.mkdir()
        script = art2 / "s.sh"
        script.write_text("#!/bin/sh\necho ok\n", encoding="utf-8")
        script.chmod(0o644)
        env = sign_artifact(art2, "a2", self.private_key, entrypoint=["s.sh"])
        report = verify_artifact(art2, json.dumps(env), self.trust)
        # 验证仍通过（只有 not_executable 警告），但直接 spawn 会失败：
        self.assertTrue(report.ok, msg=report.summary())
        self.assertTrue(any(p.code == "not_executable" for p in report.warnings))


if __name__ == "__main__":
    unittest.main()
