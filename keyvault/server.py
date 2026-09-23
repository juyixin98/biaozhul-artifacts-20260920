"""HTTP JSON API（基于标准库 http.server，无外部 Web 框架依赖）。

端点：
  POST   /keys                     生成新版本
  GET    /keys                     列出所有版本元数据
  GET    /keys/{vid}               查询单个版本
  POST   /keys/{vid}/activate      激活
  POST   /keys/{vid}/deactivate    停用
  POST   /keys/{vid}/destroy       销毁
  POST   /encrypt   {"plaintext_b64": "..."}          -> 密文信封
  POST   /decrypt   {"envelope": {...}, "expect_version": "v1"?}  -> 明文
  GET    /status                   服务状态与一致性自检
  GET    /audit                    审计日志（全部记录）
  GET    /audit/verify             校验审计哈希链

所有请求/响应均为 JSON；明文与密文以 base64 编码传输。
"""

from __future__ import annotations

import base64
import json
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

from .errors import (
    DecryptError,
    InvalidStateTransitionError,
    KeyDestroyedError,
    KeyNotFoundError,
    KeyVaultError,
    NoActiveKeyError,
    WrongVersionError,
)
from .service import KeyService

_ERROR_STATUS = {
    KeyNotFoundError: 404,
    InvalidStateTransitionError: 409,
    KeyDestroyedError: 410,
    NoActiveKeyError: 409,
    WrongVersionError: 422,
    DecryptError: 422,
}


def make_handler(service: KeyService):
    class Handler(BaseHTTPRequestHandler):
        server_version = "keyvault/0.1"

        # ---- 工具 ----
        def _send_json(self, obj, status=200):
            body = json.dumps(obj, ensure_ascii=False).encode("utf-8")
            self.send_response(status)
            self.send_header("Content-Type", "application/json; charset=utf-8")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)

        def _read_json(self) -> dict:
            length = int(self.headers.get("Content-Length") or 0)
            if length == 0:
                return {}
            return json.loads(self.rfile.read(length).decode("utf-8"))

        def _handle_error(self, e: Exception):
            status = 500
            for cls, code in _ERROR_STATUS.items():
                if isinstance(e, cls):
                    status = code
                    break
            if isinstance(e, KeyVaultError):
                self._send_json({"error": type(e).__name__, "message": str(e)}, status)
            else:
                self._send_json(
                    {"error": "InternalError", "message": "内部错误"}, 500
                )

        def log_message(self, fmt, *args):  # 静默访问日志，避免噪声
            pass

        # ---- 路由 ----
        def do_GET(self):
            try:
                parts = [p for p in self.path.split("/") if p]
                if parts == ["keys"]:
                    self._send_json({"versions": service.list_keys()})
                elif parts == ["status"]:
                    self._send_json(service.status())
                elif parts == ["audit"]:
                    self._send_json({"entries": list(service.audit)})
                elif parts == ["audit", "verify"]:
                    ok, reason = service.audit.verify()
                    self._send_json({"ok": ok, "reason": reason})
                elif len(parts) == 2 and parts[0] == "keys":
                    meta = service.store.get_metadata(parts[1])
                    if meta is None:
                        raise KeyNotFoundError(f"版本不存在: {parts[1]}")
                    self._send_json(meta.to_dict())
                else:
                    self._send_json({"error": "NotFound"}, 404)
            except Exception as e:  # noqa: BLE001
                self._handle_error(e)

        def do_POST(self):
            try:
                parts = [p for p in self.path.split("/") if p]
                body = self._read_json()
                if parts == ["keys"]:
                    self._send_json(service.generate_key().to_dict(), 201)
                elif len(parts) == 3 and parts[0] == "keys" and parts[2] == "activate":
                    self._send_json(service.activate(parts[1]).to_dict())
                elif len(parts) == 3 and parts[0] == "keys" and parts[2] == "deactivate":
                    self._send_json(service.deactivate(parts[1]).to_dict())
                elif len(parts) == 3 and parts[0] == "keys" and parts[2] == "destroy":
                    self._send_json(service.destroy(parts[1]).to_dict())
                elif parts == ["encrypt"]:
                    pt = base64.b64decode(body["plaintext_b64"])
                    aad = base64.b64decode(body.get("aad_b64", ""))
                    self._send_json({"envelope": service.encrypt(pt, aad)})
                elif parts == ["decrypt"]:
                    aad = base64.b64decode(body.get("aad_b64", ""))
                    pt = service.decrypt(
                        body["envelope"], aad, body.get("expect_version")
                    )
                    self._send_json(
                        {"plaintext_b64": base64.b64encode(pt).decode("ascii")}
                    )
                else:
                    self._send_json({"error": "NotFound"}, 404)
            except Exception as e:  # noqa: BLE001
                self._handle_error(e)

    return Handler


def run_server(data_dir: str, host: str = "127.0.0.1", port: int = 8765):
    service = KeyService(data_dir)
    httpd = ThreadingHTTPServer((host, port), make_handler(service))
    print(f"keyvault 服务已启动: http://{host}:{port}  数据目录: {data_dir}")
    try:
        httpd.serve_forever()
    except KeyboardInterrupt:
        pass
    finally:
        httpd.server_close()


if __name__ == "__main__":
    import argparse

    parser = argparse.ArgumentParser(description="keyvault 本地密钥版本审计服务")
    parser.add_argument("--data-dir", default="./keyvault-data", help="数据目录")
    parser.add_argument("--host", default="127.0.0.1")
    parser.add_argument("--port", type=int, default=8765)
    args = parser.parse_args()
    run_server(args.data_dir, args.host, args.port)
