"""JSON service for the Imp toolchain.

Two transports share one request handler:

* stdio line protocol (default) — each input line is one JSON request, each
  output line one JSON response.  This is convenient for pipes and tests;
* HTTP (``python -m intervalai.service --http --port 8000``) with
  ``POST /analyze``, ``POST /execute`` and ``POST /parse``.

Request
-------
``{"op": "analyze", "source": "...",
   "input_bounds": {"n": {"lo": "0", "hi": "5"}},   // optional
   "include_points": true,                            // default true
   "include_cfg": false}                              // default false

``{"op": "execute", "source": "...",
   "inputs": {"n": 3}, "step_limit": 1000000}``

``{"op": "parse", "source": "...", "include_cfg": true}``

All integer interval endpoints are serialized as decimal **strings** so
arbitrarily large integers round-trip safely through JSON.

Errors are returned as structured objects
``{"status": "error", "error": "<code>", "message": "...", "loc": {...}}``.
"""

import argparse
import json
import sys

from . import concrete
from .errors import IvalError
from .pipeline import analyze_source, dump_cfg, execute_source, parse_program


def handle(req):
    if not isinstance(req, dict):
        return _err("invalid_request", "request must be a JSON object")
    op = req.get("op")
    src = req.get("source")
    if not isinstance(src, str):
        return _err("invalid_request", "missing string field 'source'")
    try:
        if op == "analyze":
            bounds = _parse_bounds(req.get("input_bounds"))
            d = analyze_source(
                src,
                input_bounds=bounds,
                include_points=bool(req.get("include_points", True)),
                include_cfg=bool(req.get("include_cfg", False)),
            )
            d["input_bounds"] = req.get("input_bounds") or {}
            return d
        if op == "execute":
            inputs = req.get("inputs") or {}
            if not isinstance(inputs, dict):
                return _err("invalid_request", "'inputs' must be an object")
            limit = int(req.get("step_limit", concrete.DEFAULT_STEP_LIMIT))
            out = execute_source(src, inputs, step_limit=limit)
            out["status"] = "ok"
            return out
        if op == "parse":
            prog = parse_program(src)
            d = {
                "status": "ok",
                "ast": _dump_ast(prog),
            }
            if req.get("include_cfg"):
                from . import ir as ir_mod
                d["cfg"] = dump_cfg(ir_mod.lower(prog))
            return d
        return _err("invalid_request", f"unknown op {op!r}")
    except IvalError as e:
        d = e.to_dict()
        d["status"] = "error"
        return d


def _parse_bounds(raw):
    if raw is None:
        return None
    if not isinstance(raw, dict):
        raise _service("'input_bounds' must map name -> {lo,hi}")
    out = {}
    for name, b in raw.items():
        if not isinstance(b, dict) or "lo" not in b or "hi" not in b:
            raise _service(f"bound for {name!r} must contain 'lo' and 'hi'")
        lo = _to_int(b["lo"])
        hi = _to_int(b["hi"])
        if lo > hi:
            raise _service(f"bound for {name!r} has lo > hi")
        out[name] = (lo, hi)
    return out


def _to_int(x):
    if isinstance(x, bool):
        raise _service("boolean where integer bound expected")
    if isinstance(x, int):
        return x
    if isinstance(x, str):
        try:
            return int(x, 10)
        except ValueError:
            pass
    raise _service(f"invalid integer bound {x!r}")


def _service(msg):
    from .errors import ServiceError
    return ServiceError(msg)


def _err(code, message):
    return {"status": "error", "error": code, "message": message}


# ------------------------------------------------------------------- AST dump

def _dump_ast(prog):
    return {
        "variables": [
            {"name": d.name, "init": None if d.init is None else str(d.init),
             "input": d.is_input, "loc": d.loc.to_dict()}
            for d in prog.var_decls
        ],
        "arrays": [
            {"name": a.name, "size": a.size,
             "elements": [str(x) for x in a.elems],
             "loc": a.loc.to_dict()}
            for a in prog.arr_decls
        ],
        "body": [_dump_stmt(s) for s in prog.body],
    }


