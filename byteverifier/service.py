"""零依赖 JSON HTTP 服务（仅标准库 http.server）。

端点::

    POST /compile   {source}                 -> {ok, functions, disassembly, module_hex}
    POST /verify    {module_hex}             -> {ok, functions}
    POST /run       {source, fuel?}          -> {ok, result, steps, output}
    POST /mutate    {module_hex, byte, bit?  -> {ok, module_hex,
                    | value?}                   decode_ok, verify_ok, run_ok,
                                                error?}
    GET  /health                             -> {ok:true, service:"byteverifier"}

所有错误统一 HTTP 200 + ``{"ok": false, "error": {...}}``，只有传输层问题
（坏 JSON、请求过大、方法/路径不对）才用 4xx。
"""

from __future__ import annotations

import json
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

from .bytecode import OPERAND_SIZE, decode_module
from .common import ToolError
from .compiler import compile_source
from .interpreter import Interpreter
from .mutate import flip_bit, replace_byte
from .verifier import verify_module

MAX_BODY = 1 << 20  # 1 MiB


def _process_compile(source: str) -> dict:
    module = compile_source(source)
    from .bytecode import disassemble
    blob = module.encode()
    return {
        "ok": True,
        "functions": [f.name for f in module.functions],
        "disassembly": "\n\n".join(disassemble(f) for f in module.functions),
        "module_hex": blob.hex(),
        "module_size": len(blob),
    }


def _process_verify(module_hex: str) -> dict:
    data = bytes.fromhex(module_hex)
    module = decode_module(data)
    verify_module(module)
    return {
        "ok": True,
        "functions": [
            {"name": f.name, "max_stack": f.max_stack,
             "nlocals": f.nslots,
             "code_len": f.instructions[-1].pc + 1
             + OPERAND_SIZE[f.instructions[-1].opcode]}
            for f in module.functions
        ],
    }


def _process_run(source: str, fuel: int) -> dict:
    import io
    module = compile_source(source)
    verify_module(module, require_main=True)
    buf = io.StringIO()
    interp = Interpreter(module, fuel=fuel, out=buf)
    result = interp.call_main()
    return {
        "ok": True,
        "result": _jsonable(result),
        "steps": interp.steps,
        "output": buf.getvalue(),
    }


def _process_mutate(module_hex: str, byte: int,
                    bit: int | None, value: int | None) -> dict:
    """对模块做单字节变异后跑完整 解码->验证->（若通过）运行 流水线。"""
    data = bytes.fromhex(module_hex)
    if bit is not None:
        mutated = flip_bit(data, byte, bit)
        mutation = {"byte": byte, "bit": bit}
    else:
        val = 0 if value is None else value
        mutated = replace_byte(data, byte, val)
        mutation = {"byte": byte, "value": val}

    out: dict = {"ok": True, "mutation": mutation,
                 "decode_ok": False, "verify_ok": False, "run_ok": False}

    try:
        module = decode_module(mutated)
        out["decode_ok"] = True
    except ToolError as e:
        out["error"] = e.to_dict()
        out["module_hex"] = mutated.hex()
        return out

    try:
        verify_module(module)
        out["verify_ok"] = True
    except ToolError as e:
        out["error"] = e.to_dict()
        out["module_hex"] = mutated.hex()
        return out

    # 变异后仍然合法的代码才尝试运行（用较小 fuel 防止死循环拖垮服务）
    if module.by_name("main") is not None:
        try:
            import io
            buf = io.StringIO()
            interp = Interpreter(module, fuel=20_000, out=buf)
            r = interp.call_main()
            out["run_ok"] = True
            out["result"] = _jsonable(r)
            out["steps"] = interp.steps
            out["output"] = buf.getvalue()
        except ToolError as e:
            out["error"] = e.to_dict()
    out["module_hex"] = mutated.hex()
    return out


def _jsonable(v):
    return v if isinstance(v, (int, bool, str)) or v is None else str(v)


