"""JSON HTTP service exposing the Slang toolchain.

Pure backend, Python standard library only (``http.server``).  Every endpoint
accepts ``{"source": "...", "filename": "..."}`` and answers with JSON.
Errors from every phase are returned with HTTP 400 and a body of
``{"ok": false, "error": {...}}`` where the error carries the source location.

Endpoints
---------
GET  /health                         service liveness
POST /parse                          tokens + AST
POST /analyze                        scope/escape analysis
POST /lower                          closure-converted IR
POST /run/ir                         run converted program via IR interpreter
POST /run/source                     run via the source reference interpreter
POST /compare                        run both; report whether traces agree
"""

from __future__ import annotations

import json
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

from . import __version__
from .analyzer import analyze
from .errors import LangError
from .ir_interp import IRInterpreter
from .lexer import tokenize
from .lower import lower_module
from .parser import parse
from .source_interp import SourceInterpreter


def _serialize_token(t):
    return {"kind": t.kind, "value": t.value, "text": t.text, "loc": t.loc.to_dict()}


def run_phase(data: dict, phase: str) -> dict:
    source = data.get("source")
    filename = data.get("filename", "<input>")
    if not isinstance(source, str):
        raise LangError("request body must contain a string field 'source'")

    if phase == "parse":
        tokens = tokenize(source, filename)
        tree = parse(source, filename)
        return {"tokens": [_serialize_token(t) for t in tokens if t.kind != "EOF"], "ast": tree.to_dict()}

    tree = parse(source, filename)
    if phase == "analyze":
        result = analyze(tree)
        return {"ast": tree.to_dict(), "analysis": result.to_dict()}

    result = analyze(tree)
    module = lower_module(tree, result)
    if phase == "lower":
        return {"analysis": result.to_dict(), "module": module.to_dict()}

    if phase == "run/ir":
        trace = IRInterpreter(module).run()
        return {"trace": trace, "module": module.to_dict()}

    if phase == "run/source":
        trace = SourceInterpreter(tree).run()
        return {"trace": trace}

    if phase == "compare":
        ir_trace = IRInterpreter(module).run()
        src_trace = SourceInterpreter(tree).run()
        return {
            "agree": ir_trace == src_trace,
            "ir_trace": ir_trace,
            "source_trace": src_trace,
        }

    raise LangError(f"unknown phase {phase!r}")  # pragma: no cover


PHASES = {"parse", "analyze", "lower", "run/ir", "run/source", "compare"}


class _Handler(BaseHTTPRequestHandler):
    server_version = f"SlangToolchain/{__version__}"

    def log_message(self, fmt, *args):  # quiet default access log
        return

    def _send_json(self, status: int, payload: dict):
        body = json.dumps(payload, ensure_ascii=False).encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def do_GET(self):
        if self.path.split("?")[0] == "/health":
            self._send_json(200, {"ok": True, "service": "slang", "version": __version__})
        else:
            self._send_json(404, {"ok": False, "error": {"phase": "http", "message": "not found"}})

    def do_POST(self):
        path = self.path.split("?")[0].lstrip("/")
        if path not in PHASES:
            self._send_json(404, {"ok": False, "error": {"phase": "http", "message": f"no endpoint /{path}"}})
            return
        try:
            length = int(self.headers.get("Content-Length", "0"))
            raw = self.rfile.read(length) if length else b"{}"
            try:
                data = json.loads(raw.decode("utf-8")) if raw.strip() else {}
            except json.JSONDecodeError as e:
                self._send_json(400, {"ok": False, "error": {"phase": "request", "message": f"invalid JSON: {e}"}})
                return
            if not isinstance(data, dict):
                raise LangError("request body must be a JSON object")
            result = run_phase(data, path)
            self._send_json(200, {"ok": True, "result": result})
        except LangError as e:
            self._send_json(400, {"ok": False, "error": e.to_dict()})
        except RecursionError:
            self._send_json(
                400,
                {"ok": False, "error": {"phase": "runtime", "message": "recursion limit exceeded"}},
            )
        except Exception as e:  # pragma: no cover - defensive
            self._send_json(500, {"ok": False, "error": {"phase": "internal", "message": str(e)}})


def create_server(host: str = "127.0.0.1", port: int = 8080) -> ThreadingHTTPServer:
    return ThreadingHTTPServer((host, port), _Handler)


def serve(host: str = "127.0.0.1", port: int = 8080):  # pragma: no cover - manual
    httpd = create_server(host, port)
    print(f"Slang JSON service on http://{host}:{port}")
    try:
        httpd.serve_forever()
    except KeyboardInterrupt:
        pass
    finally:
        httpd.server_close()
