"""本地 HTTP 接口（仅绑定 127.0.0.1，无鉴权，仅供本地测试使用）。

路由
====

``POST /keys``
    生成新主密钥。响应 ``{"kid", "created_at"}``。
``GET /keys``
    列出主密钥 ``[{"kid", "created_at", "is_latest"}, ...]``。
``POST /encrypt``
    JSON 请求 ``{"data_b64": "<base64 明文>", "chunk_size": 可选}``；
    响应 ``{"file_id", "plaintext_size", "chunk_size", "blocks"}``。
``GET /decrypt?file_id=<fid>``
    响应 ``{"file_id", "data_b64"}``。
``GET /files``
    列出全部文件及其信封信息。
``GET /files/<fid>``
    单个文件的信封信息。
``POST /rotate``
    JSON ``{"file_id": "...", "new_kid": 可选}``；响应轮换结果
    （含 ``blob_bytes_changed``，恒为 0）。

错误统一为 ``{"error": "..."}`` 并带合理状态码：400 参数错误、404 密钥 /
文件不存在、409 无主密钥或目标密钥相同、500 其余内部错误。

明文在 HTTP 层以 base64 承载，仅走本地回环；不要把该服务直接暴露到网络。
"""

from __future__ import annotations

import base64
import io
import json
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import parse_qs, urlsplit

from .errors import (
    AEADAuthenticationError,
    CorruptContainerError,
    EnvelopeError,
    InvalidFormatError,
    KeyNotFoundError,
    NoMasterKeyError,
    RotationError,
    TruncatedContainerError,
)
from .service import EnvelopeService

_MAX_BODY = 100 * 1024 * 1024  # 请求体上限 100 MiB


def _b64e(data: bytes) -> str:
    return base64.b64encode(data).decode("ascii")


def _b64d(text: str) -> bytes:
    return base64.b64decode(text.encode("ascii"), validate=True)


