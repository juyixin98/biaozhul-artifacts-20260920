import hashlib
import unittest

from replay_protection.signing import (
    CanonicalizationError,
    body_digest,
    canonical_path,
    canonical_query,
    canonical_request,
    sign_request,
    verify_signature,
)


class TestCanonicalPath(unittest.TestCase):
    def test_basic_normalized(self):
        self.assertEqual(canonical_path("/v1/verify"), "/v1/verify")

    def test_dot_segments_removed(self):
        self.assertEqual(canonical_path("/a/./b"), "/a/b")
        self.assertEqual(canonical_path("/a/c/../b"), "/a/b")
        self.assertEqual(canonical_path("/a/b/.."), "/a")
        self.assertEqual(canonical_path("/a/b/../"), "/a/")

    def test_encoded_slash_is_not_a_separator(self):
        # %2F must stay encoded inside a single segment.
        self.assertEqual(canonical_path("/a%2Fb"), "/a%2Fb")
        self.assertEqual(canonical_path("/a%2fb"), "/a%2Fb")  # case normalized

    def test_escape_case_and_utf8(self):
        self.assertEqual(canonical_path("/a%7eb"), "/a~b")  # ~ is unreserved
        self.assertEqual(canonical_path("/%E2%9C%93"), "/%E2%9C%93")  # check mark

    def test_ambiguous_encodings_converge(self):
        # A raw space and its encoded form canonicalize identically.
        self.assertEqual(canonical_path("/Hello%20World"), canonical_path("/Hello World"))
        # '+' is a sub-delim; literal and %2B converge to the literal form.
        self.assertEqual(canonical_path("/a+b"), canonical_path("/a%2Bb"))
        self.assertEqual(canonical_path("/a+b"), "/a+b")

    def test_malformed_escape_rejected(self):
        for bad in ["/a%2", "/a%zz", "/a%", "/a%2g"]:
            with self.subTest(bad=bad):
                with self.assertRaises(CanonicalizationError):
                    canonical_path(bad)

    def test_control_chars_rejected(self):
        with self.assertRaises(CanonicalizationError):
            canonical_path("/a%00b")
        with self.assertRaises(CanonicalizationError):
            canonical_path("/a\x00b")

    def test_trailing_slash_preserved(self):
        self.assertEqual(canonical_path("/v1/"), "/v1/")


class TestCanonicalQuery(unittest.TestCase):
    def test_sorted_pairs(self):
        self.assertEqual(canonical_query("b=2&a=1"), "a=1&b=2")

    def test_encoding_converges(self):
        self.assertEqual(canonical_query("k=a%20b"), canonical_query("k=a%20b"))
        # + in a query is an encoded space under application/x-www-form-urlencoded,
        # but here '+' is a sub-delim and stays literal on purpose — this is the
        # documented convention; %2B and '+' converge.
        self.assertEqual(canonical_query("k=a%2Bb"), canonical_query("k=a+b"))

    def test_uppercase_escape_normalization(self):
        self.assertEqual(canonical_query("k=%2f"), canonical_query("k=%2F"))

    def test_empty(self):
        self.assertEqual(canonical_query(""), "")

    def test_rejects_empty_pair(self):
        with self.assertRaises(CanonicalizationError):
            canonical_query("a=1&&b=2")


class TestSignAndVerify(unittest.TestCase):
    SECRET = b"0" * 32

    def _canon(self, **over):
        kw = dict(
            method="POST",
            target="/v1/verify",
            body=b'{"op":"ping"}',
            key_id="test-key-1",
            timestamp=1700000000,
            nonce="n" * 24,
        )
        kw.update(over)
        return canonical_request(**kw)

    def test_canonical_field_order(self):
        c = self._canon()
        lines = c.split("\n")
        self.assertEqual(lines[0], "POST")
        self.assertEqual(lines[1], "/v1/verify")
        self.assertEqual(lines[2], "")
        self.assertEqual(lines[3], hashlib.sha256(b'{"op":"ping"}').hexdigest())
        self.assertEqual(lines[4], "test-key-1")
        self.assertEqual(lines[5], "1700000000")
        self.assertEqual(lines[6], "n" * 24)

    def test_roundtrip(self):
        c = self._canon()
        sig = sign_request(self.SECRET, c)
        self.assertTrue(verify_signature(self.SECRET, c, sig))
        self.assertTrue(verify_signature(self.SECRET, c, sig.upper()))

    def test_each_field_breaks_signature(self):
        good = self._canon()
        sig = sign_request(self.SECRET, good)
        for mutated in [
            self._canon(method="GET"),
            self._canon(target="/v1/other"),
            self._canon(target="/v1/verify?x=1"),
            self._canon(body=b'{"op":"pong"}'),
            self._canon(key_id="test-key-2"),
            self._canon(timestamp=1700000001),
            self._canon(nonce="m" * 24),
        ]:
            with self.subTest(m=mutated):
                self.assertFalse(verify_signature(self.SECRET, mutated, sig))

    def test_empty_body_hashes_empty_bytes(self):
        c = self._canon(body=b"")
        self.assertIn(hashlib.sha256(b"").hexdigest(), c)
        self.assertTrue(verify_signature(self.SECRET, c, sign_request(self.SECRET, c)))

    def test_body_digest_helper(self):
        self.assertEqual(body_digest(b""), hashlib.sha256(b"").hexdigest())
        self.assertEqual(body_digest(None), hashlib.sha256(b"").hexdigest())

    def test_bad_signature_inputs_return_false(self):
        c = self._canon()
        self.assertFalse(verify_signature(self.SECRET, c, "zz"))
        self.assertFalse(verify_signature(self.SECRET, c, ""))


if __name__ == "__main__":
    unittest.main()
