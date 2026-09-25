"""离线证书链验证 HTTP 服务（纯标准库实现，仅监听本地回环地址）。

接口：
- GET  /health  -> {"status": "ok"}
- POST /verify  -> 请求体 JSON：
    {
      "leaf": "<PEM>",
      "intermediates": ["<PEM>", ...],
      "trust_roots": ["<PEM>", ...],
      "validation_time": "2026-06-01T00:00:00Z",
      "purpose": "server_tls" | "client_tls",
      "hostname": "service.example.local"   // server_tls 必填
    }
  响应 200：{"valid": bool, "chain": [...]|null, "error": str|null,
             "revocation_checked": false, "revocation_note": "...", ...}
  响应 400：输入不合法（JSON 解析失败、缺字段、PEM 非法、时间非法等）

安全边界：服务不发起任何网络请求，不读取请求体以外的本地文件，
不查询吊销；吊销状态在响应中显式标记为未验证。
"""
from __future__ import annotations

import argparse
import json
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

from .verifier import PURPOSES, VerifyInputError, verify_chain

MAX_BODY = 1024 * 1024  # 1 MiB，证书包足够用，防止异常大请求


class VerifyHandler(BaseHTTPRequestHandler):
    server_version = "OfflineCertVerify/0.1"

    # 保持日志简洁，打到 stderr 即可
    def log_message(self, fmt, *args):  # noqa: N802 (stdlib 命名)
        pass

    def _send_json(self, status: int, payload: dict) -> None:
        body = json.dumps(payload, ensure_ascii=False, indent=2).encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def do_GET(self) -> None:  # noqa: N802
        if self.path == "/health":
            self._send_json(200, {"status": "ok"})
        else:
            self._send_json(404, {"error": "not found"})

    def do_POST(self) -> None:  # noqa: N802
        if self.path != "/verify":
            self._send_json(404, {"error": "not found"})
            return
        try:
            length = int(self.headers.get("Content-Length") or 0)
        except ValueError:
            self._send_json(400, {"error": "Content-Length 非法"})
            return
        if length <= 0 or length > MAX_BODY:
            self._send_json(400, {"error": "请求体为空或超过 1 MiB 限制"})
            return
        raw = self.rfile.read(length)
        try:
            req = json.loads(raw.decode("utf-8"))
        except Exception:
            self._send_json(400, {"error": "请求体不是合法 JSON"})
            return
        if not isinstance(req, dict):
            self._send_json(400, {"error": "请求体必须是 JSON 对象"})
            return

        leaf = req.get("leaf")
        intermediates = req.get("intermediates", [])
        trust_roots = req.get("trust_roots")
        validation_time = req.get("validation_time")
        purpose = req.get("purpose")
        hostname = req.get("hostname")

        if not isinstance(leaf, str):
            self._send_json(400, {"error": "缺少字符串字段 leaf"})
            return
        if not isinstance(intermediates, list) or not all(isinstance(p, str) for p in intermediates):
            self._send_json(400, {"error": "intermediates 必须是 PEM 字符串数组"})
            return
        if not isinstance(trust_roots, list) or not all(isinstance(p, str) for p in trust_roots):
            self._send_json(400, {"error": "缺少字符串数组字段 trust_roots"})
            return
        if hostname is not None and not isinstance(hostname, str):
            self._send_json(400, {"error": "hostname 必须是字符串"})
            return

        try:
            result = verify_chain(
                leaf_pem=leaf,
                intermediates_pem=intermediates,
                trust_roots_pem=trust_roots,
                validation_time_iso=validation_time,
                purpose=purpose,
                hostname=hostname,
            )
        except VerifyInputError as exc:
            self._send_json(400, {"error": str(exc)})
            return
        self._send_json(200, result)


def main() -> None:
    parser = argparse.ArgumentParser(description="离线证书链验证服务（仅本地回环）")
    parser.add_argument("--host", default="127.0.0.1", help="监听地址，默认 127.0.0.1")
    parser.add_argument("--port", type=int, default=8443, help="监听端口，默认 8443")
    args = parser.parse_args()
    server = ThreadingHTTPServer((args.host, args.port), VerifyHandler)
    print(f"listening on http://{args.host}:{args.port}  (POST /verify, GET /health)")
    print(f"supported purposes: {', '.join(PURPOSES)}")
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        pass
    finally:
        server.server_close()


if __name__ == "__main__":
    main()
