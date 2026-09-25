import bootstrap  # noqa: F401

import threading
import unittest
from concurrent.futures import ThreadPoolExecutor

from anti_replay.nonce_store import MemoryNonceStore
from anti_replay.signing import sign_request
from anti_replay.verifier import (
    ERR_BAD_SIGNATURE,
    ERR_FUTURE,
    ERR_MALFORMED,
    ERR_REPLAY,
    ERR_STALE,
    ERR_UNKNOWN_KEY,
    Verifier,
)

KEY = b"0123456789abcdef0123456789abcdef"
KEY2 = b"fedcba9876543210fedcba9876543210"
KEYS = {"k1": KEY, "k2": KEY2}


def signed_headers(*, key=KEY, key_id="k1", method="POST", target="/api/data",
                   body=b"{}", ts=None, nonce="nonce-abcdef12"):
    import time
    if ts is None:
        ts = int(time.time())
    sig, _, cpath, cquery = sign_request(
        key=key, key_id=key_id, method=method, target=target,
        body=body, timestamp=str(ts), nonce=nonce,
    )
    return {
        "X-Auth-Key-Id": key_id,
        "X-Auth-Timestamp": str(ts),
        "X-Auth-Nonce": nonce,
        "X-Auth-Signature": sig,
    }, cpath, cquery


