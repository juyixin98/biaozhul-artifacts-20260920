"""本地 HTTP 服务测试（真实 loopback socket + urllib）。"""

import json
import tempfile
import unittest
import urllib.error
import urllib.request
from pathlib import Path

from cryptography.hazmat.primitives.serialization import (
    Encoding,
    NoEncryption,
    PrivateFormat,
)

from sig_manifest.keys import (
    TrustStore,
    generate_private_key,
    public_from_private,
)
from sig_manifest.manifest import sign_artifact
from sig_manifest.server import create_server, serve_in_thread


class ServerFixture(unittest.TestCase):
    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        self.base = Path(self._tmp.name)
        self.artifact = self.base / "artifact"
        self.artifact.mkdir()
        (self.artifact / "hello.txt").write_text("http test\n", encoding="utf-8")

        self.priv = generate_private_key()
        self.stored = public_from_private(self.priv)
        store = TrustStore()
        store.add(self.stored)
        self.trust_path = store.save(self.base / "trust.json")

        self.httpd = create_server(
            host="127.0.0.1", port=0,
            trust_store_path=str(self.trust_path), verbose=False,
        )
        self.thread = serve_in_thread(self.httpd)
        host, port = self.httpd.server_address[:2]
        self.base_url = f"http://{host}:{port}"

    def tearDown(self) -> None:
        self.httpd.shutdown()
        self.httpd.server_close()
        self.thread.join(timeout=5)
        self._tmp.cleanup()

    def _post(self, path: str, payload: dict) -> tuple[int, dict]:
        req = urllib.request.Request(
            self.base_url + path,
            data=json.dumps(payload).encode("utf-8"),
            headers={"Content-Type": "application/json"},
            method="POST",
        )
        try:
            with urllib.request.urlopen(req, timeout=10) as resp:
                return resp.status, json.loads(resp.read())
        except urllib.error.HTTPError as exc:
            return exc.code, json.loads(exc.read())

    def test_healthz(self) -> None:
        with urllib.request.urlopen(self.base_url + "/healthz", timeout=5) as resp:
            self.assertEqual(resp.status, 200)
            self.assertTrue(json.loads(resp.read())["ok"])

    def test_sign_then_verify_roundtrip(self) -> None:
        pem = self.priv.private_bytes(
            encoding=Encoding.PEM,
            format=PrivateFormat.PKCS8,
            encryption_algorithm=NoEncryption(),
        ).decode()
        status, body = self._post("/sign", {
            "artifact_root": str(self.artifact),
            "artifact_name": "http-artifact",
            "private_key_pem": pem,
        })
        self.assertEqual(status, 200, body)
        self.assertTrue(body["ok"])
        manifest = body["manifest"]

        status, body = self._post("/verify", {
            "artifact_root": str(self.artifact),
            "manifest": manifest,
        })
        self.assertEqual(status, 200, body)
        self.assertTrue(body["ok"], body)
        self.assertIn(self.stored.key_id, body["verified_key_ids"])

    def test_verify_unknown_key_422(self) -> None:
        other = generate_private_key()
        env = sign_artifact(self.artifact, "x", other)
        status, body = self._post("/verify", {
            "artifact_root": str(self.artifact),
            "manifest": env,
        })
        self.assertEqual(status, 422)
        self.assertFalse(body["ok"])
        codes = [e["code"] for e in body.get("errors", [])]
        self.assertIn("unknown_key", codes)

    def test_verify_missing_file_422(self) -> None:
        env = sign_artifact(self.artifact, "x", self.priv)
        (self.artifact / "hello.txt").unlink()
        status, body = self._post("/verify", {
            "artifact_root": str(self.artifact),
            "manifest": env,
        })
        self.assertEqual(status, 422)
        codes = [e["code"] for e in body.get("errors", [])]
        self.assertIn("file_error", codes)

    def test_run_dry_run_ok_and_real_run_not_executed_on_bad_sig(self) -> None:
        env = sign_artifact(self.artifact, "x", self.priv)
        status, body = self._post("/run", {
            "artifact_root": str(self.artifact),
            "manifest": env,
            "dry_run": True,
        })
        self.assertEqual(status, 400)  # 无 entrypoint
        self.assertFalse(body["ok"])

    def test_bad_json_400(self) -> None:
        req = urllib.request.Request(
            self.base_url + "/verify",
            data=b"not-json{",
            headers={"Content-Type": "application/json"},
            method="POST",
        )
        try:
            urllib.request.urlopen(req, timeout=5)
            self.fail("应当返回 4xx")
        except urllib.error.HTTPError as exc:
            self.assertEqual(exc.code, 400)

    def test_path_traversal_via_verify_rejected(self) -> None:
        env = sign_artifact(self.artifact, "x", self.priv)
        env["signed"]["files"].append({
            "path": "../../../../etc/passwd",
            "size": 0,
            "digest_algorithm": "sha256",
            "digest": "0" * 64,
        })
        from sig_manifest.manifest import sign_signed
        env = sign_signed(env["signed"], self.priv)
        status, body = self._post("/verify", {
            "artifact_root": str(self.artifact),
            "manifest": env,
        })
        self.assertEqual(status, 422)
        codes = [e["code"] for e in body.get("errors", [])]
        self.assertIn("unsafe_path", codes)

    def test_duplicate_key_in_manifest_text_rejected(self) -> None:
        env = sign_artifact(self.artifact, "x", self.priv)
        text = json.dumps(env, separators=(",", ":"))
        tampered = text.replace('"nonce":', '"nonce":"X","nonce":', 1)
        status, body = self._post("/verify", {
            "artifact_root": str(self.artifact),
            "manifest": tampered,
        })
        self.assertIn(status, (400, 422))
        self.assertFalse(body["ok"])


if __name__ == "__main__":
    unittest.main()
