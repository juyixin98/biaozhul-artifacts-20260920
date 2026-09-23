"""纯标准库实现的 JSON HTTP 服务（无任何第三方依赖）。

端点（均为 ``POST`` + JSON 请求体）::

    /parse      {"source", "filename"?}                 词法+语法，返回 AST
    /ir         {"source", "filename"?}                 返回优化前 SSA IR（文本+JSON）
    /analyze    {"source", "filename"?}                 SCCP 格分析结果（不改程序）
    /optimize   {"source", "filename"?, "step_limit"?}  分析+优化，返回报告/新IR/运行对比
    /run        {"source", "filename"?, "step_limit"?}  只执行（AST 金标准语义）
    GET /health                                        存活探针

错误一律返回 HTTP 200 且 JSON 带 ``"ok": false`` 与结构化 ``error``
（词法/语法/运行时错误是“正常 API 结果”，不是传输错误；仅请求体本身
不是合法 JSON 才给 400）。
"""

from __future__ import annotations

import json
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

from .cfg import build_cfg
from .interp import run_ast
from .ir_interp import run_ir
from .model import serialize_cfg
from .optimizer import optimize
from .parser import parse_source
from .pipeline import render_error
from .sccp import run_sccp
from .serialize import ast_to_dict
from .source import L0Error, SourceText
from .ssa import construct_ssa

DEFAULT_STEP_LIMIT = 2_000_000


class ServiceState:
    def __init__(self, step_limit: int = DEFAULT_STEP_LIMIT):
        self.step_limit = step_limit


def handle_request(body: dict, state: ServiceState | None = None) -> dict:
    state = state or ServiceState()
    endpoint = body.get("endpoint", "")
    source_text = body.get("source", "")
    filename = body.get("filename", "<input>")
    step_limit = int(body.get("step_limit", state.step_limit))

    if endpoint == "health":
        return {"ok": True, "status": "alive"}

    if not isinstance(source_text, str):
        return {"ok": False, "error": {"error": "request error",
                                       "message": "'source' must be a string",
                                       "location": None}}

    def fail(e: L0Error) -> dict:
        return {"ok": False, "error": render_error(e)}

    try:
        source = SourceText(source_text, filename)
        program = parse_source(source)

        if endpoint == "parse":
            return {"ok": True, "ast": ast_to_dict(program)}

        if endpoint == "ir":
            cfg = build_cfg(program, source)
            construct_ssa(cfg)
            return {"ok": True, "ir": serialize_cfg(cfg), "ir_text": cfg.to_text()}

        if endpoint == "analyze":
            cfg = build_cfg(program, source)
            construct_ssa(cfg)
            result = run_sccp(cfg)
            return {
                "ok": True,
                "ir": serialize_cfg(cfg),
                "sccp": result.to_report(),
            }

        if endpoint == "run":
            res = run_ast(program, source, step_limit=step_limit)
            return {"ok": True, "run": res.to_dict()}

        if endpoint == "optimize":
            cfg = build_cfg(program, source)
            construct_ssa(cfg)
            before_ir = serialize_cfg(cfg.clone())
            before = run_ast(program, source, step_limit=step_limit)
            before_ir_run = run_ir(cfg, source, step_limit=step_limit)
            _, report = optimize(cfg)
            after = run_ir(cfg, source, step_limit=step_limit)
            equivalent = before.signature() == after.signature()
            return {
                "ok": True,
                "ir_before": before_ir,
                "ir_after": serialize_cfg(cfg),
                "ir_after_text": cfg.to_text(),
                "report": report.to_dict(),
                "run_before_ast": before.to_dict(),
                "run_before_ir": before_ir_run.to_dict(),
                "run_after_ir": after.to_dict(),
                "equivalent": equivalent,
            }

        return {"ok": False, "error": {
            "error": "request error",
            "message": f"unknown endpoint {endpoint!r}; expected one of "
                       f"/parse /ir /analyze /optimize /run /health",
            "location": None}}
    except L0Error as e:
        return fail(e)


class _Handler(BaseHTTPRequestHandler):
    state: ServiceState = ServiceState()

    def _send(self, code: int, payload: dict) -> None:
        data = json.dumps(payload, ensure_ascii=False, indent=2).encode("utf-8")
        self.send_response(code)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(data)))
        self.end_headers()
        self.wfile.write(data)

    def do_GET(self):  # noqa: N802
        if self.path == "/health":
            self._send(200, {"ok": True, "status": "alive"})
        else:
            self._send(404, {"ok": False, "error": "not found"})

    def do_POST(self):  # noqa: N802
        length = int(self.headers.get("Content-Length", 0))
        raw = self.rfile.read(length) if length else b"{}"
        try:
            body = json.loads(raw.decode("utf-8"))
            if not isinstance(body, dict):
                raise ValueError("body must be a JSON object")
        except (ValueError, UnicodeDecodeError) as e:
            self._send(400, {"ok": False,
                             "error": {"error": "request error",
                                       "message": f"invalid JSON body: {e}",
                                       "location": None}})
            return
        body.setdefault("endpoint", self.path)
        # 路径形式 /optimize 与 body 内 "endpoint": "optimize" 都接受
        if body["endpoint"].startswith("/"):
            body["endpoint"] = body["endpoint"].lstrip("/")
        self._send(200, handle_request(body, self.state))

    def log_message(self, fmt, *args):  # 安静一点
        if self.server.verbose:  # type: ignore[attr-defined]
            super().log_message(fmt, *args)


def make_server(host: str = "127.0.0.1", port: int = 8000,
                step_limit: int = DEFAULT_STEP_LIMIT, verbose: bool = False
                ) -> ThreadingHTTPServer:
    handler = type("Handler", (_Handler,), {"state": ServiceState(step_limit)})
    server = ThreadingHTTPServer((host, port), handler)
    server.verbose = verbose  # type: ignore[attr-defined]
    return server


def serve(host: str = "127.0.0.1", port: int = 8000,
          step_limit: int = DEFAULT_STEP_LIMIT, verbose: bool = False) -> None:  # pragma: no cover
    server = make_server(host, port, step_limit, verbose)
    print(f"L0 constprop service listening on http://{host}:{port}")
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        pass
    finally:
        server.server_close()
