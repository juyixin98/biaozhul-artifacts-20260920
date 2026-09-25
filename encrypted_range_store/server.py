"""本地 HTTP 服务（标准库 http.server，无第三方 Web 框架）。

接口：
    GET    /healthz                 健康检查
    PUT    /objects/{id}            上传明文（服务端加密落盘），需 Content-Length
    GET    /objects/{id}            整对象读取（支持 Range: bytes=…）
    DELETE /objects/{id}            删除对象

安全：
- 仅绑定 127.0.0.1；
- 设置环境变量 ERS_TOKEN 后，除 /healthz 外均需 Authorization: Bearer <token>；
- 任何完整性错误统一返回 409，且响应体不含明文片段。
"""

from __future__ import annotations

import hmac
import json
import logging
import os
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import urlsplit

from .errors import (
    IntegrityError,
    InvalidObjectId,
    InvalidRangeHeader,
    ObjectNotFound,
    RangeNotSatisfiable,
)
from .format import DEFAULT_BLOCK_SIZE
from .service import EncryptedObjectStore

MAX_BODY = 16 * 1024 * 1024  # 16 MiB，本地测试上限


def _status_for(exc: Exception) -> int:
    if isinstance(exc, ObjectNotFound):
        return 404
    if isinstance(exc, (InvalidObjectId, InvalidRangeHeader)):
        return 400
    if isinstance(exc, RangeNotSatisfiable):
        return 416
    if isinstance(exc, IntegrityError):
        return 409
    return 500


class Handler(BaseHTTPRequestHandler):
    server_version = "EncryptedRangeStore/1.0"
    store: EncryptedObjectStore = None  # 由 build_server 注入（类属性）
    token: str | None = None

    def log_message(self, fmt: str, *args) -> None:
        logging.getLogger("ers").info("%s - %s", self.address_string(), fmt % args)

    # ---- 辅助 ----
    def _json_error(self, status: int, message: str) -> None:
        body = json.dumps({"error": message}, ensure_ascii=False).encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def _object_id(self, path: str) -> str | None:
        prefix = "/objects/"
        if not path.startswith(prefix):
            return None
        rest = path[len(prefix):]
        # 不允许子路径 / 查询串
        if "/" in rest or not rest:
            return None
        return rest

    def _authorized(self) -> bool:
        if not self.token:
            return True
        header = self.headers.get("Authorization", "")
        expected = "Bearer " + self.token
        return hmac.compare_digest(header, expected)

    # ---- 路由 ----
    def do_GET(self) -> None:
        try:
            parts = urlsplit(self.path)
            path = parts.path
            if path == "/healthz":
                body = b'{"status":"ok"}\n'
                self.send_response(200)
                self.send_header("Content-Type", "application/json")
                self.send_header("Content-Length", str(len(body)))
                self.end_headers()
                self.wfile.write(body)
                return
            object_id = self._object_id(path)
            if object_id is None:
                self._json_error(404, "未找到该路径")
                return
            if not self._authorized():
                self._json_error(401, "缺少或无效的 Authorization")
                return

            range_header = self.headers.get("Range")
            if range_header is not None:
                data, start, end_exclusive, total = (
                    self.store.read_http_range(object_id, range_header)
                )
                self.send_response(206)
                self.send_header("Content-Type", "application/octet-stream")
                self.send_header(
                    "Content-Range",
                    "bytes %d-%d/%d" % (start, end_exclusive - 1, total),
                )
                self.send_header("Accept-Ranges", "bytes")
                self.send_header("Content-Length", str(len(data)))
                self.end_headers()
                self.wfile.write(data)
            else:
                data = self.store.get(object_id)
                self.send_response(200)
                self.send_header("Content-Type", "application/octet-stream")
                self.send_header("Accept-Ranges", "bytes")
                self.send_header("Content-Length", str(len(data)))
                self.end_headers()
                self.wfile.write(data)
        except Exception as exc:  # noqa: BLE001 - 统一错误映射
            self._json_error(_status_for(exc), str(exc))

    def do_PUT(self) -> None:
        try:
            parts = urlsplit(self.path)
            object_id = self._object_id(parts.path)
            if object_id is None:
                self._json_error(404, "未找到该路径")
                return
            if not self._authorized():
                self._json_error(401, "缺少或无效的 Authorization")
                return
            length = self.headers.get("Content-Length")
            if length is None:
                self._json_error(411, "需要 Content-Length（不支持 chunked 上传）")
                return
            try:
                length_i = int(length)
            except ValueError:
                self._json_error(400, "Content-Length 非法")
                return
            if length_i < 0 or length_i > MAX_BODY:
                self._json_error(413, "请求体为空或超出 %d 字节上限" % MAX_BODY)
                return
            body = self.rfile.read(length_i)
            if len(body) != length_i:
                self._json_error(400, "请求体长度与 Content-Length 不符")
                return
            self.store.put(object_id, body)
            resp = json.dumps(
                {"object_id": object_id, "size": length_i}
            ).encode("utf-8")
            self.send_response(201)
            self.send_header("Content-Type", "application/json; charset=utf-8")
            self.send_header("Content-Length", str(len(resp)))
            self.end_headers()
            self.wfile.write(resp)
        except Exception as exc:  # noqa: BLE001
            self._json_error(_status_for(exc), str(exc))

    def do_DELETE(self) -> None:
        try:
            parts = urlsplit(self.path)
            object_id = self._object_id(parts.path)
            if object_id is None:
                self._json_error(404, "未找到该路径")
                return
            if not self._authorized():
                self._json_error(401, "缺少或无效的 Authorization")
                return
            self.store.delete(object_id)
            self.send_response(204)
            self.end_headers()
        except Exception as exc:  # noqa: BLE001
            self._json_error(_status_for(exc), str(exc))


def build_server(
    host: str = "127.0.0.1",
    port: int = 8080,
    data_dir: str = "./data",
    block_size: int = DEFAULT_BLOCK_SIZE,
    token: str | None = None,
) -> ThreadingHTTPServer:
    store = EncryptedObjectStore(data_dir, block_size=block_size)
    # 每次构建独立子类，避免多个服务实例共享 Handler 类属性。
    handler = type("BoundHandler", (Handler,), {"store": store, "token": token})
    return ThreadingHTTPServer((host, port), handler)
