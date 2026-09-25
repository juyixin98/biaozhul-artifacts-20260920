"""End-to-end tests against a real HTTP server (localhost, ephemeral port)."""

from __future__ import annotations

import http.client
import io
import json
import logging
import tempfile
import threading
import time
import unittest

from replay_protection.keys import KeyRegistry
from replay_protection.logging_setup import RedactingFormatter
from replay_protection.nonce_store import NonceStore
from replay_protection.server import build_server
from replay_protection.signing import SIGNING_ALGORITHM, canonical_request, sign_request

SECRET = bytes.fromhex("bb" * 32)
NOW = int(time.time())  # real wall clock for this integration test
WINDOW = 300


def _headers(method, target, body, *, ts=NOW, nonce="z" * 24):
    canonical = canonical_request(
        method, target, body, key_id="test-key-1", timestamp=ts, nonce=nonce
    )
    sig = sign_request(SECRET, canonical)
    return {
        "X-Key-Id": "test-key-1",
        "X-Timestamp": str(ts),
        "X-Nonce": nonce,
        "Authorization": f"{SIGNING_ALGORITHM} Credential=test-key-1, Signature={sig}",
        "Content-Type": "application/json",
    }


class ServerHarness:
    def __init__(self):
        self.tmp = tempfile.TemporaryDirectory()
        registry = KeyRegistry({"test-key-1": SECRET})
        self.nonces = NonceStore(self.tmp.name + "/nonces.db", window_seconds=WINDOW)
        self.httpd = build_server(registry, self.nonces, "127.0.0.1", 0)
        self.port = self.httpd.server_address[1]
        self.thread = threading.Thread(target=self.httpd.serve_forever, daemon=True)

    def __enter__(self):
        self.thread.start()
        return self

    def __exit__(self, *exc):
        self.httpd.shutdown()
        self.httpd.server_close()
        self.nonces.close()
        self.tmp.cleanup()

    def post(self, target, body, headers):
        conn = http.client.HTTPConnection("127.0.0.1", self.port, timeout=5)
        conn.request("POST", target, body=body, headers=headers)
        resp = conn.getresponse()
        data = resp.read()
        conn.close()
        return resp.status, data

    def get(self, target):
        conn = http.client.HTTPConnection("127.0.0.1", self.port, timeout=5)
        conn.request("GET", target)
        resp = conn.getresponse()
        data = resp.read()
        conn.close()
        return resp.status, data


