"""仅监听 127.0.0.1 的演示 HTTP 服务（纯后端，标准库实现）。

设计约束：
- 默认只绑定 127.0.0.1，不对外暴露；如需更改必须显式传 --host（有警告）；
- 所有数据经 base64 放进 JSON 请求体，内存中处理，不落任何明文临时文件；
- 请求体上限 10 MiB（本地演示服务，不是生产服务）；
- 通过静态 Bearer Token 做最简单的访问控制。

启动::

    python -m envelope_enc.server -k keys.json --token s3cret --port 8080

接口（均为 POST application/json，除健康检查外需 Authorization: Bearer <token>）：
    GET  /health
    POST /keys/rotate-master   {} -> {"old_kid", "new_kid"}
    GET  /keys                 -> {"active_kid", "keys":[{kid,status,created_at}]}
    POST /encrypt   {"data_b64": "...", "chunk_size": 65536?}
                   -> {"ciphertext_b64": "...", "file_id", "kid"}
    POST /decrypt   {"ciphertext_b64": "..."} -> {"data_b64": "...", "kid"}
    POST /rotate    {"ciphertext_b64": "..."}
                   -> {"ciphertext_b64": "...", "old_kid", "new_kid", "skipped"}
"""

from __future__ import annotations

import argparse
import base64
import binascii
import json
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

from .container import (
    DEFAULT_CHUNK_SIZE,
    ContainerError,
    decrypt_bytes,
    encrypt_bytes,
    parse_header,
    rotate_bytes,
)
from .keyring import Keyring, KeyringError

_MAX_BODY = 10 * 1024 * 1024  # 10 MiB


class ApiError(Exception):
    def __init__(self, status: int, message: str):
        super().__init__(message)
        self.status = status
        self.message = message


def _b64(data: bytes) -> str:
    return base64.b64encode(data).decode("ascii")


class ApiState:
    """跨请求共享的密钥环与串行化锁。"""

    def __init__(self, keyring: Keyring, token: str):
        self.keyring = keyring
        self.token = token
        # 主密钥轮换涉及 keyring 文件写，用一把锁简单串行化管理类操作
        self.lock = threading.Lock()


def handle_api(method: str, path: str, body: dict, state: ApiState) -> tuple[int, dict]:
    """纯函数式业务分发，方便自动化测试直接调用。"""
    if method == "GET" and path == "/health":
        return 200, {"status": "ok"}

    if method == "GET" and path == "/keys":
        active = state.keyring.active()
        return 200, {
            "active_kid": active.kid,
            "keys": [
                {"kid": mk.kid, "status": mk.status, "created_at": mk.created_at}
                for mk in state.keyring.list_keys()
            ],
        }

    if method == "POST" and path == "/keys/rotate-master":
        with state.lock:
            old = state.keyring.active()
            new = state.keyring.rotate_master()
        return 200, {"old_kid": old.kid, "new_kid": new.kid}

    if method == "POST" and path == "/encrypt":
        data = _require_b64(body, "data_b64")
        chunk_size = int(body.get("chunk_size", DEFAULT_CHUNK_SIZE))
        if chunk_size < 1:
            raise ApiError(400, "chunk_size 必须 >= 1")
        blob = encrypt_bytes(data, state.keyring, chunk_size=chunk_size)
        header = parse_header(blob)
        return 200, {
            "ciphertext_b64": _b64(blob),
            "file_id": header["file_id"],
            "kid": header["kid"],
        }

    if method == "POST" and path == "/decrypt":
        blob = _require_b64(body, "ciphertext_b64")
        plaintext = decrypt_bytes(blob, state.keyring)
        header = parse_header(blob)
        return 200, {"data_b64": _b64(plaintext), "kid": header["kid"]}

    if method == "POST" and path == "/rotate":
        blob = _require_b64(body, "ciphertext_b64")
        old_kid = parse_header(blob)["kid"]
        new_blob = rotate_bytes(blob, state.keyring)
        new_kid = parse_header(new_blob)["kid"]
        return 200, {
            "ciphertext_b64": _b64(new_blob),
            "old_kid": old_kid,
            "new_kid": new_kid,
            "skipped": old_kid == new_kid,
        }

    raise ApiError(404, f"未知接口：{method} {path}")


