"""Local-only HTTP JSON service for offline certificate chain verification.

Design constraints honoured on purpose:

* Binds to 127.0.0.1 only -- never 0.0.0.0; the service is a local secure
  data-processing utility, not a network server.
* Makes NO outbound connections: certificates/anchors arrive in the request
  body; there is no AIA fetch, no CRL download, no OCSP query.
* Trust anchors, verification moment, purpose and hostname are all explicit
  request fields -- nothing is inferred from a system store or wall clock.
"""

from __future__ import annotations

import json
from datetime import datetime, timezone
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from typing import Any

from .models import Purpose, VerificationOptions
from .pemutils import parse_pem_certificates
from .verify import verify_chain

MAX_BODY_BYTES = 2 * 1024 * 1024  # 2 MiB cap on request bodies

PURPOSE_VALUES = {p.value for p in Purpose}


class RequestError(ValueError):
    def __init__(self, code: str, message: str):
        super().__init__(message)
        self.code = code
        self.message = message


def _parse_time(value: Any, field: str) -> datetime:
    if not isinstance(value, str) or not value.strip():
        raise RequestError("INVALID_TIME", f"{field} must be an ISO 8601 string")
    text = value.strip()
    if text.endswith("Z"):
        text = text[:-1] + "+00:00"
    try:
        parsed = datetime.fromisoformat(text)
    except ValueError as exc:
        raise RequestError("INVALID_TIME", f"{field} is not valid ISO 8601: {exc}") from exc
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=timezone.utc)
    return parsed


def _parse_certs(value: Any, field: str) -> list:
    if not isinstance(value, str) or "-----BEGIN CERTIFICATE-----" not in value:
        raise RequestError(
            "INVALID_CERTIFICATES",
            f"{field} must be PEM text containing CERTIFICATE blocks",
        )
    try:
        return parse_pem_certificates(value)
    except ValueError as exc:
        raise RequestError("INVALID_CERTIFICATES", f"{field}: {exc}") from exc


def handle_verify(payload: dict) -> tuple[int, dict]:
    if not isinstance(payload, dict):
        raise RequestError("INVALID_REQUEST", "request body must be a JSON object")

    leaf_list = _parse_certs(payload.get("leaf_certificate"), "leaf_certificate")
    if len(leaf_list) != 1:
        raise RequestError(
            "INVALID_CERTIFICATES",
            "leaf_certificate must contain exactly one PEM certificate",
        )
    leaf = leaf_list[0]

    intermediates = _parse_certs(payload.get("intermediates", ""), "intermediates") \
        if payload.get("intermediates") else []
    raw_anchors = payload.get("trust_anchors")
    if not raw_anchors:
        raise RequestError(
            "MISSING_TRUST_ANCHORS",
            "at least one explicit trust anchor PEM is required (offline mode "
            "never uses an implicit system store)",
        )
    anchors = _parse_certs(raw_anchors, "trust_anchors")

    verification_time = _parse_time(payload.get("verification_time"), "verification_time")

    purpose_value = payload.get("purpose", Purpose.SERVER_AUTH.value)
    if purpose_value not in PURPOSE_VALUES:
        raise RequestError(
            "INVALID_PURPOSE",
            f"purpose must be one of {sorted(PURPOSE_VALUES)}",
        )
    purpose = Purpose(purpose_value)

    hostname = payload.get("hostname")
    if hostname is not None and (not isinstance(hostname, str) or not hostname.strip()):
        raise RequestError("INVALID_HOSTNAME", "hostname must be a non-empty string")
    if hostname is None and purpose is not Purpose.ANY:
        # Not fatal: many offline checks (expiry/path) are useful without a
        # hostname; the result notes that hostname matching was skipped.
        pass

    opts = payload.get("options", {}) or {}
    if not isinstance(opts, dict):
        raise RequestError("INVALID_OPTIONS", "options must be an object")
    options = VerificationOptions(
        check_anchor_validity=bool(opts.get("check_anchor_validity", False)),
        allow_cn_hostname=bool(opts.get("allow_cn_hostname", False)),
        allow_weak_signature_algorithms=bool(
            opts.get("allow_weak_signature_algorithms", False)
        ),
    )

    result = verify_chain(
        leaf,
        intermediates,
        anchors,
        verification_time=verification_time,
        purpose=purpose,
        hostname=hostname,
        options=options,
    )
    return 200, result.to_dict()


