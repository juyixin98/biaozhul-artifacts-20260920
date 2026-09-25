"""HTTP 服务：ThreadingHTTPServer + 防重放验证。

路由：

* ``GET  /health``        —— 不签名，存活探针；
* ``ANY  /api/...``       —— 必须带完整认证头并通过 :class:`Verifier`。

只用标准库 ``http.server``，不引入 Web 框架。
"""

from __future__ import annotations

import argparse
import json
from http import HTTPStatus
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from typing import Any

from .crypto import sha256_hex
from .canonical import CanonicalizationError, canonical_request_target
from .keys import KeystoreError, load_keystore
from .logutil import configure_logging
from .nonce_store import MemoryNonceStore, SqliteNonceStore
from .verifier import DEFAULT_WINDOW_SECONDS, VerifyResult, Verifier

MAX_BODY_BYTES = 1 * 1024 * 1024  # 1 MiB
API_PREFIX = "/api/"
HEALTH_PATH = "/health"

# 验证错误码 -> HTTP 状态码。
_STATUS_BY_CODE = {
    "malformed_request": HTTPStatus.BAD_REQUEST,            # 400
    "unknown_key": HTTPStatus.UNAUTHORIZED,                 # 401
    "bad_signature": HTTPStatus.UNAUTHORIZED,              # 401
    "stale_timestamp": HTTPStatus.UNAUTHORIZED,            # 401
    "future_timestamp": HTTPStatus.UNAUTHORIZED,           # 401
    "replay_detected": HTTPStatus.CONFLICT,                # 409
}


class _ClientError(Exception):
    def __init__(self, status: HTTPStatus, code: str, message: str) -> None:
        super().__init__(message)
        self.status = status
        self.code = code
        self.message = message


class AntiReplayServer(ThreadingHTTPServer):
    # 默认 backlog 仅 5，并发突增时内核会拒绝新连接；提到 128。
    request_queue_size = 128
    daemon_threads = True
    allow_reuse_address = True


