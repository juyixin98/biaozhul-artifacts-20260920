import bootstrap  # noqa: F401

import unittest

from anti_replay.canonical import build_canonical_request, canonical_request_target
from anti_replay.crypto import sha256_hex
from anti_replay.signing import sign_request, verify_request_signature

KEY = bytes(range(32))
KEY_ID = "k1"


def make_signature(*, target="/api/data", body=b"", ts="1000", nonce="n-once-1234",
                   method="POST", key=KEY):
    sig, canonical, cpath, cquery = sign_request(
        key=key, key_id=KEY_ID, method=method, target=target,
        body=body, timestamp=ts, nonce=nonce,
    )
    return sig, cpath, cquery


class CanonicalRequestFormatTests(unittest.TestCase):
    def test_exact_eight_line_format(self):
        cpath, cquery = canonical_request_target("/api/data?a=1")
        cr = build_canonical_request(
            key_id=KEY_ID, method="post", canonical_path_value=cpath,
            canonical_query_value=cquery, body=b"{}", timestamp="1000",
            nonce="nonce-1234",
        )
        lines = cr.split("\n")
        self.assertEqual(lines, [
            "ANTI-REPLAY-API-HMAC-SHA256-v1",
            "k1",
            "POST",  # 方法被大写
            "/api/data",
            "a=1",
            sha256_hex(b"{}"),
            "1000",
            "nonce-1234",
        ])

    def test_empty_body_uses_sha256_of_empty(self):
        cr = build_canonical_request(
            key_id=KEY_ID, method="GET", canonical_path_value="/x",
            canonical_query_value="", body=b"", timestamp="1", nonce="nonce-1234",
        )
        self.assertIn(
            "e3b0c44298fc1c149afbf4c8996fb92427ae41e4649b934ca495991b7852b855",
            cr,
        )

    def test_empty_query_is_blank_line(self):
        cr = build_canonical_request(
            key_id=KEY_ID, method="GET", canonical_path_value="/x",
            canonical_query_value="", body=b"", timestamp="1", nonce="nonce-1234",
        )
        # 第 5 行（索引 4）为空行
        self.assertEqual(cr.split("\n")[4], "")


class SignatureTests(unittest.TestCase):
    def test_valid_signature_roundtrip(self):
        body = b'{"amount":100}'
        sig, cpath, cquery = make_signature(body=body)
        self.assertTrue(verify_request_signature(
            key=KEY, key_id=KEY_ID, method="POST",
            canonical_path_value=cpath, canonical_query_value=cquery,
            body=body, timestamp="1000", nonce="n-once-1234",
            signature_hex=sig,
        ))

    def test_tampered_body_fails(self):
        body = b'{"amount":100}'
        sig, cpath, cquery = make_signature(body=body)
        self.assertFalse(verify_request_signature(
            key=KEY, key_id=KEY_ID, method="POST",
            canonical_path_value=cpath, canonical_query_value=cquery,
            body=b'{"amount":999}', timestamp="1000", nonce="n-once-1234",
            signature_hex=sig,
        ))

    def test_tampered_method_fails(self):
        body = b""
        sig, cpath, cquery = make_signature(body=body, method="POST")
        self.assertFalse(verify_request_signature(
            key=KEY, key_id=KEY_ID, method="PUT",
            canonical_path_value=cpath, canonical_query_value=cquery,
            body=body, timestamp="1000", nonce="n-once-1234",
            signature_hex=sig,
        ))

    def test_tampered_path_query_timestamp_nonce_fail(self):
        body = b"x"
        sig, _, _ = make_signature(target="/api/data?a=1", body=body)
        cpath, cquery = canonical_request_target("/api/data?a=2")
        self.assertFalse(verify_request_signature(
            key=KEY, key_id=KEY_ID, method="POST",
            canonical_path_value=cpath, canonical_query_value=cquery,
            body=body, timestamp="1000", nonce="n-once-1234",
            signature_hex=sig,
        ))

        cpath, cquery = canonical_request_target("/api/data?a=1")
        for ts, nonce in (("1001", "n-once-1234"), ("1000", "n-once-9999")):
            self.assertFalse(verify_request_signature(
                key=KEY, key_id=KEY_ID, method="POST",
                canonical_path_value=cpath, canonical_query_value=cquery,
                body=body, timestamp=ts, nonce=nonce, signature_hex=sig,
            ))

    def test_wrong_key_fails(self):
        sig, cpath, cquery = make_signature()
        self.assertFalse(verify_request_signature(
            key=bytes(range(1, 33)), key_id=KEY_ID, method="POST",
            canonical_path_value=cpath, canonical_query_value=cquery,
            body=b"", timestamp="1000", nonce="n-once-1234",
            signature_hex=sig,
        ))

    def test_equivalent_targets_share_one_signature_material(self):
        # 不同 raw target 规范化后签名输入相同 -> 相同签名
        def sig_for(target):
            sig, cpath, cquery = make_signature(target=target)
            return verify_request_signature(
                key=KEY, key_id=KEY_ID, method="POST",
                canonical_path_value=cpath, canonical_query_value=cquery,
                body=b"", timestamp="1000", nonce="n-once-1234",
                signature_hex=sig,
            )
        for target in ("/api/data", "/./api/data", "//api//data",
                       "/api/%64ata", "/api/../api/data", "/api/data?b=2"):
            self.assertTrue(sig_for(target), target)

    def test_malformed_signature_hex_returns_false(self):
        _, cpath, cquery = make_signature()
        for bad in ("zz", "abc", "00" * 31, "00" * 33):
            self.assertFalse(verify_request_signature(
                key=KEY, key_id=KEY_ID, method="POST",
                canonical_path_value=cpath, canonical_query_value=cquery,
                body=b"", timestamp="1000", nonce="n-once-1234",
                signature_hex=bad,
            ))


if __name__ == "__main__":
    unittest.main()
