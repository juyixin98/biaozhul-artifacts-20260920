"""Ed25519 测试密钥与 STH 签名测试。"""

from __future__ import annotations

import tempfile
import unittest
from pathlib import Path

from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey

from tlog import keys


class KeyAndSignatureTests(unittest.TestCase):
    def setUp(self):
        self.key = keys.generate_test_keypair()
        self.pub = self.key.public_key()

    def test_keypair_roundtrip_pem(self):
        with tempfile.TemporaryDirectory() as d:
            priv_path = str(Path(d) / "priv.pem")
            pub_path = str(Path(d) / "pub.pem")
            keys.save_private_key(self.key, priv_path)
            keys.save_public_key(self.pub, pub_path)
            loaded_priv = keys.load_private_key(priv_path)
            loaded_pub = keys.load_public_key(pub_path)
            self.assertEqual(
                keys.public_key_raw(loaded_pub), keys.public_key_raw(self.pub)
            )
            # 加载回来的私钥签名、原公钥能验通
            sig = keys.sign_sth(loaded_priv, 3, 1000, b"\x0a" * 32)
            self.assertTrue(
                keys.verify_sth_signature(self.pub, 3, 1000, b"\x0a" * 32, sig)
            )

    def test_raw_public_key_is_32_bytes(self):
        self.assertEqual(len(keys.public_key_raw(self.pub)), 32)

    def test_sth_signature_verifies(self):
        root = b"\x11" * 32
        sig = keys.sign_sth(self.key, 7, 123456, root)
        self.assertEqual(len(sig), 64)  # Ed25519 签名长度
        self.assertTrue(
            keys.verify_sth_signature(self.pub, 7, 123456, root, sig)
        )

    def test_sth_signature_rejects_tampering(self):
        root = b"\x22" * 32
        sig = keys.sign_sth(self.key, 7, 123456, root)
        other_key = keys.generate_test_keypair()
        # 任一字段被改 / 换公钥都必须验签失败
        self.assertFalse(
            keys.verify_sth_signature(self.pub, 8, 123456, root, sig)
        )
        self.assertFalse(
            keys.verify_sth_signature(self.pub, 7, 123457, root, sig)
        )
        self.assertFalse(
            keys.verify_sth_signature(
                self.pub, 7, 123456, bytes([0x33] * 32), sig
            )
        )
        self.assertFalse(
            keys.verify_sth_signature(other_key.public_key(), 7, 123456, root, sig)
        )
        # 签名翻转一个比特
        bad_sig = sig[:10] + bytes([sig[10] ^ 1]) + sig[11:]
        self.assertFalse(
            keys.verify_sth_signature(self.pub, 7, 123456, root, bad_sig)
        )

    def test_encoded_sth_is_deterministic_and_domain_separated(self):
        root = b"\x44" * 32
        a = keys.encode_sth(1, 2, root)
        b = keys.encode_sth(1, 2, root)
        self.assertEqual(a, b)
        self.assertTrue(a.startswith(keys.STH_SIGNING_LABEL))
        # 不同树大小产生不同待签字节
        self.assertNotEqual(keys.encode_sth(1, 2, root), keys.encode_sth(2, 2, root))

    def test_wrong_key_type_files_rejected(self):
        from cryptography.hazmat.primitives.asymmetric import rsa
        from cryptography.hazmat.primitives import serialization

        rsa_key = rsa.generate_private_key(public_exponent=65537, key_size=2048)
        with tempfile.TemporaryDirectory() as d:
            p = Path(d) / "rsa.pem"
            p.write_bytes(
                rsa_key.private_bytes(
                    serialization.Encoding.PEM,
                    serialization.PrivateFormat.PKCS8,
                    serialization.NoEncryption(),
                )
            )
            with self.assertRaises(TypeError):
                keys.load_private_key(str(p))


if __name__ == "__main__":
    unittest.main(verbosity=2)
