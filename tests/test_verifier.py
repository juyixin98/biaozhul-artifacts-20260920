import unittest

from replay_protection.keys import KeyRegistry
from replay_protection.nonce_store import NonceStore
from replay_protection.signing import canonical_request, sign_request
from replay_protection.verifier import RequestVerifier, VerificationError

SECRET_HEX = "aa" * 32
NOW = 1_700_000_000
WINDOW = 300


def make_verifier(clock_now=NOW):
    registry = KeyRegistry({"test-key-1": bytes.fromhex(SECRET_HEX)})
    store = NonceStore(":memory:", window_seconds=WINDOW)
    return RequestVerifier(registry, store, WINDOW, clock=lambda: clock_now), store


def signed_headers(method, target, body, *, kid="test-key-1", ts=NOW, nonce="n" * 24,
                   secret=bytes.fromhex(SECRET_HEX), tamper_sig=False):
    canonical = canonical_request(
        method, target, body, key_id=kid, timestamp=ts, nonce=nonce
    )
    sig = sign_request(secret, canonical)
    if tamper_sig:
        sig = ("0" if sig[0] != "0" else "1") + sig[1:]
    return {
        "X-Key-Id": kid,
        "X-Timestamp": str(ts),
        "X-Nonce": nonce,
        "Authorization": f"HMAC-SHA256 Credential={kid}, Signature={sig}",
    }


