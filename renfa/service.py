"""基于标准库 http.server 的 JSON 服务（纯后端，无前端）。

启动：
    python -m renfa.service --host 127.0.0.1 --port 8080

接口：
  GET  /healthz              健康检查
  POST /match                匹配请求，JSON body：
                             {"pattern": str, "text": str,
                              "op": "fullmatch"|"match"|"search"|"findall",
                              "debug": false(可选)}
成功响应 200：
  {"ok": true, "op": ..., "matched": bool,
   "match": {"start":int,"end":int,"text":str} | null,
   "matches": [{"start":..,"end":..,"text":..}, ...]}
错误响应 400：
  {"ok": false, "error": {"type": "syntax|compile|request",
                          "message": str, "location": "行:列"|null}}
"""

import json
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

from . import __version__
from .engine import Match, Regex
from .errors import RegexCompileError, RegexSyntaxError
from .lexer import tokenize

MAX_BODY_BYTES = 1 << 20  # 1 MiB
OPS = ("fullmatch", "match", "search", "findall")


# ---- AST / token 调试序列化 ----

def _ast_to_obj(node) -> dict:
    import dataclasses

    obj = {"type": type(node).__name__}
    for f in dataclasses.fields(node):
        val = getattr(node, f.name)
        if f.name == "span":
            obj["span"] = {
                "start": val.start,
                "end": val.end,
                "line": val.start_line,
                "col": val.start_col,
            }
        elif f.name == "children":
            obj["children"] = [_ast_to_obj(c) for c in val]
        elif f.name in ("left", "right", "child"):
            obj[f.name] = _ast_to_obj(val)
        elif f.name == "codepoint":
            obj["codepoint"] = val
            obj["char"] = chr(val)
        else:
            obj[f.name] = val
    return obj


def _tokens_to_obj(tokens) -> list[dict]:
    out = []
    for t in tokens:
        item = {
            "kind": t.kind,
            "start": t.span.start,
            "end": t.span.end,
            "line": t.span.start_line,
            "col": t.span.start_col,
        }
        if t.kind == "LITERAL":
            item["codepoint"] = t.value
            item["char"] = chr(t.value)  # type: ignore[arg-type]
        elif t.kind == "REPEAT":
            mn, mx = t.value  # type: ignore[misc]
            item["min"] = mn
            item["max"] = mx
            item["raw"] = t.raw
        out.append(item)
    return out


def _match_obj(m: Match) -> dict:
    return {"start": m.start, "end": m.end, "text": m.text}


def run_request(body: dict) -> tuple[dict, int]:
    """纯函数形式的请求处理，方便测试。返回 (响应对象, 状态码)。"""
    if not isinstance(body, dict):
        return _err("request", "请求体必须是 JSON 对象"), 400
    pattern = body.get("pattern")
    text = body.get("text", "")
    op = body.get("op", "search")
    debug = bool(body.get("debug", False))
    if not isinstance(pattern, str):
        return _err("request", "缺少字符串字段 'pattern'"), 400
    if not isinstance(text, str):
        return _err("request", "字段 'text' 必须是字符串"), 400
    if op not in OPS:
        return _err("request", f"字段 'op' 必须是 {list(OPS)} 之一"), 400

    try:
        regex = Regex(pattern)
    except RegexSyntaxError as e:
        loc = None if e.span is None else f"{e.span.start_line}:{e.span.start_col}"
        return _err("syntax", e.message, loc), 400
    except RegexCompileError as e:
        return _err("compile", str(e)), 400

    resp: dict = {"ok": True, "op": op, "pattern": pattern}
    if op == "findall":
        ms = regex.findall(text)
        resp["matched"] = bool(ms)
        resp["match"] = _match_obj(ms[0]) if ms else None
        resp["matches"] = [_match_obj(m) for m in ms]
    else:
        m = getattr(regex, op)(text)
        resp["matched"] = m is not None
        resp["match"] = _match_obj(m) if m is not None else None
        resp["matches"] = []

    if debug:
        _, tokens = tokenize(pattern)
        resp["tokens"] = _tokens_to_obj(tokens)
        resp["ast"] = _ast_to_obj(regex.ast)
        resp["nfa"] = regex.nfa.to_debug()
    return resp, 200


def _err(kind: str, message: str, location: str | None = None) -> dict:
    return {"ok": False, "error": {"type": kind, "message": message,
                                   "location": location}}


class _Handler(BaseHTTPRequestHandler):
    server_version = f"renfa/{__version__}"

    def _send_json(self, code: int, obj: dict) -> None:
        data = json.dumps(obj, ensure_ascii=False).encode("utf-8")
        self.send_response(code)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(data)))
        self.end_headers()
        self.wfile.write(data)

    def do_GET(self) -> None:
        if self.path.split("?")[0] == "/healthz":
            self._send_json(200, {"ok": True, "engine": "renfa",
                                  "version": __version__})
            return
        if self.path.split("?")[0] == "/":
            self._send_json(200, {"engine": "renfa", "version": __version__,
                                  "usage": "POST /match {pattern,text,op}"})
            return
        self._send_json(404, _err("request", f"未知路径 {self.path}"))

    def do_POST(self) -> None:
        if self.path.split("?")[0] != "/match":
            self._send_json(404, _err("request", f"未知路径 {self.path}"))
            return
        length = int(self.headers.get("Content-Length") or 0)
        if length <= 0:
            self._send_json(400, _err("request", "缺少 JSON 请求体"))
            return
        if length > MAX_BODY_BYTES:
            self._send_json(400, _err("request", "请求体超过 1 MiB 上限"))
            return
        raw = self.rfile.read(length)
        try:
            body = json.loads(raw.decode("utf-8"))
        except (UnicodeDecodeError, json.JSONDecodeError) as e:
            self._send_json(400, _err("request", f"请求体不是合法 JSON: {e}"))
            return
        obj, code = run_request(body)
        self._send_json(code, obj)

    def log_message(self, fmt: str, *args) -> None:  # 安静一点
        if self.server.verbose:  # type: ignore[attr-defined]
            super().log_message(fmt, *args)


def serve(host: str = "127.0.0.1", port: int = 8080, verbose: bool = False) -> None:
    httpd = ThreadingHTTPServer((host, port), _Handler)
    httpd.verbose = verbose  # type: ignore[attr-defined]
    print(f"renfa JSON 服务已启动: http://{host}:{port}  (Ctrl+C 停止)")
    try:
        httpd.serve_forever()
    except KeyboardInterrupt:
        pass
    finally:
        httpd.server_close()


def main(argv: list[str] | None = None) -> int:
    import argparse

    ap = argparse.ArgumentParser(description="renfa 正则自动机引擎 JSON 服务")
    ap.add_argument("--host", default="127.0.0.1")
    ap.add_argument("--port", type=int, default=8080)
    ap.add_argument("--verbose", action="store_true")
    args = ap.parse_args(argv)
    serve(args.host, args.port, args.verbose)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
