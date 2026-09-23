"""JSON HTTP 服务（纯标准库实现，无第三方框架）。

路由（均为 POST，Content-Type: application/json）
=================================================
/compile
    请求: {"source": "...", "name"?: "...", "encoding"?: "text"|"hex"|"base64"}
    成功: {"ok": true, "module": {"name", "encoding": "hex", "binary",
                                  "disasm": [{func, instrs:[...]}]}}
    失败: {"ok": false, "stage": "compile", "errors": [ {message,line,col,...} ]}

/verify
    请求: {"module": {"binary": "...", "encoding": "hex"|"base64"}}
    成功: {"ok": true, "verified": true}
    失败: {"ok": false, "stage": "verify", "verified": false,
           "errors": [ VerifyError.to_dict(), ... ]}

/run
    请求: {"module": {...}, "fuel"?: int}
    成功: {"ok": true, "printed": [...], "return": "...", "steps": n,
           "max_stack": n}
    失败: {"ok": false, "stage": "verify"|"runtime"|"decode", ...}

/mutate
    请求: {"module": {...}, "strategy"?: "targeted"|"exhaustive",
           "func_index"?: int, "limit"?: int}
    成功: {"ok": true, "count": n,
           "mutants": [{label, func_index, offset, orig, value, kind,
                        "binary"?, include_binary?: false}]}
    默认不回传变异二进制（可能很大）；"include_binary": true 才带。

GET /health -> {"ok": true, "service": "slang-bytecode-verifier"}

错误响应都带 HTTP 200 与 ok:false（便于客户端统一解析）；
仅传输层/协议问题返回 4xx。
"""

from __future__ import annotations

import base64
import binascii
import json
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from typing import Any

from .bytecode import decode_module
from .compiler import compile_source
from .errors import CompileError, DecodeError, InvariantBroken, RuntimeErr
from .interpreter import Interpreter, format_value
from .location import SourceText
from .mutator import (apply_mutation, exhaustive_mutations,
                      targeted_mutations)
from .verifier import verify_module


# ---------------------------------------------------------------------
# 编解码辅助
# ---------------------------------------------------------------------
def encode_binary(data: bytes, encoding: str) -> str:
    if encoding == "hex":
        return data.hex()
    if encoding == "base64":
        return base64.b64encode(data).decode("ascii")
    raise ValueError("encoding 只能是 hex 或 base64")


def decode_binary(s: str, encoding: str) -> bytes:
    try:
        if encoding == "hex":
            return bytes.fromhex(s)
        if encoding == "base64":
            return base64.b64decode(s, validate=True)
    except (binascii.Error, ValueError):
        raise ValueError(f"模块不是合法的 {encoding} 数据")
    raise ValueError("encoding 只能是 hex 或 base64")


def disasm_module(module) -> list[dict]:
    out = []
    for fi, fn in enumerate(module.funcs):
        instrs = fn.decode()
        items = []
        for ins in instrs:
            d = fn.debug_at(ins.pc)
            items.append({
                "pc": ins.pc,
                "op": ins.name,
                "operand": ins.operand,
                "target": ins.target(),
                "src_start": d.src_start if d else -1,
                "src_end": d.src_end if d else -1,
            })
        out.append({
            "index": fi,
            "name": fn.name,
            "nparams": len(fn.param_types),
            "nlocals": len(fn.local_types),
            "instrs": items,
        })
    return out


def _compile_errors_to_json(e: CompileError, source_text: str) -> list[dict]:
    src = SourceText(source_text)
    res = []
    for d in e.diagnostics:
        line = col = None
        snippet = None
        if d.span is not None:
            line, col = src.line_col(d.span.start)
            snippet = src.line_text(line).strip()
        res.append({
            "message": d.message,
            "line": line,
            "col": col,
            "snippet": snippet,
            "hint": d.hint,
        })
    return res


# ---------------------------------------------------------------------
# 各操作的实现（便于直接复用/测试）
# ---------------------------------------------------------------------
def op_compile(body: dict) -> dict:
    source = body.get("source")
    if not isinstance(source, str):
        return {"ok": False, "stage": "request",
                "error": "缺少 source 字符串"}
    name = body.get("name", "main")
    enc = body.get("encoding", "hex")
    if enc not in ("hex", "base64"):
        return {"ok": False, "stage": "request", "error": "encoding 非法"}
    try:
        module = compile_source(source, module_name=name)
        binary = module.encode()
        return {
            "ok": True,
            "module": {
                "name": name,
                "encoding": enc,
                "binary": encode_binary(binary, enc),
                "nbytes": len(binary),
                "nfuncs": len(module.funcs),
                "disasm": disasm_module(module),
            },
        }
    except CompileError as e:
        return {"ok": False, "stage": "compile",
                "errors": _compile_errors_to_json(e, source)}


