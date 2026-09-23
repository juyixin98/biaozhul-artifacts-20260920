"""本地 HTTP 服务（纯标准库实现，默认只绑定回环地址）。

接口：
- GET  /healthz           健康检查
- POST /split             拆分
- POST /recover           恢复
- POST /validate          校验单个份额信封

所有请求/响应均为 JSON。服务无状态、无线程间共享状态，
使用 ThreadingHTTPServer 处理并发。默认绑定 127.0.0.1，
不接任何生产账号或外部服务。
"""

import json
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from dataclasses import asdict

from . import core
from .errors import (
    TSSError,
    ParameterError,
    ThresholdError,
    DuplicateIndexError,
    FormatError,
    IntegrityError,
    ConsistencyError,
)
import base64 as _b64

MAX_BODY_BYTES = 16 * 1024 * 1024

# 错误码 -> HTTP 状态码
_STATUS = {
    ParameterError.code: 400,
    FormatError.code: 400,
    IntegrityError.code: 400,
    DuplicateIndexError.code: 409,
    ConsistencyError.code: 409,
    ThresholdError.code: 409,
}


class ServerConfig:
    def __init__(self, host: str = "127.0.0.1", port: int = 8080,
                 max_body_bytes: int = MAX_BODY_BYTES):
        self.host = host
        self.port = port
        self.max_body_bytes = max_body_bytes


def _decode_secret_input(data) -> bytes:
    """secret 支持 base64（secret_b64，优先）或 UTF-8 文本（secret_text）。"""
    if data.get("secret_b64") is not None:
        if not isinstance(data["secret_b64"], str):
            raise ParameterError("secret_b64 must be a string")
        try:
            return _b64.b64decode(data["secret_b64"].encode("ascii"),
                                           validate=True)
        except Exception as exc:
            raise ParameterError(f"invalid secret_b64: {exc}") from exc
    if data.get("secret_text") is not None:
        if not isinstance(data["secret_text"], str):
            raise ParameterError("secret_text must be a string")
        return data["secret_text"].encode("utf-8")
    raise ParameterError("either secret_b64 or secret_text must be provided")


def _ts_error_payload(exc: TSSError) -> dict:
    payload = {"error": exc.code, "detail": exc.message}
    for attr in ("suspect", "votes", "rejected"):
        val = getattr(exc, attr, None)
        if val:
            payload[attr] = val
    return payload


class _TSSHandler(BaseHTTPRequestHandler):
    server_version = "TSS-Local/1.0"

    def log_message(self, fmt, *args):  # 简化日志格式
        self.server.records.append(f"{self.address_string()} - {fmt % args}")

    # --- 工具 ---
    def _send_json(self, status: int, payload: dict):
        body = json.dumps(payload, ensure_ascii=False).encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        self.send_header("X-Content-Type-Options", "nosniff")
        self.end_headers()
        self.wfile.write(body)

    def _read_json(self) -> dict:
        length = int(self.headers.get("Content-Length", 0))
        if length <= 0:
            raise ParameterError("empty request body; expected JSON")
        if length > self.server.config.max_body_bytes:
            raise ParameterError(
                f"request body too large: {length} > {self.server.config.max_body_bytes}"
            )
        raw = self.rfile.read(length)
        try:
            data = json.loads(raw.decode("utf-8"))
        except Exception as exc:
            raise ParameterError(f"request body is not valid JSON: {exc}") from exc
        if not isinstance(data, dict):
            raise ParameterError("request body must be a JSON object")
        return data

    def _handle(self, handler):
        try:
            data = self._read_json()
            handler(data)
        except TSSError as exc:
            self._send_json(_STATUS.get(exc.code, 500), _ts_error_payload(exc))
        except Exception as exc:  # 防御性兜底，不把栈泄露给客户端
            self._send_json(500, {"error": "internal_error", "detail": str(exc)})

    # --- 路由 ---
    def do_GET(self):
        if self.path.split("?")[0] == "/healthz":
            self._send_json(200, {"status": "ok", "service": "tss-local", "version": 1})
        else:
            self._send_json(404, {"error": "not_found", "detail": self.path})

    def do_POST(self):
        path = self.path.split("?")[0]
        routes = {
            "/split": self._split,
            "/recover": self._recover,
            "/validate": self._validate,
        }
        handler = routes.get(path)
        if handler is None:
            self._send_json(404, {"error": "not_found", "detail": path})
            return
        self._handle(handler)

    # --- 业务 ---
    def _split(self, data):
        secret = _decode_secret_input(data)
        try:
            threshold = int(data["threshold"])
            total = int(data["total"])
        except (KeyError, TypeError, ValueError) as exc:
            raise ParameterError("threshold and total are required integers") from exc
        auth_key = core.decode_auth_key(data.get("auth_key_b64"))
        result = core.split(secret, threshold, total, auth_key=auth_key)
        self._send_json(200, asdict(result))

    def _recover(self, data):
        shares = data.get("shares")
        if not isinstance(shares, list) or not all(isinstance(s, str) for s in shares):
            raise ParameterError("shares must be a list of base64 strings")
        auth_key = core.decode_auth_key(data.get("auth_key_b64"))
        result = core.recover(shares, auth_key=auth_key)
        self._send_json(200, {
            "secret_b64": _b64.standard_b64encode(result.secret).decode("ascii"),
            "secret_text": result.secret.decode("utf-8", errors="replace"),
            "threshold": result.threshold,
            "total": result.total,
            "split_id": result.split_id,
            "authenticated": result.authenticated,
            "used_share_count": result.used_share_count,
            "rejected": result.rejected,
        })

    def _validate(self, data):
        share = data.get("share")
        if not isinstance(share, str):
            raise ParameterError("share must be a base64 string")
        auth_key = core.decode_auth_key(data.get("auth_key_b64"))
        info = core.validate(share, auth_key=auth_key)
        self._send_json(200, info)


def build_server(config: ServerConfig = None) -> ThreadingHTTPServer:
    config = config or ServerConfig()
    server = ThreadingHTTPServer((config.host, config.port), _TSSHandler)
    server.config = config
    server.records = []
    return server


def run_server(host: str = "127.0.0.1", port: int = 8080) -> None:
    """阻塞式启动服务（CLI 使用）。"""
    server = build_server(ServerConfig(host=host, port=port))
    print(f"TSS local service listening on http://{host}:{port}", flush=True)
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        print("\nshutting down ...", flush=True)
    finally:
        server.server_close()
