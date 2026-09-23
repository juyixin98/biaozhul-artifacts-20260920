"""Local HTTP service (stdlib only, no third-party web framework).

Endpoints
---------
GET    /health
POST   /v1/rulesets                      {"name": str, "ruleset": {...}}
GET    /v1/rulesets/{name}               summary (no data)
DELETE /v1/rulesets/{name}
POST   /v1/rulesets/{name}/apply         {"document": <any JSON value>}

Safety properties
-----------------
* Request/response bodies are never written to logs (only status codes,
  lengths and rule/path identifiers from error messages).
* All processing happens inside the engine's secret scope, so the log
  redaction filter would catch an original value even if some log call were
  accidentally introduced.
* Bodies are capped at 10 MiB.
"""

import json
import logging
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from typing import Any, Dict, Optional, Tuple
from urllib.parse import urlsplit

from .compiler import CompiledRuleset, compile_ruleset
from .engine import apply_ruleset
from .errors import (
    MaskCompilerError,
    MissingFieldError,
    RuleConflictError,
    RuleSyntaxError,
    TypeMismatchError,
)
from .keys import KeyBundle
from .logsafe import install_safe_logging, secret_scope

MAX_BODY_BYTES = 10 * 1024 * 1024
logger = logging.getLogger("maskcompiler.service")


class StoredRuleset:
    def __init__(self, name: str, compiled: CompiledRuleset) -> None:
        self.name = name
        self.compiled = compiled

    def summary(self) -> dict:
        return {
            "name": self.name,
            "rule_count": len(self.compiled.entries),
            "rules": [
                {"id": e.rule_id, "priority": e.priority}
                for e in sorted(self.compiled.entries, key=lambda e: e.rule_id)
            ],
        }


class RuleStore:
    def __init__(self, key_bundle: KeyBundle) -> None:
        self._key_bundle = key_bundle
        self._rulesets: Dict[str, StoredRuleset] = {}
        self._lock = threading.Lock()

    def create(self, name: str, doc: Any) -> StoredRuleset:
        if not isinstance(name, str) or not name:
            raise RuleSyntaxError("ruleset 'name' must be a non-empty string")
        compiled = compile_ruleset(doc)
        compiled.bind_keys(self._key_bundle)
        stored = StoredRuleset(name, compiled)
        with self._lock:
            if name in self._rulesets:
                raise RuleSyntaxError("ruleset already exists: %s" % name)
            self._rulesets[name] = stored
        return stored

    def get(self, name: str) -> StoredRuleset:
        with self._lock:
            stored = self._rulesets.get(name)
        if stored is None:
            raise RuleSyntaxError("unknown ruleset: %s" % name)
        return stored

    def delete(self, name: str) -> None:
        with self._lock:
            if name not in self._rulesets:
                raise RuleSyntaxError("unknown ruleset: %s" % name)
            del self._rulesets[name]

    def apply(self, name: str, document: Any):
        return apply_ruleset(self.get(name).compiled, document)


_STATUS_FOR_ERROR = {
    RuleSyntaxError: 400,
    RuleConflictError: 409,
    MissingFieldError: 422,
    TypeMismatchError: 422,
    MaskCompilerError: 400,
}


def _status_for(exc: BaseException) -> int:
    for cls, status in _STATUS_FOR_ERROR.items():
        if isinstance(exc, cls):
            return status
    return 500


