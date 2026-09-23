"""JSON HTTP service for the regex automata engine (Python standard library).

Endpoints
---------

``GET  /health``
    ``{"status": "ok"}``

``POST /compile``
    Request  ``{"pattern": "..."}``
    Response ``{"ok": true, "pattern": ..., "ast": ..., "nfa": {...}}``

``POST /match``
    Request  ``{"pattern": "...", "text": "...", "mode": "search"|"fullmatch"|"prefix"}``
    Response ``{"ok": true, "pattern": ..., "mode": ..., "matched": bool,
                "match": {"start","end","matched"} | null,
                "nfa_states": int}``

Errors return HTTP 400 with ``{"ok": false, "error": {"type","message",
"line","column"}}``; malformed JSON returns HTTP 400 as well.
"""
from __future__ import annotations

import json
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

from . import __version__
from .compiler import compile_pattern
from .errors import RegexError
from .locations import line_col

MAX_BODY_BYTES = 1_000_000
MODES = ("search", "fullmatch", "prefix")


def _ast_dict(node) -> object:
    from . import ast

    if isinstance(node, ast.Empty):
        return {"kind": "empty", "span": [node.span.start, node.span.end]}
    if isinstance(node, ast.Char):
        return {
            "kind": "char",
            "cp": node.cp,
            "char": chr(node.cp),
            "span": [node.span.start, node.span.end],
        }
    if isinstance(node, ast.Dot):
        return {"kind": "dot", "span": [node.span.start, node.span.end]}
    if isinstance(node, ast.Class):
        return {
            "kind": "class",
            "matches": node.predicate.describe(),
            "span": [node.span.start, node.span.end],
        }
    if isinstance(node, ast.Anchor):
        return {
            "kind": "anchor",
            "anchor": node.kind,
            "span": [node.span.start, node.span.end],
        }
    if isinstance(node, ast.Concat):
        return {
            "kind": "concat",
            "parts": [_ast_dict(p) for p in node.parts],
            "span": [node.span.start, node.span.end],
        }
    if isinstance(node, ast.Alt):
        return {
            "kind": "alt",
            "branches": [_ast_dict(b) for b in node.branches],
            "span": [node.span.start, node.span.end],
        }
    if isinstance(node, ast.Repeat):
        return {
            "kind": "repeat",
            "min": node.minimum,
            "max": node.maximum,
            "child": _ast_dict(node.child),
            "span": [node.span.start, node.span.end],
        }
    if isinstance(node, ast.Group):
        return {
            "kind": "group",
            "child": _ast_dict(node.child),
            "span": [node.span.start, node.span.end],
        }
    raise TypeError(f"unknown node {node!r}")


def handle_compile(payload: dict) -> dict:
    pattern = payload.get("pattern")
    if not isinstance(pattern, str):
        raise ValueError("'pattern' must be a JSON string")
    compiled = compile_pattern(pattern)
    return {
        "ok": True,
        "pattern": pattern,
        "ast": _ast_dict(compiled.tree),
        "nfa": compiled.nfa.to_dict(),
        "assertions": {
            str(s): kinds for s, kinds in sorted(compiled.assertions.items())
        },
    }


def handle_match(payload: dict) -> dict:
    pattern = payload.get("pattern")
    text = payload.get("text", "")
    mode = payload.get("mode", "search")
    if not isinstance(pattern, str):
        raise ValueError("'pattern' must be a JSON string")
    if not isinstance(text, str):
        raise ValueError("'text' must be a JSON string")
    if mode not in MODES:
        raise ValueError(f"'mode' must be one of {list(MODES)}")
    compiled = compile_pattern(pattern)
    simulator = compiled.simulator()
    result = getattr(simulator, mode)(text)
    return {
        "ok": True,
        "pattern": pattern,
        "mode": mode,
        "matched": result is not None,
        "match": result.as_dict() if result is not None else None,
        "nfa_states": len(compiled.nfa.edges),
    }


ROUTES = {
    "/compile": handle_compile,
    "/match": handle_match,
}


class _Handler(BaseHTTPRequestHandler):
    server_version = f"RegexAutomata/{__version__}"

    def log_message(self, fmt, *args):  # quieter default logging
        pass

    def _send(self, status: int, body: dict) -> None:
        data = json.dumps(body, ensure_ascii=False).encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(data)))
        self.end_headers()
        self.wfile.write(data)

    def do_GET(self):  # noqa: N802
        if self.path.split("?")[0] == "/health":
            self._send(200, {"ok": True, "status": "ok", "version": __version__})
        else:
            self._send(404, {"ok": False, "error": {"message": "not found"}})

    def do_POST(self):  # noqa: N802
        path = self.path.split("?")[0]
        handler = ROUTES.get(path)
        if handler is None:
            self._send(404, {"ok": False, "error": {"message": "not found"}})
            return
        length = int(self.headers.get("Content-Length", 0))
        if length > MAX_BODY_BYTES:
            self._send(413, {"ok": False, "error": {"message": "request body too large"}})
            return
        raw = self.rfile.read(length) if length else b""
        try:
            payload = json.loads(raw.decode("utf-8")) if raw else {}
            if not isinstance(payload, dict):
                raise ValueError("request body must be a JSON object")
            body = handler(payload)
            self._send(200, body)
        except json.JSONDecodeError as exc:
            self._send(400, {"ok": False, "error": {"type": "JsonError", "message": str(exc)}})
        except ValueError as exc:
            self._send(400, {"ok": False, "error": {"type": "RequestError", "message": str(exc)}})
        except RegexError as exc:
            err = {"type": type(exc).__name__, "message": exc.message}
            if exc.span is not None and exc.source is not None:
                line, col = line_col(exc.source, exc.span.start)
                err.update(line=line, column=col, span=[exc.span.start, exc.span.end])
            self._send(400, {"ok": False, "error": err})


def make_server(host: str = "127.0.0.1", port: int = 8080) -> ThreadingHTTPServer:
    return ThreadingHTTPServer((host, port), _Handler)


def serve(host: str = "127.0.0.1", port: int = 8080) -> None:  # pragma: no cover
    httpd = make_server(host, port)
    print(f"regex-automata service listening on http://{host}:{port}")
    print("endpoints: GET /health, POST /compile, POST /match")
    try:
        httpd.serve_forever()
    except KeyboardInterrupt:
        pass
    finally:
        httpd.server_close()