class _Handler(BaseHTTPRequestHandler):
    server_version = "OfflineCertVerifier/1.0"

    def _send_json(self, status: int, body: dict) -> None:
        data = json.dumps(body, indent=2, sort_keys=True).encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(data)))
        self.send_header("Cache-Control", "no-store")
        self.end_headers()
        self.wfile.write(data)

    def do_GET(self):  # noqa: N802
        if self.path in ("/health", "/healthz"):
            self._send_json(200, {
                "status": "ok",
                "service": "offline-cert-verifier",
                "network": "loopback-only; no outbound connections are made",
                "revocation_checking": "disabled-by-design (offline)",
            })
        else:
            self._send_json(404, {
                "error": {
                    "code": "NOT_FOUND",
                    "message": "POST /verify a JSON body, or GET /health",
                }
            })

    def do_POST(self):  # noqa: N802
        if self.path.rstrip("/") != "/verify":
            self._send_json(404, {"error": {"code": "NOT_FOUND", "message": "use POST /verify"}})
            return
        try:
            length = int(self.headers.get("Content-Length", "0"))
        except ValueError:
            self._send_json(400, {"error": {"code": "BAD_REQUEST", "message": "bad Content-Length"}})
            return
        if length <= 0:
            self._send_json(400, {"error": {"code": "EMPTY_BODY", "message": "empty request body"}})
            return
        if length > MAX_BODY_BYTES:
            self._send_json(413, {"error": {"code": "BODY_TOO_LARGE",
                                           "message": f"body exceeds {MAX_BODY_BYTES} bytes"}})
            return
        raw = self.rfile.read(length)
        try:
            payload = json.loads(raw.decode("utf-8"))
        except (UnicodeDecodeError, json.JSONDecodeError) as exc:
            self._send_json(400, {"error": {"code": "INVALID_JSON", "message": str(exc)}})
            return
        try:
            status, body = handle_verify(payload)
        except RequestError as exc:
            self._send_json(400, {"error": {"code": exc.code, "message": exc.message}})
            return
        except Exception as exc:  # never leak a stack trace over the API
            self._send_json(500, {"error": {"code": "INTERNAL_ERROR", "message": str(exc)}})
            return
        self._send_json(status, body)

    def log_message(self, fmt, *args):  # keep stderr concise
        import sys
        sys.stderr.write("[certverifier] " + (fmt % args) + "\n")


def create_server(host: str = "127.0.0.1", port: int = 8080) -> ThreadingHTTPServer:
    # Bind loopback explicitly. Refuse to start on a non-loopback interface.
    if host not in ("127.0.0.1", "localhost", "::1"):
        raise ValueError(
            f"refusing to bind {host!r}: this service is local-only; "
            "use 127.0.0.1 (or ::1)"
        )
    return ThreadingHTTPServer((host, port), _Handler)


def main(argv: list[str] | None = None) -> int:
    import argparse

    parser = argparse.ArgumentParser(description="Offline X.509 chain verification service")
    parser.add_argument("--host", default="127.0.0.1")
    parser.add_argument("--port", type=int, default=8080)
    args = parser.parse_args(argv)

    server = create_server(args.host, args.port)
    print(f"Offline certificate verifier listening on http://{args.host}:{args.port}")
    print("POST /verify (JSON)  |  GET /health  |  Ctrl-C to stop")
    print("No outbound network connections are ever made.")
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        print("\nshutting down")
    finally:
        server.server_close()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
