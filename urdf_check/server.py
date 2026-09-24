"""HTTP 检查服务(仅标准库,纯后端,不控制任何硬件)。

接口:
  GET  /healthz        → 200 {"status": "ok"}
  POST /check          → 请求体为 URDF XML;响应 JSON 报告
                         200 = 通过,422 = 存在 error
  GET  /               → 用法说明

启动: python -m urdf_check.server [--host 127.0.0.1] [--port 8080]
"""

from __future__ import annotations

import argparse
import json
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

from .service import check_urdf

_MAX_BODY = 4 * 1024 * 1024  # 4 MiB,防资源耗尽


class CheckHandler(BaseHTTPRequestHandler):
    server_version = "urdf-check/1.0"

    def _send_json(self, status: int, payload: dict) -> None:
        body = json.dumps(payload, ensure_ascii=False).encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def do_GET(self) -> None:  # noqa: N802 (stdlib 命名)
        if self.path == "/healthz":
            self._send_json(200, {"status": "ok"})
        elif self.path == "/":
            self._send_json(200, {
                "service": "urdf-inertia-check",
                "usage": "POST /check with URDF XML body; GET /healthz",
            })
        else:
            self._send_json(404, {"error": "not found"})

    def do_POST(self) -> None:  # noqa: N802
        if self.path != "/check":
            self._send_json(404, {"error": "not found"})
            return
        try:
            length = int(self.headers.get("Content-Length", "0"))
        except ValueError:
            self._send_json(400, {"error": "invalid Content-Length"})
            return
        if length <= 0 or length > _MAX_BODY:
            self._send_json(413, {"error": "body empty or exceeds 4 MiB"})
            return
        source = self.rfile.read(length)
        report = check_urdf(source)
        self._send_json(200 if report.ok else 422, report.to_dict())

    def log_message(self, fmt, *args):  # 静默访问日志,保持输出干净
        pass


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(description="URDF 惯性检查 HTTP 服务")
    parser.add_argument("--host", default="127.0.0.1")
    parser.add_argument("--port", type=int, default=8080)
    args = parser.parse_args(argv)
    server = ThreadingHTTPServer((args.host, args.port), CheckHandler)
    print(f"urdf-check 服务已启动: http://{args.host}:{args.port} "
          "(POST /check, GET /healthz)")
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        pass
    finally:
        server.server_close()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