def make_handler(store: RuleStore) -> type:
    class Handler(BaseHTTPRequestHandler):
        server_version = "maskcompiler/0.1"

        # --- helpers (never log bodies) ---
        def _send_json(self, status: int, payload: dict) -> None:
            body = json.dumps(payload, ensure_ascii=False).encode("utf-8")
            self.send_response(status)
            self.send_header("Content-Type", "application/json; charset=utf-8")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)

        def _send_error(self, status: int, code: str, message: str) -> None:
            # `message` comes from our exception types, which are data-free.
            self._send_json(status, {"error": {"code": code, "message": message}})

        def _read_json(self) -> Tuple[bool, Any]:
            length = int(self.headers.get("Content-Length") or 0)
            if length <= 0:
                self._send_error(400, "empty_body", "request body must be a JSON object")
                return False, None
            if length > MAX_BODY_BYTES:
                self._send_error(413, "body_too_large", "request body exceeds 10 MiB limit")
                return False, None
            raw = self.rfile.read(length)
            try:
                value = json.loads(raw.decode("utf-8"))
            except (UnicodeDecodeError, json.JSONDecodeError):
                self._send_error(400, "invalid_json", "request body is not valid JSON")
                return False, None
            return True, value

        def log_message(self, fmt: str, *args: Any) -> None:
            # Deliberately minimal: no headers, no query strings, no bodies.
            logger.info("%s %s -> handled", self.command, urlsplit(self.path).path)

        # --- routing ---
        def do_GET(self) -> None:  # noqa: N802
            path = urlsplit(self.path).path
            if path == "/health":
                self._send_json(200, {"status": "ok"})
                return
            if path.startswith("/v1/rulesets/"):
                name = path[len("/v1/rulesets/"):]
                if name and "/" not in name:
                    self._guarded(lambda: self._get_ruleset(name))
                    return
            self._send_error(404, "not_found", "unknown endpoint")

        def do_DELETE(self) -> None:  # noqa: N802
            path = urlsplit(self.path).path
            if path.startswith("/v1/rulesets/"):
                name = path[len("/v1/rulesets/"):]
                if name and "/" not in name:
                    self._guarded(lambda: self._delete_ruleset(name))
                    return
            self._send_error(404, "not_found", "unknown endpoint")

        def do_POST(self) -> None:  # noqa: N802
            path = urlsplit(self.path).path
            if path == "/v1/rulesets":
                self._guarded(self._create_ruleset)
                return
            if path.endswith("/apply"):
                prefix = "/v1/rulesets/"
                if path.startswith(prefix):
                    name = path[len(prefix):-len("/apply")]
                    if name and "/" not in name:
                        self._guarded(lambda: self._apply(name))
                        return
            self._send_error(404, "not_found", "unknown endpoint")

        # --- actions ---
        def _guarded(self, action) -> None:
            try:
                with secret_scope():
                    action()
            except MaskCompilerError as exc:
                status = _status_for(exc)
                logger.warning("request failed: %s", type(exc).__name__)
                self._send_error(status, type(exc).__name__, str(exc))
            except Exception:  # noqa: BLE001
                logger.exception("unhandled error")
                self._send_error(500, "internal_error", "internal error")

        def _create_ruleset(self) -> None:
            ok, body = self._read_json()
            if not ok:
                return
            if not isinstance(body, dict) or "ruleset" not in body:
                self._send_error(400, "bad_request", "expected {'name': ..., 'ruleset': {...}}")
                return
            stored = store.create(body.get("name"), body["ruleset"])
            self._send_json(201, {"ruleset": stored.summary()})

        def _get_ruleset(self, name: str) -> None:
            self._send_json(200, {"ruleset": store.get(name).summary()})

        def _delete_ruleset(self, name: str) -> None:
            store.delete(name)
            self._send_json(200, {"deleted": name})

        def _apply(self, name: str) -> None:
            ok, body = self._read_json()
            if not ok:
                return
            if not isinstance(body, dict) or "document" not in body:
                self._send_error(400, "bad_request", "expected {'document': ...}")
                return
            result = store.apply(name, body["document"])
            self._send_json(
                200,
                {"output": result.output, "report": {
                    "transformed_locations": result.report.transformed_locations,
                    "matched_rules": result.report.matched_rules,
                }},
            )

    return Handler


def build_server(host: str, port: int, key_bundle: KeyBundle) -> ThreadingHTTPServer:
    install_safe_logging()
    store = RuleStore(key_bundle)
    return ThreadingHTTPServer((host, port), make_handler(store))


def serve(host: str = "127.0.0.1", port: int = 8080, key_bundle: Optional[KeyBundle] = None) -> None:
    if key_bundle is None:
        from .keys import generate_bundle

        key_bundle = generate_bundle()
        logger.warning("no key file provided; generated ephemeral keys for this process")
    server = build_server(host, port, key_bundle)
    logger.info("maskcompiler listening on http://%s:%d", host, server.server_address[1])
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        pass
    finally:
        server.server_close()
