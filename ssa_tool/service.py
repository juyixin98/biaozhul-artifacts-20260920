"""JSON 服务：把 SSA 工具链各阶段以 HTTP POST 暴露。

仅依赖标准库。启动：

    python -m ssa_tool.service --host 127.0.0.1 --port 8000

接口（均为 POST + JSON，返回 JSON）：

- ``/health``                         健康检查
- ``/parse``            {source, file?}
- ``/build_ir``         {source, file?}                      原始 IR
- ``/ssa``              {source, file?}                      SSA IR + 校验
- ``/eliminate``        {source, file?}                      φ 消除后 IR
- ``/interpret``        {source|ir, file?, args?, flavor?}   解释执行
- ``/pipeline``         {source, file?, args?, run?}         全流程对照

错误统一返回 ``{"ok": false, "error": ..., "message": ...}``，HTTP 状态码 400。
"""

from __future__ import annotations

import argparse
import json
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

from .errors import ToolchainError
from .interpreter import interpret
from .ir import FunctionIR, dump_function
from .ir_builder import build_ir
from .parser import parse
from .phi_elimination import eliminate_phis
from .pipeline import ast_to_dict, compile_pipeline
from .ssa_construction import construct_ssa
from .ssa_validate import validate_ssa

ROUTES = ("/health", "/parse", "/build_ir", "/ssa",
          "/eliminate", "/interpret", "/pipeline")


def _operand_from_dict(d: dict):
    from .ir import Const, Name
    if d["kind"] == "const":
        return Const(int(d["value"]))
    return Name(d["value"])


def _instr_from_dict(d: dict):
    from .ir import Instr
    return Instr(
        op=d["op"], dest=d.get("dest"),
        operands=[_operand_from_dict(o) for o in d.get("operands", [])],
        blocks=list(d.get("blocks", [])), slot=d.get("slot"), span=None)


def _phi_from_dict(d: dict):
    from .ir import PhiInstr
    incoming = {x["pred"]: _operand_from_dict(x["value"])
                for x in d.get("incoming", [])}
    return PhiInstr(d["dest"], d["slot"], incoming)


def function_from_dict(d: dict) -> FunctionIR:
    from .ir import Block
    blocks = []
    for bd in d["blocks"]:
        b = Block(bd["label"],
                  [_phi_from_dict(p) for p in bd.get("phis", [])],
                  [_instr_from_dict(i) for i in bd.get("instrs", [])],
                  _instr_from_dict(bd["terminator"]) if bd.get("terminator") else None)
        blocks.append(b)
    return FunctionIR(d["name"], list(d.get("params", [])), blocks,
                      d.get("flavor", "raw"))


def handle_route(path: str, payload: dict) -> dict:
    if path == "/health":
        return {"ok": True, "service": "ssa_tool", "routes": list(ROUTES)}

    if path == "/parse":
        program = parse(payload["source"], payload.get("file", "<request>"))
        return {"ok": True, "program": ast_to_dict(program)}

    if path == "/build_ir":
        program = parse(payload["source"], payload.get("file", "<request>"))
        fn = build_ir(program)
        return {"ok": True, "ir": fn.as_dict(), "text": dump_function(fn)}

    if path == "/ssa":
        program = parse(payload["source"], payload.get("file", "<request>"))
        raw = build_ir(program)
        ssa = construct_ssa(raw.copy())
        violations = validate_ssa(ssa, raise_on_error=False)
        return {
            "ok": not violations,
            "ir": ssa.as_dict(),
            "text": dump_function(ssa),
            "violations": [str(v) for v in violations],
        }

    if path == "/eliminate":
        program = parse(payload["source"], payload.get("file", "<request>"))
        raw = build_ir(program)
        ssa = construct_ssa(raw.copy())
        validate_ssa(ssa)
        exec_fn = eliminate_phis(ssa.copy())
        return {"ok": True, "ir": exec_fn.as_dict(),
                "text": dump_function(exec_fn)}

    if path == "/interpret":
        flavor = payload.get("flavor", "raw")
        if "ir" in payload:
            fn = function_from_dict(payload["ir"])
        else:
            program = parse(payload["source"], payload.get("file", "<request>"))
            raw = build_ir(program)
            if flavor == "raw":
                fn = raw
            elif flavor == "ssa":
                fn = construct_ssa(raw.copy())
            elif flavor == "exec":
                ssa = construct_ssa(raw.copy())
                validate_ssa(ssa)
                fn = eliminate_phis(ssa.copy())
            else:
                raise ToolchainError(f"未知 flavor {flavor}")
        r = interpret(fn, payload.get("args") or [],
                      max_steps=int(payload.get("max_steps", 1_000_000)),
                      trace=bool(payload.get("trace", False)))
        return {"ok": True, "value": r.value, "steps": r.steps,
                "trace": r.trace}

    if path == "/pipeline":
        return compile_pipeline(
            payload["source"], payload.get("file", "<request>"),
            args=payload.get("args") or [],
            run=bool(payload.get("run", True)),
            max_steps=int(payload.get("max_steps", 1_000_000)))

    raise ToolchainError(f"未知路径 {path}")


class Handler(BaseHTTPRequestHandler):
    server_version = "ssa_tool/1.0"

    def _send(self, code: int, body: dict) -> None:
        data = json.dumps(body, ensure_ascii=False, indent=2).encode("utf-8")
        self.send_response(code)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(data)))
        self.end_headers()
        self.wfile.write(data)

    def do_GET(self) -> None:  # noqa: N802
        if self.path == "/health":
            self._send(200, handle_route("/health", {}))
        else:
            self._send(404, {"ok": False, "error": "NotFound",
                             "message": "请使用 POST 调用；GET 仅支持 /health"})

    def do_POST(self) -> None:  # noqa: N802
        path = self.path.split("?", 1)[0]
        if path not in ROUTES:
            self._send(404, {"ok": False, "error": "NotFound",
                             "message": f"未知路径 {path}"})
            return
        length = int(self.headers.get("Content-Length", 0))
        raw_body = self.rfile.read(length) if length else b"{}"
        try:
            payload = json.loads(raw_body.decode("utf-8") or "{}")
            if not isinstance(payload, dict):
                raise ValueError("请求体必须是 JSON 对象")
            body = handle_route(path, payload)
            self._send(200, body)
        except ToolchainError as exc:
            self._send(400, {"ok": False, "error": type(exc).__name__,
                             "message": str(exc)})
        except (KeyError, ValueError, json.JSONDecodeError) as exc:
            self._send(400, {"ok": False, "error": "BadRequest",
                             "message": str(exc)})

    def log_message(self, fmt, *args) -> None:  # 安静一点
        return


def serve(host: str = "127.0.0.1", port: int = 8000) -> None:
    httpd = ThreadingHTTPServer((host, port), Handler)
    print(f"ssa_tool JSON 服务监听 http://{host}:{port}")
    print("路由:", ", ".join(ROUTES))
    try:
        httpd.serve_forever()
    except KeyboardInterrupt:
        print("\n停止服务")
    finally:
        httpd.server_close()


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description="SSA 构造与回退 JSON 服务")
    ap.add_argument("--host", default="127.0.0.1")
    ap.add_argument("--port", type=int, default=8000)
    ns = ap.parse_args(argv)
    serve(ns.host, ns.port)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