class AntiReplayHandler(BaseHTTPRequestHandler):
    server_version = "AntiReplay/1.0"
    protocol_version = "HTTP/1.1"

    # 注：verifier / log 装配在 server 实例上，handler 通过 self.server 访问。

    # ---- 框架钩子 --------------------------------------------------------

    def do_GET(self) -> None:
        self._dispatch()

    def do_POST(self) -> None:
        self._dispatch()

    def do_PUT(self) -> None:
        self._dispatch()

    def do_DELETE(self) -> None:
        self._dispatch()

    def log_message(self, fmt: str, *args: Any) -> None:
        # 吞掉 BaseHTTPRequestHandler 默认的访问日志（可能带路径/查询串），
        # 全部改走我们自己的结构化、脱敏日志。
        return

    # ---- 主分发 ----------------------------------------------------------

    def _dispatch(self) -> None:
        try:
            raw_target = self.path
            # 先规范化请求目标：路由与签名验证都基于同一个规范结果，
            # 杜绝“路由看到一种路径、签名看到另一种路径”的歧义。
            try:
                cpath, cquery = canonical_request_target(raw_target)
            except CanonicalizationError as exc:
                raise _ClientError(
                    HTTPStatus.BAD_REQUEST, "malformed_request",
                    f"request target rejected: {exc}",
                ) from None

            if self.command == "GET" and cpath == HEALTH_PATH:
                # health 不处理正文；若客户端异常地携带了 body，强制关闭连接，
                # 避免 keep-alive 复用时把残留正文当成下一个请求。
                if self.headers.get("Content-Length") not in (None, "0"):
                    self.close_connection = True
                self._write_json(HTTPStatus.OK, {"status": "ok"})
                return

            if not (cpath == "/api" or cpath.startswith(API_PREFIX)):
                # 未知路由：尚未读取正文，关闭连接以免 keep-alive 复用错位。
                self.close_connection = True
                self._write_json(
                    HTTPStatus.NOT_FOUND,
                    {"error": {"code": "not_found", "message": "unknown route"}},
                )
                return

            body = self._read_body()

            # 阶段 1：认证（头格式/密钥/规范编码/HMAC/时间窗）——不触碰 nonce 表。
            verifier = self.server.verifier  # type: ignore[attr-defined]
            auth = verifier.authenticate(
                method=self.command,
                headers=self.headers,
                body=body,
                precomputed_target=(cpath, cquery),
            )
            if isinstance(auth, VerifyResult):
                self._reject(auth)
                return

            # 阶段 2：所有可能失败的正文/业务前置校验。此阶段失败不烧 nonce，
            # 诚实客户端可用同一签名安全重试。
            if self.command == "POST":
                try:
                    payload = json.loads(body.decode("utf-8")) if body else {}
                except (UnicodeDecodeError, json.JSONDecodeError):
                    raise _ClientError(
                        HTTPStatus.BAD_REQUEST, "invalid_json",
                        "request body must be UTF-8 encoded JSON",
                    )
                if not isinstance(payload, dict):
                    raise _ClientError(
                        HTTPStatus.BAD_REQUEST, "invalid_json",
                        "request body must be a JSON object",
                    )

            # 阶段 3：原子登记 nonce —— 生效前的最后一步。
            #     并发重复请求在此裁决：恰好一个通过，其余 409。
            replay = verifier.commit_nonce(auth)
            if replay is not None:
                self._reject(replay)
                return

            # 阶段 4：生效（本服务的“处理”即回显摘要）。nonce 已登记，
            # 此处之后不再有可预期的客户端可修复失败。
            if self.command == "POST":
                response = {
                    "status": "accepted",
                    "body_sha256": sha256_hex(body),
                    "top_level_keys": sorted(payload.keys()),
                }
            else:
                response = {"status": "accepted", "body_sha256": sha256_hex(body)}

            self._audit(
                ok=True,
                code="accepted",
                key_id=auth.key_id,
                nonce=auth.nonce,
            )
            self._write_json(HTTPStatus.OK, response)

        except _ClientError as exc:
            # 客户端错误可能发生在正文读取之前；关闭连接，避免 keep-alive 错位。
            self.close_connection = True
            self._audit(ok=False, code=exc.code, key_id=None, nonce=None)
            self._write_json(
                exc.status, {"error": {"code": exc.code, "message": exc.message}}
            )
        except Exception:  # noqa: BLE001 - 未预期异常一律 500，且不回显细节
            self.server.log.exception("internal error while handling request")  # type: ignore[attr-defined]
            try:
                self._write_json(
                    HTTPStatus.INTERNAL_SERVER_ERROR,
                    {"error": {"code": "internal_error", "message": "internal error"}},
                )
            except Exception:  # noqa: BLE001
                pass

    # ---- 读取正文 --------------------------------------------------------

    def _read_body(self) -> bytes:
        # RFC 9112：Content-Length 必须唯一；重复头（即使值相同）必须拒绝，
        # 防止与解析宽松的前置代理之间产生请求走私歧义。
        raw_lens = self.headers.get_all("Content-Length")
        if raw_lens is None:
            if self.command in ("POST", "PUT"):
                raise _ClientError(
                    HTTPStatus.LENGTH_REQUIRED, "length_required",
                    "Content-Length header is required",
                )
            return b""
        if len(raw_lens) != 1:
            raise _ClientError(
                HTTPStatus.BAD_REQUEST, "malformed_request",
                "duplicate Content-Length header is not allowed",
            )
        raw_len = raw_lens[0]
        # 不允许逗号合并形态（"21, 21"）或任何非纯数字内容。
        if not raw_len.isdigit():
            raise _ClientError(
                HTTPStatus.BAD_REQUEST, "malformed_request",
                "invalid Content-Length",
            )
        length = int(raw_len)
        if length > MAX_BODY_BYTES:
            raise _ClientError(
                HTTPStatus.REQUEST_ENTITY_TOO_LARGE, "payload_too_large",
                f"body exceeds limit of {MAX_BODY_BYTES} bytes",
            )
        return self.rfile.read(length)

    # ---- 输出 / 审计 -----------------------------------------------------

    def _write_json(self, status: HTTPStatus, payload: dict[str, Any]) -> None:
        data = json.dumps(payload, separators=(",", ":")).encode("utf-8")
        self.send_response(status.value)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(data)))
        self.end_headers()
        self.wfile.write(data)

    def _reject(self, result: VerifyResult) -> None:
        """把 verifier 的失败结果写成错误响应并记审计。"""
        assert result.error_code is not None
        status = _STATUS_BY_CODE.get(result.error_code, HTTPStatus.BAD_REQUEST)
        self._audit(
            ok=False,
            code=result.error_code,
            key_id=result.key_id,
            nonce=self.headers.get("X-Auth-Nonce"),
        )
        self._write_json(
            status,
            {"error": {"code": result.error_code, "message": result.error_message}},
        )

    def _audit(
        self, *, ok: bool, code: str, key_id: str | None, nonce: str | None
    ) -> None:
        """记一条审计事件。

        安全约束（见 README）：只记录 key id、对端地址、结果码和 nonce 短指纹；
        **不记录密钥、签名，也不记录查询串/正文。**
        """
        # nonce 来自请求头，可能含非 ASCII（此时 verifier 已判 malformed）。
        # 指纹计算必须容错：用替换编码，绝不让审计抛异常把 400 变成 500。
        if nonce:
            nonce_bytes = nonce.encode("utf-8", errors="replace")
            nonce_fp = sha256_hex(nonce_bytes)[:8]
        else:
            nonce_fp = "-"
        self.server.log.info(  # type: ignore[attr-defined]
            "event=%s result=%s key_id=%s nonce_fp=%s peer=%s",
            "request_accepted" if ok else "request_rejected",
            code,
            key_id or "-",
            nonce_fp,
            self.client_address[0],
        )