class TestVerifier(unittest.TestCase):
    def test_accepts_valid_request(self):
        v, store = make_verifier()
        body = b'{"op":"ping"}'
        h = signed_headers("POST", "/v1/verify", body)
        authn = v.verify("POST", "/v1/verify", body, h)
        self.assertEqual(authn.kid, "test-key-1")

    def test_replay_rejected(self):
        v, _ = make_verifier()
        body = b"{}"
        h = signed_headers("POST", "/v1/verify", body)
        v.verify("POST", "/v1/verify", body, h)
        with self.assertRaises(VerificationError) as cm:
            v.verify("POST", "/v1/verify", body, h)
        self.assertEqual(cm.exception.code, "replay_detected")
        self.assertEqual(cm.exception.http_status, 403)

    def test_body_tamper_rejected(self):
        v, _ = make_verifier()
        h = signed_headers("POST", "/v1/verify", b'{"v":1}')
        with self.assertRaises(VerificationError) as cm:
            v.verify("POST", "/v1/verify", b'{"v":2}', h)
        self.assertEqual(cm.exception.code, "bad_signature")

    def test_method_tamper_rejected(self):
        v, _ = make_verifier()
        h = signed_headers("GET", "/v1/verify", b"")
        with self.assertRaises(VerificationError) as cm:
            v.verify("POST", "/v1/verify", b"", h)
        self.assertEqual(cm.exception.code, "bad_signature")

    def test_path_encoding_ambiguity_converges(self):
        v, _ = make_verifier()
        body = b""
        # All wire spellings canonicalize to /b; signing the canonical target
        # validates every spelling, so path-encoding tricks cannot bypass or
        # forge a different resource. Distinct nonces avoid self-replay.
        for i, wire in enumerate(["/b", "/a/../b", "/a/./../b", "/%62", "/a/%2E%2E/b"]):
            nonce = f"path-{i:03d}".ljust(24, "x")
            h = signed_headers("POST", "/b", body, nonce=nonce)
            v.verify("POST", wire, body, h)

        # Encoded slash is a DIFFERENT resource from a real separator.
        h3 = signed_headers("POST", "/a%2Fb", body, nonce="p" * 24)
        v.verify("POST", "/a%2fb", body, h3)  # hex case differs, path same
        with self.assertRaises(VerificationError):
            v.verify("POST", "/a/b", body, h3)

        # Malformed percent-encoding is refused at canonicalization.
        with self.assertRaises(ValueError):
            bad_h = signed_headers("POST", "/a%zz", body, nonce="r" * 24)
            v.verify("POST", "/a%zz", body, bad_h)

    def test_query_canonicalization(self):
        v, _ = make_verifier()
        body = b""
        h = signed_headers("POST", "/x?a=1&b=2", body, nonce="q" * 24)
        v.verify("POST", "/x?b=2&a=1", body, h)  # reordered wire params

    def test_timestamp_boundaries_inclusive(self):
        body = b""
        for delta, ok in [(-WINDOW, True), (WINDOW, True), (-WINDOW - 1, False), (WINDOW + 1, False)]:
            v, _ = make_verifier()
            ts = NOW + delta
            sign = "neg" if delta < 0 else "pos"
            nonce = (sign + str(abs(delta)).zfill(5)).ljust(24, "x")
            h = signed_headers("POST", "/v1/verify", body, ts=ts, nonce=nonce)
            if ok:
                v.verify("POST", "/v1/verify", body, h)
            else:
                with self.assertRaises(VerificationError) as cm:
                    v.verify("POST", "/v1/verify", body, h)
                self.assertEqual(cm.exception.code, "stale_timestamp")

    def test_nonce_reusable_after_expiry(self):
        v, store = make_verifier()
        body = b""
        edge = NOW - WINDOW  # oldest accepted timestamp (boundary included)
        h = signed_headers("POST", "/v1/verify", body, ts=edge, nonce="edge-nonce-0000000000")
        v.verify("POST", "/v1/verify", body, h)
        with self.assertRaises(VerificationError) as cm:
            v.verify("POST", "/v1/verify", body, h)
        self.assertEqual(cm.exception.code, "replay_detected")
        # After the window closes the same nonce can be registered again.
        self.assertTrue(
            store.register("test-key-1", "edge-nonce-0000000000", edge + WINDOW + 1, now=NOW + 1)
        )

    def test_missing_and_malformed_headers(self):
        v, _ = make_verifier()
        body = b""
        valid = signed_headers("POST", "/v1/verify", body, nonce="v" * 24)
        cases = [
            ({}, "missing_headers"),
            (
                {
                    "X-Key-Id": "bad kid!",
                    "X-Timestamp": str(NOW),
                    "X-Nonce": "n" * 24,
                    "Authorization": "HMAC-SHA256 Credential=x, Signature=" + "a" * 64,
                },
                "invalid_key_id",
            ),
            (
                {**valid, "X-Nonce": "short"},
                "invalid_nonce",
            ),
            (
                {**valid, "X-Timestamp": "12.5"},
                "invalid_timestamp",
            ),
            (
                {**valid, "Authorization": "Bearer abc"},
                "invalid_authorization",
            ),
        ]
        for headers, code in cases:
            with self.subTest(code=code):
                with self.assertRaises(VerificationError) as cm:
                    v.verify("POST", "/v1/verify", body, headers)
                self.assertEqual(cm.exception.code, code)

    def test_unknown_key(self):
        v, _ = make_verifier()
        body = b""
        h = signed_headers("POST", "/v1/verify", body, kid="ghost", secret=b"\x11" * 32)
        with self.assertRaises(VerificationError) as cm:
            v.verify("POST", "/v1/verify", body, h)
        self.assertEqual(cm.exception.code, "unknown_key")

    def test_tampered_signature(self):
        v, _ = make_verifier()
        body = b""
        h = signed_headers("POST", "/v1/verify", body, tamper_sig=True)
        with self.assertRaises(VerificationError) as cm:
            v.verify("POST", "/v1/verify", body, h)
        self.assertEqual(cm.exception.code, "bad_signature")

    def test_authorization_kid_must_match(self):
        v, _ = make_verifier()
        body = b""
        h = signed_headers("POST", "/v1/verify", body, nonce="k" * 24)
        h["Authorization"] = h["Authorization"].replace(
            "Credential=test-key-1", "Credential=test-key-2"
        )
        with self.assertRaises(VerificationError) as cm:
            v.verify("POST", "/v1/verify", body, h)
        self.assertEqual(cm.exception.code, "invalid_authorization")


if __name__ == "__main__":
    unittest.main()