class TestServerEndToEnd(unittest.TestCase):
    def test_healthz_open(self):
        with ServerHarness() as s:
            status, data = s.get("/healthz")
            self.assertEqual(status, 200)
            self.assertEqual(json.loads(data), {"status": "ok"})

    def test_signed_request_accepted(self):
        with ServerHarness() as s:
            body = b'{"op":"ping"}'
            status, data = s.post("/v1/verify", body, _headers("POST", "/v1/verify", body, nonce="a" * 24))
            self.assertEqual(status, 200, data)
            payload = json.loads(data)
            self.assertTrue(payload["accepted"])
            # Response must not echo the full nonce either.
            self.assertTrue(payload["nonce_prefix"].endswith("..."))

    def test_body_tampered_over_the_wire(self):
        with ServerHarness() as s:
            body = b'{"amount":10}'
            h = _headers("POST", "/v1/verify", body, nonce="b" * 24)
            status, data = s.post("/v1/verify", b'{"amount":999}', h)
            self.assertEqual(status, 401)
            self.assertEqual(json.loads(data)["error"], "bad_signature")

    def test_replay_pair(self):
        with ServerHarness() as s:
            body = b"{}"
            h = _headers("POST", "/v1/verify", body, nonce="c" * 24)
            self.assertEqual(s.post("/v1/verify", body, h)[0], 200)
            status, data = s.post("/v1/verify", body, h)
            self.assertEqual(status, 403)
            self.assertEqual(json.loads(data)["error"], "replay_detected")

    def test_concurrent_identical_requests_exactly_one_accepted(self):
        with ServerHarness() as s:
            body = b'{"id":"concurrent-1"}'
            h = _headers("POST", "/v1/verify", body, nonce="d" * 24)
            statuses = []
            barrier = threading.Barrier(40)

            def fire():
                barrier.wait()
                statuses.append(s.post("/v1/verify", body, h)[0])

            threads = [threading.Thread(target=fire) for _ in range(40)]
            for t in threads:
                t.start()
            for t in threads:
                t.join()

            self.assertEqual(sorted(statuses).count(200), 1, statuses)
            self.assertEqual(sorted(statuses).count(403), 39, statuses)

    def test_path_variants_validate_same_signature(self):
        with ServerHarness() as s:
            body = b""
            # Each wire spelling canonicalizes to /v1/verify, so signing that
            # canonical target validates every spelling. Distinct nonces keep
            # the requests from being replays of each other.
            for i, wire in enumerate(["/v1/verify", "/v1/./verify", "/v0/../v1/verify", "/v1/%76erify"]):
                nonce = f"variant-{i:03d}".ljust(24, "x")
                h = _headers("POST", "/v1/verify", body, nonce=nonce)
                status, data = s.post(wire, body, h)
                self.assertEqual(status, 200, f"{wire}: {data!r}")

    def test_stale_timestamp(self):
        with ServerHarness() as s:
            body = b""
            old = NOW - WINDOW - 5
            h = _headers("POST", "/v1/verify", body, ts=old, nonce="f" * 24)
            status, data = s.post("/v1/verify", body, h)
            self.assertEqual(status, 401)
            self.assertEqual(json.loads(data)["error"], "stale_timestamp")

    def test_timestamp_near_boundary_accepted(self):
        with ServerHarness() as s:
            body = b""
            # Exact +/-WINDOW endpoints are covered with an injected clock in
            # test_verifier. Over real HTTP one second can tick mid-request, so
            # stay one second inside the window here.
            ts = int(time.time()) - WINDOW + 1
            h = _headers("POST", "/v1/verify", body, ts=ts, nonce="g" * 24)
            status, data = s.post("/v1/verify", body, h)
            self.assertEqual(status, 200, data)

    def test_missing_signature_headers(self):
        with ServerHarness() as s:
            status, data = s.post("/v1/verify", b"x", {"Content-Length": "1"})
            self.assertEqual(status, 401)
            self.assertEqual(json.loads(data)["error"], "missing_headers")

    def test_unknown_route(self):
        with ServerHarness() as s:
            self.assertEqual(s.post("/nope", b"", {})[0], 404)


class TestLogRedaction(unittest.TestCase):
    def test_secrets_and_signatures_never_logged(self):
        secret_hex = "de" * 32
        signature = "ab" * 32
        stream = io.StringIO()
        handler = logging.StreamHandler(stream)
        fmt = RedactingFormatter("%(message)s", secrets=[secret_hex])
        handler.setFormatter(fmt)
        logger = logging.getLogger("redaction.test")
        logger.handlers[:] = [handler]
        logger.setLevel(logging.DEBUG)
        logger.propagate = False

        logger.info("loaded secret=%s", secret_hex)
        logger.info("Authorization: HMAC-SHA256 Credential=k1, Signature=%s", signature)
        logger.info("signature=%s header", signature)
        logger.info("debugging HMAC-SHA256 Credential=k1, Signature=%s done", signature)

        text = stream.getvalue()
        self.assertNotIn(secret_hex, text)
        self.assertNotIn(signature, text)
        self.assertIn("REDACTED", text)

    def test_formatter_unit_snippets(self):
        fmt = RedactingFormatter("%(message)s", secrets=["TOPSECRETVALUE"])
        self.assertNotIn("TOPSECRETVALUE", fmt.redact("key TOPSECRETVALUE here"))
        self.assertIn(
            "REDACTED",
            fmt.redact("Authorization: HMAC-SHA256 Credential=k, Signature=" + "ab" * 32),
        )


if __name__ == "__main__":
    unittest.main()