class _Handler(BaseHTTPRequestHandler):
    server_version = "EnvelopeService/1.0"

    # 让 service 实例可通过 self.server.envelope 访问
    @property
    def envelope(self) -> EnvelopeService:
        return self.server.envelope  # type: ignore[attr-defined]

    def log_message(self, fmt: str, *args) -> None:  # 安静一点
        if getattr(self.server, "verbose", False):
            super().log_message(fmt, *args)

    # -------------------------------------------------------------- 基础框架

    def _send_json(self, status: int, payload: dict) -> None:
        body = json.dumps(payload, ensure_ascii=False, indent=2).encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def _read_json(self) -> dict:
        length = int(self.headers.get("Content-Length", "0"))
        if length <= 0:
            raise ValueError("请求体为空，需要 JSON")
        if length > _MAX_BODY:
            raise ValueError("请求体超过 100 MiB 上限")
        raw = self.rfile.read(length)
        try:
            data = json.loads(raw.decode("utf-8"))
        except (UnicodeDecodeError, json.JSONDecodeError) as exc:
            raise ValueError("请求体不是合法 JSON") from exc
        if not isinstance(data, dict):
            raise ValueError("请求体必须是 JSON 对象")
        return data

    def _handle(self, fn):
        """统一异常 → HTTP 状态码映射。"""
        try:
            fn()
        except (ValueError, InvalidFormatError) as exc:
            self._send_json(400, {"error": str(exc)})
        except KeyNotFoundError as exc:
            self._send_json(404, {"error": str(exc)})
        except FileNotFoundError as exc:
            self._send_json(404, {"error": str(exc)})
        except FileExistsError as exc:
            self._send_json(409, {"error": str(exc)})
        except NoMasterKeyError as exc:
            self._send_json(409, {"error": str(exc)})
        except RotationError as exc:
            self._send_json(409, {"error": str(exc)})
        except (
            AEADAuthenticationError,
            CorruptContainerError,
            TruncatedContainerError,
        ) as exc:
            # 密文侧校验失败按 422 返回（内容存在但无法处理/不可信）
            self._send_json(422, {"error": str(exc)})
        except EnvelopeError as exc:
            self._send_json(500, {"error": str(exc)})

    # ------------------------------------------------------------------ 路由

    def do_GET(self) -> None:  # noqa: N802
        parts = urlsplit(self.path)
        path = parts.path.rstrip("/") or "/"

        if path == "/keys":
            self._handle(self._list_keys)
        elif path == "/files":
            self._handle(self._list_files)
        elif path.startswith("/files/"):
            file_id = path[len("/files/") :]
            self._handle(lambda: self._describe(file_id))
        elif path == "/decrypt":
            qs = parse_qs(parts.query)
            file_id = (qs.get("file_id") or [""])[0]
            self._handle(lambda: self._decrypt(file_id))
        elif path in ("/", "/health"):
            self._send_json(200, {"status": "ok"})
        else:
            self._send_json(404, {"error": f"未知路径：{path}"})

    def do_POST(self) -> None:  # noqa: N802
        path = urlsplit(self.path).path.rstrip("/") or "/"
        if path == "/keys":
            self._handle(self._create_key)
        elif path == "/encrypt":
            self._handle(self._encrypt)
        elif path == "/rotate":
            self._handle(self._rotate)
        else:
            self._send_json(404, {"error": f"未知路径：{path}"})

    # ------------------------------------------------------------------ 处理

    def _create_key(self) -> None:
        entry = self.envelope.create_master_key()
        self._send_json(201, {"kid": entry.kid, "created_at": entry.created_at})

    def _list_keys(self) -> None:
        entries = self.envelope.list_master_keys()
        latest = entries[-1].kid if entries else None
        self._send_json(
            200,
            {
                "keys": [
                    {
                        "kid": e.kid,
                        "created_at": e.created_at,
                        "is_latest": e.kid == latest,
                    }
                    for e in entries
                ]
            },
        )

    def _encrypt(self) -> None:
        data = self._read_json()
        raw = _b64d(data.get("data_b64", ""))
        chunk_size = int(data.get("chunk_size", 64 * 1024))
        loc = self.envelope.encrypt_stream(
            io.BytesIO(raw), chunk_size=chunk_size
        )
        info = self.envelope.describe(loc.file_id)
        self._send_json(
            201,
            {
                "file_id": loc.file_id,
                "plaintext_size": info["plaintext_size"],
                "chunk_size": info["chunk_size"],
                "blocks": info["blocks"],
                "wrapped_by_kid": info["wrapped_by_kid"],
            },
        )

    def _decrypt(self, file_id: str) -> None:
        if not file_id:
            raise ValueError("缺少 file_id 查询参数")
        buf = io.BytesIO()
        header = self.envelope.decrypt_stream(file_id, buf)
        self._send_json(
            200,
            {
                "file_id": file_id,
                "data_b64": _b64e(buf.getvalue()),
                "plaintext_size": header["plaintext_size"],
            },
        )

    def _list_files(self) -> None:
        items = []
        for fid in self.envelope.list_files():
            try:
                items.append(self.envelope.describe(fid))
            except (AEADAuthenticationError, KeyNotFoundError) as exc:
                items.append({"file_id": fid, "error": str(exc)})
        self._send_json(200, {"files": items})

    def _describe(self, file_id: str) -> None:
        self._send_json(200, self.envelope.describe(file_id))

    def _rotate(self) -> None:
        data = self._read_json()
        file_id = data.get("file_id")
        if not isinstance(file_id, str) or not file_id:
            raise ValueError("缺少 file_id")
        new_kid = data.get("new_kid")
        info = self.envelope.rotate_master_key(
            file_id, new_kid=new_kid if isinstance(new_kid, str) else None
        )
        self._send_json(
            200,
            {
                "file_id": info.file_id,
                "old_kid": info.old_kid,
                "new_kid": info.new_kid,
                "header_version": info.header_version,
                "blob_bytes_changed": info.blob_bytes_changed,
            },
        )


def build_server(
    store_dir: str | Path,
    host: str = "127.0.0.1",
    port: int = 8080,
    *,
    verbose: bool = False,
) -> ThreadingHTTPServer:
    """构造（但不启动）HTTP 服务。"""
    server = ThreadingHTTPServer((host, port), _Handler)
    server.envelope = EnvelopeService(store_dir)  # type: ignore[attr-defined]
    server.verbose = verbose  # type: ignore[attr-defined]
    # 进程内所有密码操作由 service 的锁串行化，ThreadingHTTPServer 仅用于并发连接处理
    return server


def serve(
    store_dir: str | Path,
    host: str = "127.0.0.1",
    port: int = 8080,
    *,
    verbose: bool = False,
) -> None:
    """阻塞式启动 HTTP 服务。"""
    server = build_server(store_dir, host, port, verbose=verbose)
    print(f"信封加密服务监听于 http://{host}:{port} （存储目录：{store_dir}）")
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        print("\n正在关闭……")
    finally:
        server.server_close()
