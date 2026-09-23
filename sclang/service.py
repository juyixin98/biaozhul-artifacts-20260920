"""JSON HTTP service for the ScL toolchain (Python standard library only).

Endpoints (all accept/return JSON, except ``/healthz``):

    POST /lex        {source}              -> tokens with source spans
    POST /parse      {source}              -> AST summary
    POST /analyze    {source}              -> bindings, capture/boxing info
    POST /compile    {source}              -> closure-converted IR module
    POST /eval       {source}              -> run converted IR + reference,
                                              comparing outputs
    GET  /healthz                           -> {"ok": true}

Compile errors come back as HTTP 400 with a structured payload (stage,
message, span, source snippet); runtime errors as HTTP 422.

Run:  python -m sclang.service [--host 127.0.0.1] [--port 8000]
"""

import argparse
import json
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

from . import ast_nodes as ast
from .errors import SclError
from .lexer import Lexer
from .pipeline import (analyze, analysis_json, error_payload,
                       run_frontend, run_reference, tokens_json)
from .ir import disassemble
from .parser import parse_source


def _ast_summary(node):
    """Compact JSON view of an AST (node type + fields, spans included)."""
    if node is None:
        return None
    if isinstance(node, list):
        return [_ast_summary(x) for x in node]
    if isinstance(node, ast.Stmt) or isinstance(node, ast.Expr):
        d = {"type": type(node).__name__}
        s = getattr(node, "span", None)
        if s is not None:
            d["span"] = [s.start, s.end, s.line, s.col, s.end_line, s.end_col]
        for fname, field in vars(node).items():
            if fname in ("span", "res"):
                continue
            if isinstance(field, (ast.Stmt, ast.Expr)) or isinstance(
                    field, list):
                d[fname] = _ast_summary(field)
            elif isinstance(field, (str, int, bool)) or field is None:
                d[fname] = field
        return d
    return node


def handle_request(path: str, body: dict) -> tuple[int, dict]:
    source = body.get("source", "")
    if not isinstance(source, str):
        return 400, {"ok": False, "stage": "compile",
                     "error": "'source' must be a string", "span": None}

    if path == "/lex":
        try:
            tokens = Lexer(source).tokenize()
        except SclError as e:
            return 400, error_payload(e)
        return 200, {"ok": True, "tokens": tokens_json(tokens)}

    if path == "/parse":
        try:
            program = parse_source(source)
        except SclError as e:
            return 400, error_payload(e)
        return 200, {"ok": True, "ast": _ast_summary(program)}

    # the rest need the full frontend
    try:
        fe = analyze(source)
    except SclError as e:
        return 400, error_payload(e)

    if path == "/analyze":
        return 200, {"ok": True, **analysis_json(fe)}

    if path == "/compile":
        return 200, {
            "ok": True,
            "module": fe.module.to_dict(),
            "disassembly": disassemble(fe.module),
        }

    if path == "/eval":
        # converted program on the independent VM
        try:
            result, out = run_frontend(fe)
        except SclError as e:
            return 422, error_payload(e)
        # reference source interpreter, for comparison
        try:
            ref_result, ref_out = run_reference(fe)
        except SclError as e:
            return 422, {"ok": False, "where": "reference",
                         **error_payload(e)}
        return 200, {
            "ok": True,
            "output": out,
            "result": _json_value(result),
            "reference_output": ref_out,
            "reference_result": _json_value(ref_result),
            "outputs_match": out == ref_out,
            "results_match": _values_equal(result, ref_result),
        }

    return 404, {"ok": False, "error": f"unknown path {path}"}


def _json_value(v):
    if v is None or isinstance(v, (str, int, bool)):
        return v
    if isinstance(v, tuple):
        return list(v)
    return str(v)


def _values_equal(a, b) -> bool:
    if isinstance(a, tuple):
        a = list(a)
    if isinstance(b, tuple):
        b = list(b)
    if type(a) is not type(b) and not (a is None or b is None):
        return False
    return a == b


class Handler(BaseHTTPRequestHandler):
    server_version = "ScLToolchain/1.0"

    def _send(self, status: int, payload: dict):
        data = json.dumps(payload, ensure_ascii=False).encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(data)))
        self.end_headers()
        self.wfile.write(data)

    def do_GET(self):
        if self.path.split("?")[0] == "/healthz":
            self._send(200, {"ok": True, "service": "sclang"})
            return
        self._send(404, {"ok": False, "error": "not found"})

    def do_POST(self):
        path = self.path.split("?")[0]
        if path not in ("/lex", "/parse", "/analyze", "/compile", "/eval"):
            self._send(404, {"ok": False, "error": f"unknown path {path}"})
            return
        try:
            length = int(self.headers.get("Content-Length", "0"))
            raw = self.rfile.read(length) if length else b"{}"
            body = json.loads(raw.decode("utf-8") or "{}")
            if not isinstance(body, dict):
                raise ValueError("body must be a JSON object")
        except (ValueError, UnicodeDecodeError) as e:
            self._send(400, {"ok": False, "stage": "compile",
                             "error": f"invalid JSON request: {e}",
                             "span": None})
            return
        try:
            status, payload = handle_request(path, body)
        except SclError as e:  # safety net
            status, payload = 400, error_payload(e)
        self._send(status, payload)

    def log_message(self, fmt, *args):
        # keep stderr concise
        return


def serve(host: str = "127.0.0.1", port: int = 8000):
    httpd = ThreadingHTTPServer((host, port), Handler)
    print(f"ScL JSON service listening on http://{host}:{port}")
    try:
        httpd.serve_forever()
    except KeyboardInterrupt:
        pass
    finally:
        httpd.server_close()


def main(argv=None):
    ap = argparse.ArgumentParser(description="ScL JSON HTTP service")
    ap.add_argument("--host", default="127.0.0.1")
    ap.add_argument("--port", type=int, default=8000)
    args = ap.parse_args(argv)
    serve(args.host, args.port)


if __name__ == "__main__":
    main()
