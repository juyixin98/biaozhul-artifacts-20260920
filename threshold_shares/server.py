"""Local-only HTTP API around the threshold-share service.

No web framework dependency: stdlib http.server is enough for a local data
processing tool. The server binds 127.0.0.1 by default (use --host to change;
that is a deliberate operator decision, never needed for the tests).

Endpoints (all POST + JSON except /healthz):

  GET  /healthz                  -> {"status": "ok", "version": ...}
  POST /v1/split                 -> create shares
  POST /v1/recover               -> recover from shares (fail-closed)
  POST /v1/seal, /v1/unseal      -> optional local passphrase wrapping demo

Errors are JSON: {"error": {"code": ..., "message": ..., "details": {...}}}
and never include secret material.
"""

import base64
import json
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

from . import __version__
from . import service
from .scheme import PARAM_VERSION
from .sealing import SealingError, seal_share, unseal_share

MAX_BODY_BYTES = 1 * 1024 * 1024  # 1 MiB local request cap


def _parse_secret(payload: dict) -> bytes:
    """Accept {"secret_b64": "..."} or {"secret_text": "..."}; exactly one."""
    has_b64 = "secret_b64" in payload
    has_text = "secret_text" in payload
    if has_b64 == has_text:
        raise service.ParameterError(
            "provide exactly one of 'secret_b64' (base64) or 'secret_text' (UTF-8)"
        )
    if has_b64:
        value = payload["secret_b64"]
        if not isinstance(value, str):
            raise service.ParameterError("'secret_b64' must be a string")
        try:
            return base64.b64decode(value, validate=True)
        except Exception as exc:
            raise service.ParameterError(f"'secret_b64' is not valid base64: {exc}") from exc
    value = payload["secret_text"]
    if not isinstance(value, str):
        raise service.ParameterError("'secret_text' must be a string")
    return value.encode("utf-8")


def handle_split(payload: dict) -> dict:
    secret = _parse_secret(payload)
    try:
        threshold = int(payload["threshold"])
        total = int(payload["total"])
    except (KeyError, TypeError, ValueError) as exc:
        raise service.ParameterError("'threshold' and 'total' must be integers") from exc
    return service.split(secret, threshold, total)


def handle_recover(payload: dict) -> dict:
    shares = payload.get("shares")
    if not isinstance(shares, list):
        raise service.ParameterError("'shares' must be a list of encoded share strings/objects")
    expected_fp = payload.get("expected_fingerprint")
    if expected_fp is not None and not isinstance(expected_fp, str):
        raise service.ParameterError("'expected_fingerprint' must be a string")
    # Accept both encoded strings and JSON-object shares.
    normalized = []
    for item in shares:
        if not isinstance(item, (str, dict)):
            raise service.ParameterError("each share must be a string or object")
        normalized.append(item)
    return service.recover_from_encoded(normalized, expected_fingerprint=expected_fp)


def handle_seal(payload: dict) -> dict:
    share = payload.get("share")
    passphrase = payload.get("passphrase")
    if not isinstance(share, str) or not isinstance(passphrase, str):
        raise service.ParameterError("'share' and 'passphrase' must be strings")
    n = payload.get("scrypt_n", 2**14)
    if not isinstance(n, int) or isinstance(n, bool):
        raise service.ParameterError("'scrypt_n' must be an integer")
    return {"sealed_share": seal_share(share, passphrase, n=n)}


def handle_unseal(payload: dict) -> dict:
    token = payload.get("sealed_share")
    passphrase = payload.get("passphrase")
    if not isinstance(token, str) or not isinstance(passphrase, str):
        raise service.ParameterError("'sealed_share' and 'passphrase' must be strings")
    return {"share": unseal_share(token, passphrase)}


ROUTES = {
    "/v1/split": handle_split,
    "/v1/recover": handle_recover,
    "/v1/seal": handle_seal,
    "/v1/unseal": handle_unseal,
}


def build_handler():
    class Handler(BaseHTTPRequestHandler):
        server_version = f"ThresholdShares/{__version__}"

        def log_message(self, fmt, *args):  # quieter, single-line local logs
            self.server.log.append(f"{self.address_string()} - {fmt % args}")

        def _send_json(self, status: int, body: dict):
            data = json.dumps(body, ensure_ascii=False).encode("utf-8")
            self.send_response(status)
            self.send_header("Content-Type", "application/json; charset=utf-8")
            self.send_header("Content-Length", str(len(data)))
            self.send_header("X-Content-Type-Options", "nosniff")
            self.end_headers()
            self.wfile.write(data)

        def do_GET(self):
            if self.path.split("?")[0] == "/healthz":
                self._send_json(200, {"status": "ok", "version": __version__, "scheme_version": PARAM_VERSION})
            else:
                self._send_json(404, {"error": {"code": "not_found", "message": self.path}})

        def do_POST(self):
            path = self.path.split("?")[0]
            handler = ROUTES.get(path)
            if handler is None:
                self._send_json(404, {"error": {"code": "not_found", "message": path}})
                return
            try:
                length = int(self.headers.get("Content-Length", 0))
            except ValueError:
                self._send_json(400, {"error": {"code": "bad_request", "message": "bad Content-Length"}})
                return
            if length <= 0:
                self._send_json(400, {"error": {"code": "bad_request", "message": "empty request body"}})
                return
            if length > MAX_BODY_BYTES:
                # Drain the body before replying, otherwise the RST from a
                # half-closed socket can surface to the client as a connection
                # error instead of our tidy 413.
                self.rfile.read(length)
                self._send_json(413, {"error": {"code": "body_too_large", "message": f"limit is {MAX_BODY_BYTES} bytes"}})
                return
            raw = self.rfile.read(length)
            try:
                payload = json.loads(raw.decode("utf-8"))
            except (UnicodeDecodeError, json.JSONDecodeError) as exc:
                self._send_json(400, {"error": {"code": "bad_json", "message": f"request body is not valid JSON: {exc}"}})
                return
            if not isinstance(payload, dict):
                self._send_json(400, {"error": {"code": "bad_request", "message": "request body must be a JSON object"}})
                return
            try:
                result = handler(payload)
            except service.ServiceError as exc:
                self._send_json(exc.http_status, {"error": {"code": exc.code, "message": str(exc), "details": exc.details}})
            except SealingError as exc:
                self._send_json(400, {"error": {"code": "sealing_error", "message": str(exc)}})
            except (ValueError, TypeError) as exc:
                self._send_json(400, {"error": {"code": "bad_request", "message": str(exc)}})
            else:
                self._send_json(200, result)

    return Handler


def create_server(host: str = "127.0.0.1", port: int = 8080):
    httpd = ThreadingHTTPServer((host, port), build_handler())
    httpd.log = []
    return httpd