def build_server(
    *,
    keystore_path: str,
    host: str,
    port: int,
    window_seconds: int = DEFAULT_WINDOW_SECONDS,
    store: str = "memory",
    db_path: str = "nonces.db",
    log_level: str = "INFO",
) -> ThreadingHTTPServer:
    """装配一个可 ``serve_forever()`` 的服务实例（测试也用它）。"""
    try:
        keys = load_keystore(keystore_path)
    except KeystoreError as exc:
        raise SystemExit(f"failed to load keystore: {exc}") from exc

    logger = configure_logging(
        log_level,
        # 把每个密钥的 hex 形态注册给脱敏过滤器（纵深防御）。
        secrets=[secret.hex() for secret in keys.values()],
    )

    if store == "memory":
        nonce_store: Any = MemoryNonceStore(ttl_seconds=2 * window_seconds)
    elif store == "sqlite":
        nonce_store = SqliteNonceStore(db_path, ttl_seconds=2 * window_seconds)
    else:
        raise SystemExit(f"unknown nonce store: {store!r}")

    verifier = Verifier(keys, nonce_store, window_seconds=window_seconds)

    server = AntiReplayServer((host, port), AntiReplayHandler)
    # handler 统一通过 self.server.<name> 取这些依赖（见 AntiReplayHandler）。
    server.verifier = verifier
    server.nonce_store = nonce_store
    server.log = logger
    logger.info(
        "event=server_start host=%s port=%d window=%ds store=%s keys=%d",
        host, port, window_seconds, store, len(keys),
    )
    return server


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Anti-replay HTTP service")
    parser.add_argument("--keystore", required=True, help="JSON keystore 路径")
    parser.add_argument("--host", default="127.0.0.1")
    parser.add_argument("--port", type=int, default=8080)
    parser.add_argument("--window", type=int, default=DEFAULT_WINDOW_SECONDS)
    parser.add_argument(
        "--store", choices=("memory", "sqlite"), default="memory"
    )
    parser.add_argument("--db", default="nonces.db", help="SQLite 存储文件")
    parser.add_argument("--log-level", default="INFO")
    args = parser.parse_args(argv)

    server = build_server(
        keystore_path=args.keystore,
        host=args.host,
        port=args.port,
        window_seconds=args.window,
        store=args.store,
        db_path=args.db,
        log_level=args.log_level,
    )
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        pass
    finally:
        server.server_close()
        server.nonce_store.close()  # type: ignore[attr-defined]
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