def _load_module(body: dict) -> tuple[Any | None, dict | None]:
    m = body.get("module")
    if not isinstance(m, dict) or "binary" not in m:
        return None, {"ok": False, "stage": "request",
                      "error": "缺少 module.binary"}
    enc = m.get("encoding", "hex")
    try:
        raw = decode_binary(m["binary"], enc)
    except ValueError as e:
        return None, {"ok": False, "stage": "request", "error": str(e)}
    try:
        return decode_module(raw), None
    except DecodeError as e:
        return None, {"ok": False, "stage": "decode", "verified": False,
                      "errors": [{"code": "DECODE_ERROR", "message": str(e),
                                  "function_index": getattr(e, "_func", None),
                                  "pc": -1, "shortest_error_path": []}]}


def op_verify(body: dict) -> dict:
    module, err = _load_module(body)
    if err is not None:
        return err
    errors = verify_module(module)
    if errors:
        return {"ok": False, "stage": "verify", "verified": False,
                "errors": [e.to_dict() for e in errors]}
    return {"ok": True, "stage": "verify", "verified": True,
            "nfuncs": len(module.funcs)}


def op_run(body: dict) -> dict:
    module, err = _load_module(body)
    if err is not None:
        return err
    fuel = int(body.get("fuel", 1_000_000))
    try:
        interp = Interpreter(module, fuel=fuel)
        result = interp.run_main()
    except InvariantBroken as e:
        return {"ok": False, "stage": "invariant", "error": str(e)}
    except RuntimeErr as e:
        return {"ok": False, "stage": "runtime",
                "error": str(e), "pc": e.pc, "function": e.func}
    return {
        "ok": True,
        "printed": result.printed,
        "return": format_value(result.return_value),
        "steps": result.steps,
        "max_stack": result.max_stack,
    }


def op_mutate(body: dict) -> dict:
    module, err = _load_module(body)
    if err is not None:
        return err
    strategy = body.get("strategy", "targeted")
    fi = body.get("func_index")
    include_binary = bool(body.get("include_binary", False))
    enc = body.get("module", {}).get("encoding", "hex")
    try:
        if strategy == "exhaustive":
            muts = exhaustive_mutations(module, func_index=fi)
        else:
            muts = targeted_mutations(module, func_index=fi)
    except Exception as e:  # 解码失败/预算超限
        return {"ok": False, "stage": "mutate", "error": str(e)}
    limit = body.get("limit")
    truncated = False
    if isinstance(limit, int) and limit >= 0 and len(muts) > limit:
        muts = muts[:limit]
        truncated = True
    items = []
    for m in muts:
        item = {"label": m.label, "func_index": m.func_index,
                "offset": m.offset, "orig": m.orig, "value": m.value,
                "kind": m.kind}
        if include_binary:
            item["binary"] = encode_binary(apply_mutation(module, m), enc)
            item["encoding"] = enc
        items.append(item)
    return {"ok": True, "strategy": strategy, "count": len(items),
            "truncated": truncated, "mutants": items}


ROUTES = {
    "/compile": op_compile,
    "/verify": op_verify,
    "/run": op_run,
    "/mutate": op_mutate,
}


# ---------------------------------------------------------------------
# HTTP 层
# ---------------------------------------------------------------------
class _Handler(BaseHTTPRequestHandler):
    server_version = "SlangVerifier/1.0"

    def log_message(self, fmt, *args):  # 安静一点：默认打 stderr
        if getattr(self.server, "verbose", False):
            super().log_message(fmt, *args)

    def _send_json(self, obj: dict, status: int = 200) -> None:
        data = json.dumps(obj, ensure_ascii=False).encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(data)))
        self.end_headers()
        self.wfile.write(data)

    def do_GET(self):  # noqa: N802
        if self.path.split("?")[0] == "/health":
            self._send_json({"ok": True, "service": "slang-bytecode-verifier"})
            return
        self._send_json({"ok": False, "error": "not found"}, status=404)

    def do_POST(self):  # noqa: N802
        path = self.path.split("?")[0]
        handler = ROUTES.get(path)
        if handler is None:
            self._send_json({"ok": False, "error": f"未知路由 {path}"},
                            status=404)
            return
        length = int(self.headers.get("Content-Length", 0))
        raw = self.rfile.read(length) if length else b""
        try:
            body = json.loads(raw.decode("utf-8")) if raw else {}
            if not isinstance(body, dict):
                raise ValueError("请求体必须是 JSON 对象")
        except (UnicodeDecodeError, json.JSONDecodeError, ValueError) as e:
            self._send_json({"ok": False, "stage": "request",
                             "error": f"请求 JSON 解析失败: {e}"},
                            status=400)
            return
        try:
            self._send_json(handler(body))
        except Exception as e:  # 服务不应当因单个请求崩溃
            self._send_json({"ok": False, "stage": "internal",
                             "error": f"{type(e).__name__}: {e}"},
                            status=500)


def serve(host: str = "127.0.0.1", port: int = 8000,
          verbose: bool = False) -> ThreadingHTTPServer:
    httpd = ThreadingHTTPServer((host, port), _Handler)
    httpd.verbose = verbose
    return httpd
