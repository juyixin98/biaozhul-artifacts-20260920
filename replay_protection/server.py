"""Threaded HTTP server enforcing HMAC request signatures and anti-replay.

Only the Python standard library is used. Protected endpoints are verified by
``RequestVerifier``; the protected resource is a trivial echo used to prove
end-to-end behavior.
"""

from __future__ import annotations

import argparse
import json
import logging
import os
import sys
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

from .keys import KeyRegistry
from .logging_setup import configure_logging
from .nonce_store import NonceStore, start_purge_thread
from .verifier import RequestVerifier, VerificationError

log = logging.getLogger("replay.server")
MAX_BODY = 1 * 1024 * 1024  # 1 MiB cap on signed bodies


class _Handler(BaseHTTPRequestHandler):
    server_version = "AntiReplay/1.0"
    verifier: RequestVerifier = None  # injected by build_server

    # --- helpers -----------------------------------------------------------
    def _send_json(self, status: int, payload: dict) -> None:
        data = json.dumps(payload).encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(data)))
        self.end_headers()
        self.wfile.write(data)

    def _read_body(self) -> bytes:
        length = int(self.headers.get("Content-Length", "0") or "0")
        if length < 0 or length > MAX_BODY:
            raise VerificationError("body_too_large", 413, f"body exceeds {MAX_BODY} bytes")
        return self.rfile.read(length) if length else b""

    def _header_bag(self) -> dict[str, str]:
        return {
            "X-Key-Id": self.headers.get("X-Key-Id", ""),
            "X-Timestamp": self.headers.get("X-Timestamp", ""),
            "X-Nonce": self.headers.get("X-Nonce", ""),
            "Authorization": self.headers.get("Authorization", ""),
        }

    def _reject(self, err: VerificationError) -> None:
        # Log machine codes only; never headers or signatures.
        log.warning("rejected request: %s %s", err.code, err.http_status)
        self._send_json(
            err.http_status,
            {"error": err.code, "message": err.message},
        )

    # --- routing -----------------------------------------------------------
    def do_GET(self) -> None:  # noqa: N802
        if self.path == "/healthz":
            self._send_json(200, {"status": "ok"})
            return
        self._send_json(404, {"error": "not_found"})

    def do_POST(self) -> None:  # noqa: N802
        from .signing import CanonicalizationError, canonical_path

        raw_path = self.path.split("?", 1)[0]
        try:
            route = canonical_path(raw_path)
        except CanonicalizationError:
            self._send_json(400, {"error": "bad_path", "message": "malformed request path"})
            return
        if route != "/v1/verify":
            self._send_json(404, {"error": "not_found"})
            return
        try:
            body = self._read_body()
            authn = self.verifier.verify(
                self.command, self.path, body, self._header_bag()
            )
        except VerificationError as err:
            self._reject(err)
            return
        except (ValueError, json.JSONDecodeError):
            self._send_json(400, {"error": "bad_request", "message": "unreadable request"})
            return

        from .signing import body_digest

        log.info("accepted signed request kid_len=%d", len(authn.kid))
        self._send_json(
            200,
            {
                "accepted": True,
                "kid": authn.kid,
                "nonce_prefix": authn.nonce[:4] + "...",
                "body_sha256": body_digest(body),
                "bytes": len(body),
            },
        )

    # Keep the noise out of logs; never log raw request lines (which contain
    # Authorization on some logging configurations).
    def log_message(self, fmt, *args) -> None:  # noqa: A003
        log.debug("http: " + fmt, *args)


def build_server(
    registry: KeyRegistry,
    nonces: NonceStore,
    host: str = "127.0.0.1",
    port: int = 8080,
) -> ThreadingHTTPServer:
    handler = type("BoundHandler", (_Handler,), {"verifier": RequestVerifier(registry, nonces)})
    httpd = ThreadingHTTPServer((host, port), handler)
    # Allow bursts of concurrent signed requests to complete the handshake
    # instead of being dropped in the listen queue.
    httpd.request_queue_size = 128
    httpd.socket.listen(httpd.request_queue_size)
    start_purge_thread(nonces)
    return httpd


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Anti-replay HMAC verification server")
    parser.add_argument("--host", default=os.environ.get("HOST", "127.0.0.1"))
    parser.add_argument("--port", type=int, default=int(os.environ.get("PORT", "8080")))
    parser.add_argument(
        "--keys", default=os.environ.get("KEY_FILE", os.path.join("data", "keys.json"))
    )
    parser.add_argument(
        "--nonce-db", default=os.environ.get("NONCE_DB", os.path.join("data", "nonces.db"))
    )
    args = parser.parse_args(argv)

    if not os.path.exists(args.keys):
        print(
            f"key file {args.keys} not found; run: python3 -m scripts.gen_keys",
            file=sys.stderr,
        )
        return 2

    registry = KeyRegistry.load(args.keys)
    # Register raw secrets with the log redactor as a last line of defense.
    configure_logging(secrets=[s.hex() for s in (registry.get(k) for k in registry.ids()) if s])
    nonces = NonceStore(args.nonce_db)

    httpd = build_server(registry, nonces, args.host, args.port)
    log.info("listening on http://%s:%d (protected: POST /v1/verify)", args.host, args.port)
    try:
        httpd.serve_forever()
    except KeyboardInterrupt:
        pass
    finally:
        httpd.server_close()
        nonces.close()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