class _Handler(BaseHTTPRequestHandler):
    server_version = "byteverifier/1.0"

    def log_message(self, fmt, *args):  # 静默默认日志
        pass

    def _send(self, code: int, payload: dict) -> None:
        body = json.dumps(payload, ensure_ascii=False).encode("utf-8")
        self.send_response(code)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def _read_json(self) -> dict | None:
        try:
            length = int(self.headers.get("Content-Length", "0"))
        except ValueError:
            self._send(400, {"ok": False, "error": {"kind": "bad.length"}})
            return None
        if length > MAX_BODY:
            self._send(413, {"ok": False, "error": {"kind": "body.too.large"}})
            return None
        raw = self.rfile.read(length)
        try:
            payload = json.loads(raw.decode("utf-8"))
        except (UnicodeDecodeError, json.JSONDecodeError) as e:
            self._send(400, {"ok": False,
                             "error": {"kind": "bad.json", "message": str(e)}})
            return None
        if not isinstance(payload, dict):
            self._send(400, {"ok": False,
                             "error": {"kind": "bad.json", "message": "需要 JSON 对象"}})
            return None
        return payload

    def do_GET(self):
        if self.path.split("?")[0] == "/health":
            self._send(200, {"ok": True, "service": "byteverifier"})
        else:
            self._send(404, {"ok": False, "error": {"kind": "not.found"}})

    def do_POST(self):
        path = self.path.split("?")[0]
        handlers = {
            "/compile": self._handle_compile,
            "/verify": self._handle_verify,
            "/run": self._handle_run,
            "/mutate": self._handle_mutate,
        }
        handler = handlers.get(path)
        if handler is None:
            self._send(404, {"ok": False, "error": {"kind": "not.found"}})
            return
        payload = self._read_json()
        if payload is None:
            return
        try:
            self._send(200, handler(payload))
        except ToolError as e:
            self._send(200, {"ok": False, "error": e.to_dict()})
        except Exception as e:  # 服务不能崩溃：任何意外都包装成 internal
            self._send(200, {"ok": False, "error": {
                "phase": "service", "kind": "internal",
                "message": f"{type(e).__name__}: {e}",
            }})

    def _handle_compile(self, p):
        if "source" not in p or not isinstance(p["source"], str):
            raise ToolError("service", "bad.request", "需要字符串字段 source")
        return _process_compile(p["source"])

    def _handle_verify(self, p):
        if "module_hex" not in p or not isinstance(p["module_hex"], str):
            raise ToolError("service", "bad.request", "需要字符串字段 module_hex")
        try:
            return _process_verify(p["module_hex"])
        except ValueError:
            raise ToolError("decode", "hex", "module_hex 不是合法的十六进制串")

    def _handle_run(self, p):
        if "source" not in p or not isinstance(p["source"], str):
            raise ToolError("service", "bad.request", "需要字符串字段 source")
        fuel = int(p.get("fuel", 100_000))
        return _process_run(p["source"], fuel)

    def _handle_mutate(self, p):
        if "module_hex" not in p or not isinstance(p["module_hex"], str):
            raise ToolError("service", "bad.request", "需要字符串字段 module_hex")
        try:
            data = bytes.fromhex(p["module_hex"])
        except ValueError:
            raise ToolError("decode", "hex", "module_hex 不是合法的十六进制串")
        byte = p.get("byte")
        if not isinstance(byte, int) or not (0 <= byte < len(data)):
            raise ToolError("service", "bad.request",
                            f"需要 0..{len(data) - 1} 范围内的整数字段 byte")
        bit, value = p.get("bit"), p.get("value")
        if bit is not None and not (isinstance(bit, int) and 0 <= bit <= 7):
            raise ToolError("service", "bad.request", "bit 必须是 0..7 的整数")
        if value is not None and not (isinstance(value, int) and 0 <= value <= 255):
            raise ToolError("service", "bad.request", "value 必须是 0..255 的整数")
        return _process_mutate(p["module_hex"], byte, bit, value)


def serve(host: str = "127.0.0.1", port: int = 8080) -> None:
    httpd = ThreadingHTTPServer((host, port), _Handler)
    print(f"byteverifier JSON 服务已启动: http://{host}:{port}")
    try:
        httpd.serve_forever()
    except KeyboardInterrupt:
        pass
    finally:
        httpd.server_close()