class VerifierTests(unittest.TestCase):
    def setUp(self):
        self.now = 1_000_000.0
        self.store = MemoryNonceStore(ttl_seconds=600)
        self.verifier = Verifier(
            KEYS, self.store, window_seconds=300, clock=lambda: self.now
        )

    def verify(self, headers, body=b"{}", target="/api/data", method="POST"):
        return self.verifier.verify(
            method=method, target=target, headers=headers, body=body,
        )

    def test_happy_path(self):
        h, _, _ = signed_headers(ts=1_000_000)
        result = self.verify(h)
        self.assertTrue(result.accepted)
        self.assertEqual(result.key_id, "k1")
        self.assertEqual(result.canonical_path, "/api/data")

    def test_replay_second_time_rejected(self):
        h, _, _ = signed_headers(ts=1_000_000, nonce="nonce-replay-01")
        self.assertTrue(self.verify(h).accepted)
        result = self.verify(h)
        self.assertFalse(result.accepted)
        self.assertEqual(result.error_code, ERR_REPLAY)

    def test_concurrent_identical_requests_exactly_one_accepted(self):
        h, _, _ = signed_headers(ts=1_000_000, nonce="nonce-conc-0001")
        n = 64
        barrier = threading.Barrier(n)
        results = []

        def run():
            barrier.wait()
            results.append(self.verify(dict(h)).accepted)

        with ThreadPoolExecutor(max_workers=n) as pool:
            list(pool.map(lambda _: run(), range(n)))
        self.assertEqual(sum(results), 1)
        self.assertEqual(len(results), n)

    # ---- 时间边界 --------------------------------------------------------

    def test_timestamp_boundaries(self):
        for offset, accepted, code in [
            (-300, True, None),    # 恰好旧 300s：边界内
            (300, True, None),     # 恰好新 300s
            (-301, False, ERR_STALE),
            (301, False, ERR_FUTURE),
            (0, True, None),
        ]:
            with self.subTest(offset=offset):
                store = MemoryNonceStore(ttl_seconds=600)
                verifier = Verifier(
                    KEYS, store, window_seconds=300, clock=lambda: self.now
                )
                h, _, _ = signed_headers(
                    ts=int(self.now) + offset, nonce=f"nonce-bound-{offset}"
                )
                result = verifier.verify(
                    method="POST", target="/api/data", headers=h, body=b"{}"
                )
                self.assertEqual(result.accepted, accepted)
                if not accepted:
                    self.assertEqual(result.error_code, code)

    # ---- 签名/密钥 -------------------------------------------------------

    def test_tampered_body(self):
        h, _, _ = signed_headers(ts=1_000_000)
        result = self.verify(h, body=b'{"x":1}')
        self.assertEqual(result.error_code, ERR_BAD_SIGNATURE)

    def test_unknown_key(self):
        h, _, _ = signed_headers(key_id="ghost", ts=1_000_000)
        result = self.verify(h)
        self.assertEqual(result.error_code, ERR_UNKNOWN_KEY)

    def test_signature_with_other_key_fails(self):
        # 用 k2 的密钥签名但声称 k1
        h, _, _ = signed_headers(key=KEY2, key_id="k1", ts=1_000_000)
        self.assertEqual(self.verify(h).error_code, ERR_BAD_SIGNATURE)

    def test_path_encoding_ambiguity_equivalence(self):
        # 对 /./api/%64ata?b=2&a=1 签名，服务端按同一规则规范化并通过
        target = "/./api/%64ata?b=2&a=1"
        h, _, _ = signed_headers(target=target, ts=1_000_000, nonce="nonce-path-001")
        result = self.verifier.verify(
            method="POST", target=target, headers=h, body=b"{}"
        )
        self.assertTrue(result.accepted)
        self.assertEqual(result.canonical_path, "/api/data")

    def test_encoded_separator_rejected_before_signature_check(self):
        # 攻击者可以绕过“合规客户端”直接按原始字节构造签名；服务端仍必须在
        # 规范化阶段拒绝 %2f 分隔符，而不是去比较签名。
        target = "/api%2fdata"
        from anti_replay.canonical import build_canonical_request
        from anti_replay.crypto import hmac_sha256_hex

        # 直接把原始 target 塞进签名串（绕过客户端的规范化拒绝）
        cr = build_canonical_request(
            key_id="k1", method="POST",
            canonical_path_value=target, canonical_query_value="",
            body=b"{}", timestamp="1000000", nonce="nonce-path-002",
        )
        h = {
            "X-Auth-Key-Id": "k1",
            "X-Auth-Timestamp": "1000000",
            "X-Auth-Nonce": "nonce-path-002",
            "X-Auth-Signature": hmac_sha256_hex(KEY, cr.encode()),
        }
        result = self.verifier.verify(
            method="POST", target=target, headers=h, body=b"{}"
        )
        self.assertEqual(result.error_code, ERR_MALFORMED)

    def test_raw_target_differs_from_signed_target_fails_signature(self):
        # 攻击者把签名时的 /api/data 改成 /api/admin，但保留签名
        h, _, _ = signed_headers(target="/api/data", ts=1_000_000)
        result = self.verifier.verify(
            method="POST", target="/api/admin", headers=h, body=b"{}"
        )
        self.assertEqual(result.error_code, ERR_BAD_SIGNATURE)

    # ---- 认证头格式 ------------------------------------------------------

    def test_missing_headers(self):
        h, _, _ = signed_headers(ts=1_000_000)
        del h["X-Auth-Nonce"]
        self.assertEqual(self.verify(h).error_code, ERR_MALFORMED)

    def test_bad_formats(self):
        cases = [
            {"X-Auth-Key-Id": "bad id!", "X-Auth-Timestamp": "1000000",
             "X-Auth-Nonce": "nonce-abcdef12", "X-Auth-Signature": "a" * 64},
            {"X-Auth-Key-Id": "k1", "X-Auth-Timestamp": "1e6",
             "X-Auth-Nonce": "nonce-abcdef12", "X-Auth-Signature": "a" * 64},
            {"X-Auth-Key-Id": "k1", "X-Auth-Timestamp": "1000000",
             "X-Auth-Nonce": "short", "X-Auth-Signature": "a" * 64},
            {"X-Auth-Key-Id": "k1", "X-Auth-Timestamp": "1000000",
             "X-Auth-Nonce": "nonce-abcdef12", "X-Auth-Signature": "Z" * 64},
        ]
        for h in cases:
            with self.subTest(h=h):
                self.assertEqual(self.verify(h).error_code, ERR_MALFORMED)

    def test_header_names_case_insensitive(self):
        h, _, _ = signed_headers(ts=1_000_000)
        lower = {k.lower(): v for k, v in h.items()}
        self.assertTrue(self.verify(lower).accepted)


if __name__ == "__main__":
    unittest.main()
