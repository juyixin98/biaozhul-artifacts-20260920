"""CLI 端到端测试：以子进程方式运行真实命令。"""

import json
import os
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[1]


class CliFixture(unittest.TestCase):
    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        self.base = Path(self._tmp.name)
        self.artifact = self.base / "artifact"
        self.artifact.mkdir()
        (self.artifact / "app").mkdir()
        script = self.artifact / "app" / "hello.sh"
        script.write_text("#!/bin/sh\necho \"cli-run:$1\"\n", encoding="utf-8")
        script.chmod(0o755)
        (self.artifact / "payload.bin").write_bytes(os.urandom(256))

        self.priv_path = self.base / "testkey.pem"
        self.trust_path = self.base / "trust.json"
        self.manifest_path = self.base / "manifest.json"

    def tearDown(self) -> None:
        self._tmp.cleanup()

    def _run(self, *args: str, expect_ok: bool = True) -> subprocess.CompletedProcess:
        proc = subprocess.run(
            [sys.executable, "-m", "sig_manifest", *args],
            cwd=REPO_ROOT,
            capture_output=True,
            text=True,
            timeout=120,
        )
        if expect_ok:
            self.assertEqual(
                proc.returncode, 0,
                msg=f"命令失败:\nSTDOUT={proc.stdout}\nSTDERR={proc.stderr}",
            )
        return proc

    def test_01_keygen_sign_verify_run_flow(self) -> None:
        # 1) 生成密钥（无口令，便于自动化）
        self._run(
            "keygen",
            "--private-key", str(self.priv_path),
            "--trust-store", str(self.trust_path),
            "--no-password",
        )
        self.assertTrue(self.priv_path.exists())
        self.assertEqual(os.stat(self.priv_path).st_mode & 0o777, 0o600)
        trust = json.loads(self.trust_path.read_text())
        self.assertEqual(len(trust["keys"]), 1)

        # 2) 签名
        self._run(
            "sign",
            "--artifact-root", str(self.artifact),
            "--name", "cli-demo",
            "--private-key", str(self.priv_path),
            "--no-password",
            "--entrypoint", "app/hello.sh", "from-manifest",
            "--output", str(self.manifest_path),
        )
        env = json.loads(self.manifest_path.read_text())
        self.assertEqual(len(env["signed"]["files"]), 2)
        self.assertEqual(env["signed"]["entrypoint"], ["app/hello.sh", "from-manifest"])

        # 3) 验证通过
        proc = self._run(
            "verify",
            "--artifact-root", str(self.artifact),
            "--manifest", str(self.manifest_path),
            "--trust-store", str(self.trust_path),
        )
        self.assertIn("验证通过", proc.stdout)

        # 4) run dry-run
        proc = self._run(
            "run",
            "--artifact-root", str(self.artifact),
            "--manifest", str(self.manifest_path),
            "--trust-store", str(self.trust_path),
            "--no-password",
            "--dry-run",
        )
        self.assertIn("DRY RUN", proc.stdout)

        # 5) 实际执行
        proc = self._run(
            "run",
            "--artifact-root", str(self.artifact),
            "--manifest", str(self.manifest_path),
            "--trust-store", str(self.trust_path),
            "--no-password",
            "extra-arg",
        )
        self.assertIn("cli-run:from-manifest", proc.stdout)

    def test_verify_fails_on_tampered_file_nonzero_exit(self) -> None:
        self._run("keygen", "--private-key", str(self.priv_path),
                  "--trust-store", str(self.trust_path), "--no-password")
        self._run("sign", "--artifact-root", str(self.artifact), "--name", "x",
                  "--private-key", str(self.priv_path), "--no-password",
                  "--output", str(self.manifest_path))
        (self.artifact / "payload.bin").write_bytes(b"tampered")
        proc = self._run(
            "verify", "--artifact-root", str(self.artifact),
            "--manifest", str(self.manifest_path),
            "--trust-store", str(self.trust_path),
            expect_ok=False,
        )
        self.assertNotEqual(proc.returncode, 0)
        self.assertIn("digest_mismatch", proc.stdout)

    def test_verify_fails_on_unknown_trust_store(self) -> None:
        self._run("keygen", "--private-key", str(self.priv_path),
                  "--trust-store", str(self.trust_path), "--no-password")
        self._run("sign", "--artifact-root", str(self.artifact), "--name", "x",
                  "--private-key", str(self.priv_path), "--no-password",
                  "--output", str(self.manifest_path))
        other_trust = self.base / "other-trust.json"
        self._run("keygen", "--private-key", str(self.base / "other.pem"),
                  "--trust-store", str(other_trust), "--no-password")
        proc = self._run(
            "verify", "--artifact-root", str(self.artifact),
            "--manifest", str(self.manifest_path),
            "--trust-store", str(other_trust),
            expect_ok=False,
        )
        self.assertIn("unknown_key", proc.stdout)

    def test_run_refuses_when_verify_fails(self) -> None:
        self._run("keygen", "--private-key", str(self.priv_path),
                  "--trust-store", str(self.trust_path), "--no-password")
        self._run("sign", "--artifact-root", str(self.artifact), "--name", "x",
                  "--private-key", str(self.priv_path), "--no-password",
                  "--entrypoint-sh", "app/hello.sh",
                  "--output", str(self.manifest_path))
        (self.artifact / "payload.bin").write_bytes(b"x")
        marker = self.base / "should-not-exist"
        proc = self._run(
            "run", "--artifact-root", str(self.artifact),
            "--manifest", str(self.manifest_path),
            "--trust-store", str(self.trust_path),
            "--no-password",
            expect_ok=False,
        )
        self.assertNotEqual(proc.returncode, 0)
        self.assertIn("digest_mismatch", proc.stderr)
        self.assertNotIn("cli-run", proc.stdout)
        self.assertFalse(marker.exists())

    def test_sign_rejects_path_traversal_outside_root(self) -> None:
        # 制品本身包含逃逸符号链接时，签名必须失败。
        outside = self.base / "outside.txt"
        outside.write_text("secret", encoding="utf-8")
        os.symlink(outside, self.artifact / "link-out")
        self._run("keygen", "--private-key", str(self.priv_path),
                  "--trust-store", str(self.trust_path), "--no-password")
        proc = self._run(
            "sign", "--artifact-root", str(self.artifact), "--name", "x",
            "--private-key", str(self.priv_path), "--no-password",
            "--output", str(self.manifest_path),
            expect_ok=False,
        )
        self.assertNotEqual(proc.returncode, 0)
        self.assertIn("unsafe_path", proc.stderr)


if __name__ == "__main__":
    unittest.main()
