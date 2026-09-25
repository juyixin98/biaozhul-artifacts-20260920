"""密钥与信任库测试。"""

import json
import os
import tempfile
import unittest
from pathlib import Path

from sig_manifest.errors import KeyStoreError
from sig_manifest.keys import (
    KEY_ID_PREFIX,
    TrustStore,
    generate_private_key,
    key_id_for_public,
    load_private_key_pem,
    load_public_key_from_b64,
    public_from_private,
    save_private_key_pem,
)


class TestKeys(unittest.TestCase):
    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        self.dir = Path(self._tmp.name)

    def tearDown(self) -> None:
        self._tmp.cleanup()

    def test_key_id_stable_and_prefix(self) -> None:
        priv = generate_private_key()
        stored = public_from_private(priv)
        self.assertTrue(stored.key_id.startswith(KEY_ID_PREFIX))
        self.assertEqual(len(stored.key_id), len(KEY_ID_PREFIX) + 64)
        self.assertEqual(stored.key_id, key_id_for_public(stored.public_key))

    def test_encrypted_pem_roundtrip(self) -> None:
        priv = generate_private_key()
        path = save_private_key_pem(priv, self.dir / "k.pem", "s3cret")
        mode = os.stat(path).st_mode & 0o777
        self.assertEqual(mode, 0o600)
        loaded = load_private_key_pem(path, "s3cret")
        self.assertEqual(
            public_from_private(loaded).key_id,
            public_from_private(priv).key_id,
        )

    def test_wrong_password_rejected(self) -> None:
        priv = generate_private_key()
        path = save_private_key_pem(priv, self.dir / "k.pem", "right")
        with self.assertRaises(KeyStoreError):
            load_private_key_pem(path, "wrong")

    def test_unencrypted_pem_empty_password_rejected(self) -> None:
        priv = generate_private_key()
        with self.assertRaises(KeyStoreError):
            save_private_key_pem(priv, self.dir / "k.pem", "")

    def test_trust_store_roundtrip(self) -> None:
        store = TrustStore()
        for _ in range(3):
            store.add(public_from_private(generate_private_key()))
        path = store.save(self.dir / "trust.json")
        loaded = TrustStore.load(path)
        self.assertEqual(loaded.key_ids(), store.key_ids())

    def test_trust_store_rejects_key_id_mismatch(self) -> None:
        stored = public_from_private(generate_private_key())
        other = public_from_private(generate_private_key())
        bad = {
            "version": 1,
            "kind": "sig-manifest-trust-store",
            "keys": [{
                "key_id": other.key_id,  # 用了别人的 ID
                "algorithm": "ed25519",
                "public": stored.to_dict()["public"],
            }],
        }
        with self.assertRaises(KeyStoreError):
            TrustStore.from_dict(bad)

    def test_trust_store_rejects_duplicate_entries(self) -> None:
        stored = public_from_private(generate_private_key())
        d = stored.to_dict()
        bad = {
            "version": 1,
            "kind": "sig-manifest-trust-store",
            "keys": [d, dict(d)],
        }
        with self.assertRaises(KeyStoreError):
            TrustStore.from_dict(bad)

    def test_trust_store_rejects_duplicate_json_keys_on_load(self) -> None:
        path = self.dir / "trust-bad.json"
        path.write_text(
            '{"version":1,"kind":"sig-manifest-trust-store",'
            '"keys":[],"keys":[]}',
            encoding="utf-8",
        )
        with self.assertRaises(KeyStoreError):
            TrustStore.load(path)

    def test_public_b64_bad_length(self) -> None:
        import base64

        short = base64.b64encode(b"not-32-bytes").decode()
        with self.assertRaises(KeyStoreError):
            load_public_key_from_b64(short)

    def test_trust_store_conflicting_same_id(self) -> None:
        a = public_from_private(generate_private_key())
        b = public_from_private(generate_private_key())
        store = TrustStore()
        store.add(a)
        # 手动伪造 b 使用 a 的 ID。
        forged = type(a)(key_id=a.key_id, public_key=b.public_key)
        with self.assertRaises(KeyStoreError):
            store.add(forged)


if __name__ == "__main__":
    unittest.main()
