"""纯标准库实现的 JSON HTTP 服务。

路由
====
``GET  /health``
    返回 ``{"ok": true, "service": "tinyinfer"}``。

``POST /analyze``
    请求体（JSON）::

        {
          "source": "let id = fun x -> x in id 1",
          "value_restriction": true,   // 可选，默认 true
          "annotate": true,            // 可选，默认 true
          "evaluate": true             // 可选，默认 true
        }

    成功响应（``ok: true``）包含最终类型 ``type``、顶层绑定方案
    ``bindings``、求值结果 ``value`` 与完整推导轨迹 ``trace``；
    词法/语法/类型错误返回 HTTP 400，响应体为
    ``{"ok": false, "error": {kind,message,span,snippet}}``。
    求值期错误不影响类型推导结果，放在 ``eval_error`` 字段。

也可直接命令行启动：``python -m tinyinfer.server --port 8000``。
"""
from __future__ import annotations

import argparse
import json
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

from .pipeline import analyze


class Handler(BaseHTTPRequestHandler):
    server_version = "tinyinfer/1.0"

    def _send_json(self, status: int, payload: dict) -> None:
        body = json.dumps(payload, ensure_ascii=False, indent=2).encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def do_GET(self) -> None:  # noqa: N802 - http.server 约定
        if self.path.split("?")[0] == "/health":
            self._send_json(200, {"ok": True, "service": "tinyinfer",
                                  "version": "1.0.0"})
        else:
            self._send_json(404, {"ok": False, "error": {
                "kind": "NotFound", "message": f"未知路径 {self.path}",
                "span": None, "snippet": ""}})

    def do_POST(self) -> None:  # noqa: N802
        if self.path.split("?")[0] != "/analyze":
            self._send_json(404, {"ok": False, "error": {
                "kind": "NotFound", "message": f"未知路径 {self.path}",
                "span": None, "snippet": ""}})
            return
        length = int(self.headers.get("Content-Length") or 0)
        raw = self.rfile.read(length) if length else b""
        try:
            req = json.loads(raw.decode("utf-8")) if raw else {}
            if not isinstance(req, dict):
                raise ValueError("请求体必须是 JSON 对象")
            source = req["source"]
            if not isinstance(source, str):
                raise ValueError("字段 source 必须是字符串")
        except (ValueError, KeyError, UnicodeDecodeError) as err:
            self._send_json(400, {"ok": False, "error": {
                "kind": "BadRequest", "message": f"请求解析失败：{err}",
                "span": None, "snippet": ""}})
            return

        payload = analyze(
            source,
            value_restriction=bool(req.get("value_restriction", True)),
            annotate=bool(req.get("annotate", True)),
            evaluate=bool(req.get("evaluate", True)),
        )
        # 词法/语法/类型错误 -> 400；求值错误仍返回 200（推导是成功的）
        status = 200 if payload["ok"] else 400
        self._send_json(status, payload)

    def log_message(self, fmt: str, *args) -> None:  # noqa: A003
        # 简洁日志
        super().log_message(fmt, *args)


def build_server(host: str = "127.0.0.1", port: int = 8000) -> ThreadingHTTPServer:
    return ThreadingHTTPServer((host, port), Handler)


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description="tinyinfer JSON 服务")
    ap.add_argument("--host", default="127.0.0.1")
    ap.add_argument("--port", type=int, default=8000)
    args = ap.parse_args(argv)
    httpd = build_server(args.host, args.port)
    print(f"tinyinfer 服务监听 http://{args.host}:{args.port}")
    print("POST /analyze  {\"source\": \"...\"}    GET /health")
    try:
        httpd.serve_forever()
    except KeyboardInterrupt:
        print("\n已停止")
    finally:
        httpd.server_close()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
