"""本地 HTTP 服务（标准库 http.server，无第三方 Web 框架）。

只绑定 127.0.0.1。所有请求/响应为 JSON；二进制密文用 base64 传输。
错误响应::

    {"error": {"code": "...", "message": "...", "request_id": "..."}}

路由：
    GET  /health
    POST /keys/generate
    POST /keys/rotate
    POST /keys/{id}/activate
    POST /keys/{id}/deactivate
    POST /keys/{id}/destroy
    GET  /keys
    GET  /keys/{id}
    POST /encrypt
    POST /decrypt
    GET  /audit
"""

from __future__ import annotations

import json
import logging
import uuid
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from typing import Any
from urllib.parse import urlparse

from .errors import KeyVersionError
from .service import KeyService

logger = logging.getLogger("keyversion.server")


class _Handler(BaseHTTPRequestHandler):
    server_version = "KeyVersionAudit/1.0"

    # -- 基础工具 -------------------------------------------------------
    def log_message(self, fmt: str, *args: Any) -> None:  # noqa: A003
        logger.info("%s - %s", self.client_address[0], fmt % args)

    @property
    def service(self) -> KeyService:
        return self.server.service  # type: ignore[attr-defined]

    def _send_json(self, status: int, body: dict[str, Any]) -> None:
        data = json.dumps(body, ensure_ascii=False, indent=2).encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(data)))
        self.send_header("X-Content-Type-Options", "nosniff")
        self.end_headers()
        self.wfile.write(data)

    def _read_json(self) -> dict[str, Any]:
        length = int(self.headers.get("Content-Length") or 0)
        if length <= 0:
            return {}
        raw = self.rfile.read(length)
        try:
            body = json.loads(raw.decode("utf-8"))
        except (UnicodeDecodeError, json.JSONDecodeError) as exc:
            raise _HttpError(400, "invalid_json", f"request body is not valid JSON: {exc}")
        if not isinstance(body, dict):
            raise _HttpError(400, "invalid_json", "request body must be a JSON object")
        return body

    def _error(self, exc: Exception, request_id: str | None = None) -> None:
        rid = request_id or uuid.uuid4().hex
        if isinstance(exc, KeyVersionError):
            self._send_json(
                exc.http_status,
                {"error": {"code": exc.code, "message": str(exc), "request_id": rid}},
            )
        elif isinstance(exc, _HttpError):
            self._send_json(
                exc.status,
                {"error": {"code": exc.code, "message": str(exc), "request_id": rid}},
            )
        else:
            logger.exception("unexpected error")
            self._send_json(
                500,
                {"error": {"code": "internal_error", "message": "internal server error", "request_id": rid}},
            )

    def _version_dict(self, info: Any) -> dict[str, Any]:
        return info.to_dict()

    # -- 路由 -----------------------------------------------------------
    def do_GET(self) -> None:  # noqa: N802
        parsed = urlparse(self.path)
        path = parsed.path.rstrip("/") or "/"
        try:
            if path == "/health":
                active = self.service.active_version()
                self._send_json(200, {"status": "ok", "active_version": active and active.version_id})
            elif path == "/keys":
                self._send_json(200, {"versions": [v.to_dict() for v in self.service.list_versions()]})
            elif path.startswith("/keys/"):
                vid = path.split("/", 2)[2]
                self._send_json(200, {"version": self.service.get_version(vid).to_dict()})
            elif path == "/audit":
                entries = [e.to_dict() for e in self.service.list_audit()]
                self._send_json(200, {"entries": entries, "count": len(entries)})
            else:
                raise _HttpError(404, "not_found", f"unknown path {path}")
        except Exception as exc:  # noqa: BLE001
            self._error(exc)

    def do_POST(self) -> None:  # noqa: N802
        parsed = urlparse(self.path)
        path = (parsed.path.rstrip("/") or "/").split("/")
        # ["", "keys", "{id}", "activate"] 等
        try:
            body = self._read_json()
            rid = body.get("request_id") or uuid.uuid4().hex

            if len(path) == 3 and path[1] == "keys" and path[2] == "generate":
                info = self.service.generate(request_id=rid)
                self._send_json(201, {"version": info.to_dict(), "request_id": rid})
            elif len(path) == 3 and path[1] == "keys" and path[2] == "rotate":
                info = self.service.rotate(request_id=rid)
                self._send_json(200, {"version": info.to_dict(), "request_id": rid})
            elif len(path) == 4 and path[1] == "keys" and path[3] == "activate":
                info = self.service.activate(path[2], request_id=rid)
                self._send_json(200, {"version": info.to_dict(), "request_id": rid})
            elif len(path) == 4 and path[1] == "keys" and path[3] == "deactivate":
                info = self.service.deactivate(path[2], request_id=rid)
                self._send_json(200, {"version": info.to_dict(), "request_id": rid})
            elif len(path) == 4 and path[1] == "keys" and path[3] == "destroy":
                info = self.service.destroy(path[2], request_id=rid)
                self._send_json(200, {"version": info.to_dict(), "request_id": rid})
            elif len(path) == 2 and path[1] == "encrypt":
                self._handle_encrypt(body, rid)
            elif len(path) == 2 and path[1] == "decrypt":
                self._handle_decrypt(body, rid)
            else:
                raise _HttpError(404, "not_found", f"unknown path {parsed.path}")
        except Exception as exc:  # noqa: BLE001
            self._error(exc, request_id=rid if "rid" in locals() else None)

    # -- 加解密 ---------------------------------------------------------
    def _handle_encrypt(self, body: dict[str, Any], rid: str) -> None:
        if "plaintext" not in body:
            raise _HttpError(400, "missing_field", "body requires 'plaintext' (base64 or utf-8 string)")
        plaintext = self._decode_plaintext(body["plaintext"])
        version_id = body.get("version_id")
        envelope, info = self.service.encrypt(plaintext, version_id=version_id, request_id=rid)
        self._send_json(
            200,
            {
                "ciphertext": KeyService.b64e(envelope),
                "encoding": "base64",
                "version_id": info.version_id,
                "state": info.state,
                "request_id": rid,
            },
        )

    def _handle_decrypt(self, body: dict[str, Any], rid: str) -> None:
        if "ciphertext" not in body:
            raise _HttpError(400, "missing_field", "body requires 'ciphertext' (base64 KVA1 envelope)")
        envelope = KeyService.b64d(str(body["ciphertext"]))
        plaintext, vid = self.service.decrypt(envelope, request_id=rid)
        self._send_json(
            200,
            {
                "plaintext": KeyService.b64e(plaintext),
                "plaintext_utf8": _maybe_utf8(plaintext),
                "encoding": "base64",
                "version_id": vid,
                "request_id": rid,
            },
        )

    @staticmethod
    def _decode_plaintext(value: Any) -> bytes:
        """明文字段解码。

        约定：传二进制时使用规范 base64（带正确填充，且编码结果与原文逐字节
        一致）。像 ``"first secret"``、``"hello"`` 这类普通文本不满足规范
        base64 的往返一致性，按 UTF-8 文本处理。
        """
        if not isinstance(value, str):
            raise _HttpError(400, "invalid_field", "'plaintext' must be a string")
        import base64
        import binascii

        try:
            raw = value.encode("ascii")
            decoded = base64.b64decode(raw, validate=True)
            if base64.b64encode(decoded) == raw:
                return decoded
        except (binascii.Error, ValueError, UnicodeEncodeError):
            pass
        return value.encode("utf-8")


class _HttpError(Exception):
    def __init__(self, status: int, code: str, message: str):
        super().__init__(message)
        self.status = status
        self.code = code


def _maybe_utf8(data: bytes) -> str | None:
    try:
        return data.decode("utf-8")
    except UnicodeDecodeError:
        return None


def build_server(host: str, port: int, service: KeyService) -> ThreadingHTTPServer:
    ThreadingHTTPServer.allow_reuse_address = True
    server = ThreadingHTTPServer((host, port), _Handler)
    server.service = service  # type: ignore[attr-defined]
    return server


def serve(host: str, port: int, service: KeyService) -> None:
    """阻塞运行 HTTP 服务（每个请求一个线程，状态机由存储层锁串行化）。"""
    server = build_server(host, port, service)
    logger.info("key version audit service listening on http://%s:%d", host, port)
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        logger.info("shutting down")
    finally:
        server.shutdown()
        server.server_close()