def _dump_stmt(s):
    from . import ast_nodes as ast
    if isinstance(s, ast.Assign):
        return {"type": "assign", "loc": s.loc.to_dict(),
                "target": _dump_expr(s.target),
                "value": _dump_expr(s.value)}
    if isinstance(s, ast.If):
        return {"type": "if", "loc": s.loc.to_dict(),
                "cond": _dump_expr(s.cond),
                "then": [_dump_stmt(x) for x in s.then_body],
                "else": None if s.else_body is None
                else [_dump_stmt(x) for x in s.else_body]}
    if isinstance(s, ast.While):
        return {"type": "while", "loc": s.loc.to_dict(),
                "cond": _dump_expr(s.cond),
                "body": [_dump_stmt(x) for x in s.body]}
    raise AssertionError(type(s))


def _dump_expr(e):
    from . import ast_nodes as ast
    d = {"loc": e.loc.to_dict()}
    if isinstance(e, ast.IntLit):
        d.update(type="int", value=str(e.value))
    elif isinstance(e, ast.BoolLit):
        d.update(type="bool", value=e.value)
    elif isinstance(e, ast.Var):
        d.update(type="var", name=e.name)
    elif isinstance(e, ast.ArrayRef):
        d.update(type="arrayref", name=e.name, index=_dump_expr(e.index))
    elif isinstance(e, ast.Unary):
        d.update(type="unary", op=e.op, expr=_dump_expr(e.expr))
    elif isinstance(e, ast.Binary):
        d.update(type="binary", op=e.op,
                 lhs=_dump_expr(e.lhs), rhs=_dump_expr(e.rhs))
    else:
        raise AssertionError(type(e))
    return d


# ---------------------------------------------------------------- transports

def run_stdio(inp=None, out=None):
    inp = inp or sys.stdin
    out = out or sys.stdout
    for line in inp:
        line = line.strip()
        if not line:
            continue
        try:
            req = json.loads(line)
        except json.JSONDecodeError as e:
            resp = _err("invalid_json", f"malformed JSON: {e}")
        else:
            resp = handle(req)
        out.write(json.dumps(resp) + "\n")
        out.flush()


def _make_http_handler():
    from http.server import BaseHTTPRequestHandler

    class HTTPHandler(BaseHTTPRequestHandler):
        def log_message(self, fmt, *args):
            pass

        def do_GET(self):
            if self.path in ("/", "/health"):
                self._reply(200, {"status": "ok", "service": "intervalai"})
            else:
                self._reply(404, _err("not_found", self.path))

        def do_POST(self):
            if self.path not in ("/analyze", "/execute", "/parse"):
                self._reply(404, _err("not_found", self.path))
                return
            length = int(self.headers.get("Content-Length", "0"))
            raw = self.rfile.read(length) if length else b"{}"
            try:
                req = json.loads(raw or b"{}")
                if isinstance(req, dict) and "op" not in req:
                    req["op"] = self.path.lstrip("/")
            except json.JSONDecodeError as e:
                self._reply(400, _err("invalid_json", f"malformed JSON: {e}"))
                return
            self._reply(200, handle(req))

        def _reply(self, code, obj):
            body = json.dumps(obj).encode("utf-8")
            self.send_response(code)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)

    return HTTPHandler


HTTPHandler = _make_http_handler()


def run_http(port, host="127.0.0.1"):
    from http.server import HTTPServer

    server = HTTPServer((host, port), HTTPHandler)
    print(f"intervalai JSON service on http://{host}:{port}", file=sys.stderr)
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        pass


def main(argv=None):
    ap = argparse.ArgumentParser(description="Imp interval-analysis service")
    ap.add_argument("--http", action="store_true", help="serve HTTP instead of stdio")
    ap.add_argument("--port", type=int, default=8000)
    ap.add_argument("--host", default="127.0.0.1")
    args = ap.parse_args(argv)
    if args.http:
        run_http(args.port, args.host)
    else:
        run_stdio()


if __name__ == "__main__":
    main()