def _require_b64(body: dict, field: str) -> bytes:
    if not isinstance(body, dict) or field not in body:
        raise ApiError(400, f"缺少字段 {field}")
    raw = body[field]
    if not isinstance(raw, str):
        raise ApiError(400, f"字段 {field} 必须是 base64 字符串")
    try:
        return base64.b64decode(raw, validate=True)
    except (binascii.Error, ValueError) as exc:
        raise ApiError(400, f"字段 {field} 不是合法 base64") from exc


class _Handler(BaseHTTPRequestHandler):
    state: ApiState = None  # 由 make_server 注入到类属性

    # 演示服务保持安静，不打印逐请求访问日志
    def log_message(self, fmt: str, *args) -> None:  # noqa: A003
        return

    def _reply(self, status: int, payload: dict) -> None:
        data = json.dumps(payload).encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(data)))
        self.end_headers()
        self.wfile.write(data)

    def _authed(self) -> bool:
        expected = f"Bearer {self.state.token}"
        return self.headers.get("Authorization", "") == expected

    def do_GET(self) -> None:  # noqa: N802
        self._dispatch("GET")

    def do_POST(self) -> None:  # noqa: N802
        self._dispatch("POST")

    def _dispatch(self, method: str) -> None:
        if self.path.split("?")[0] != "/health" and not self._authed():
            self._reply(401, {"error": "未授权：需要 Authorization: Bearer <token>"})
            return
        try:
            body = {}
            if method == "POST":
                length = int(self.headers.get("Content-Length", "0"))
                if length > _MAX_BODY:
                    raise ApiError(413, f"请求体超过 {_MAX_BODY} 字节上限")
                raw = self.rfile.read(length) if length else b"{}"
                try:
                    body = json.loads(raw.decode("utf-8"))
                except (UnicodeDecodeError, json.JSONDecodeError) as exc:
                    raise ApiError(400, "请求体必须是 UTF-8 JSON") from exc
            status, payload = handle_api(
                method, self.path.split("?")[0], body, self.state
            )
            self._reply(status, payload)
        except ApiError as exc:
            self._reply(exc.status, {"error": exc.message})
        except (ContainerError, KeyringError) as exc:
            self._reply(400, {"error": str(exc)})
        except Exception as exc:  # noqa: BLE001 - 演示服务兜底，不泄漏堆栈
            self._reply(500, {"error": f"内部错误：{type(exc).__name__}"})


def make_server(host: str, port: int, keyring: Keyring, token: str) -> ThreadingHTTPServer:
    state = ApiState(keyring=keyring, token=token)
    handler = type("BoundHandler", (_Handler,), {"state": state})
    return ThreadingHTTPServer((host, port), handler)


def main(argv: list[str] | None = None) -> int:
    p = argparse.ArgumentParser(
        prog="python -m envelope_enc.server",
        description="信封加密轮换本地演示 HTTP 服务（仅 127.0.0.1，禁止生产使用）",
    )
    p.add_argument("-k", "--keyring", required=True)
    p.add_argument("--host", default="127.0.0.1")
    p.add_argument("--port", type=int, default=8080)
    p.add_argument("--token", required=True, help="静态 Bearer Token")
    args = p.parse_args(argv)

    keyring = Keyring(args.keyring)
    if not keyring.exists():
        p.error(f"密钥环不存在：{args.keyring}（请先运行 cli keyring-init）")
    if args.host != "127.0.0.1":
        print(
            f"警告：host={args.host} 会把演示服务暴露到非回环地址，"
            "该服务没有生产级防护。",
            flush=True,
        )

    httpd = make_server(args.host, args.port, keyring, args.token)
    print(
        f"信封加密演示服务监听 http://{args.host}:{args.port} "
        f"（密钥环 {args.keyring}，Ctrl+C 停止）",
        flush=True,
    )
    try:
        httpd.serve_forever()
    except KeyboardInterrupt:
        print("\n已停止。")
    finally:
        httpd.server_close()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
