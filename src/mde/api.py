"""本地 HTTP API（纯后端，无前端资源）。

使用标准库 ThreadingHTTPServer；只监听 127.0.0.1，不内置鉴权——
它是“本地安全数据处理服务”，网络暴露与鉴权由部署方负责。

路由
====
GET  /health
GET  /policies
POST /policies/<id>/publish            发布新版本（?expected_version=N 乐观锁）
GET  /policies/<id>?version=N
POST /v1/exports                        {"data", "policy_id", "purpose",
                                         "version"?, "encrypt"?: bool}
POST /v1/exports/verify                 {"bundle", "encryption_key"?,
                                         "source_data"?}

注意：/v1/exports 的明文导出会在响应体中返回记录内容，仅适合本地使用；
跨网络使用应启用 encrypt 并离线分发密钥，或由部署方加 TLS/鉴权。
"""

from __future__ import annotations

import json
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from typing import Any
from urllib.parse import parse_qs, urlparse

from . import crypto
from .errors import (
    MdeError,
    PolicyConflict,
    PolicyNotFound,
    PolicyValidationError,
    VerificationError,
)
from .policy import PolicyStore
from .service import ExportService

_MAX_BODY = 10 * 1024 * 1024  # 10 MiB，防止误传超大请求。


def build_server(store: PolicyStore, service: ExportService, *,
                 host: str = "127.0.0.1", port: int = 8080) -> ThreadingHTTPServer:
    """创建服务器对象（不开始 serve）。"""

    class Handler(BaseHTTPRequestHandler):
        server_version = "mde/0.1"

        def log_message(self, fmt: str, *args: Any) -> None:  # noqa: A003
            # 统一、安静的访问日志（单行，stderr）。
            self.server.log(f"{self.address_string()} {fmt % args}")

        # ---- helpers -----------------------------------------------------

        def _read_json(self) -> Any:
            length = int(self.headers.get("Content-Length") or 0)
            if length <= 0:
                raise MdeError("request body required (JSON)")
            if length > _MAX_BODY:
                raise MdeError(f"request body too large (>{_MAX_BODY} bytes)")
            raw = self.rfile.read(length)
            try:
                return json.loads(raw.decode("utf-8"))
            except json.JSONDecodeError as exc:
                raise MdeError(f"invalid JSON body: {exc}") from None

        def _send(self, code: int, obj: Any) -> None:
            body = json.dumps(obj, ensure_ascii=False).encode("utf-8")
            self.send_response(code)
            self.send_header("Content-Type", "application/json; charset=utf-8")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)

        def _error(self, code: int, message: str, **extra: Any) -> None:
            payload: dict[str, Any] = {"error": message}
            payload.update(extra)
            self._send(code, payload)

        # ---- routing -----------------------------------------------------

        def do_GET(self) -> None:  # noqa: N802
            try:
                url = urlparse(self.path)
                parts = [p for p in url.path.split("/") if p]
                qs = parse_qs(url.query)
                if parts == ["health"]:
                    self._send(200, {"status": "ok",
                                     "verifying_key_hex": service.verifying_key_hex})
                    return
                if parts == ["policies"]:
                    ids = store.list_policies()
                    self._send(200, {"policies": [
                        {"policy_id": pid,
                         "versions": store.list_versions(pid)}
                        for pid in ids
                    ]})
                    return
                if len(parts) == 2 and parts[0] == "policies":
                    version = qs.get("version", [None])[0]
                    p = store.get(parts[1], int(version) if version else None)
                    self._send(200, p.to_dict())
                    return
                self._error(404, f"unknown path: {url.path}")
            except PolicyNotFound as exc:
                self._error(404, str(exc))
            except MdeError as exc:
                self._error(400, str(exc))
            except Exception as exc:  # 服务端缺陷不应泄露栈细节给调用方
                self._error(500, f"internal error: {type(exc).__name__}")

        def do_POST(self) -> None:  # noqa: N802
            try:
                url = urlparse(self.path)
                parts = [p for p in url.path.split("/") if p]
                qs = parse_qs(url.query)
                body = self._read_json()

                # POST /policies/<id>/publish
                if (len(parts) == 3 and parts[0] == "policies"
                        and parts[2] == "publish"):
                    if not isinstance(body, dict):
                        raise PolicyValidationError("body must be an object")
                    ev = qs.get("expected_version", [None])[0]
                    policy = store.publish(
                        parts[1],
                        body.get("rules", []),
                        aliases=body.get("aliases", []),
                        default_action=body.get("default_action", "deny"),
                        description=body.get("description", ""),
                        expected_version=int(ev) if ev is not None else None,
                    )
                    self._send(201, policy.to_dict())
                    return

                if parts == ["v1", "exports"]:
                    self._handle_export(body)
                    return
                if parts == ["v1", "exports", "verify"]:
                    self._handle_verify(body)
                    return
                self._error(404, f"unknown path: {url.path}")
            except PolicyConflict as exc:
                self._error(409, str(exc), policy_id=exc.policy_id,
                            current_version=exc.current)
            except PolicyNotFound as exc:
                self._error(404, str(exc))
            except PolicyValidationError as exc:
                self._error(422, str(exc))
            except VerificationError as exc:
                self._send(200, {"ok": False, "error": str(exc)})
            except MdeError as exc:
                self._error(400, str(exc))
            except Exception as exc:
                self._error(500, f"internal error: {type(exc).__name__}: {exc}")

        # ---- business handlers -------------------------------------------

        def _handle_export(self, body: dict[str, Any]) -> None:
            for k in ("data", "policy_id", "purpose"):
                if k not in body:
                    raise MdeError(f"export request missing {k!r}")
            enc_key = None
            if body.get("encrypt"):
                # 为简化本地使用：服务端为本次任务生成一把 Fernet 密钥，
                # 仅在响应中返回一次；生产式密钥分发不在本项目范围。
                enc_key = crypto.generate_fernet_key()
            bundle = service.export(
                body["data"],
                body["policy_id"],
                body["purpose"],
                version=body.get("version"),
                encryption_key=enc_key,
                save=body.get("save"),
            )
            resp: dict[str, Any] = {"bundle": bundle}
            if enc_key is not None:
                resp["encryption_key"] = enc_key.decode("ascii")
                resp["encryption_key_warning"] = (
                    "Returned once over the local channel; store out-of-band. "
                    "Anyone with this key can decrypt the bundle.")
            self._send(201, resp)

        def _handle_verify(self, body: dict[str, Any]) -> None:
            if "bundle" not in body:
                raise MdeError("verify request missing 'bundle'")
            key = body.get("encryption_key")
            key_bytes = key.encode("ascii") if isinstance(key, str) else None
            report = service.verify_bundle(
                body["bundle"],
                encryption_key=key_bytes,
                source_data=body.get("source_data"),
            )
            self._send(200, report)

    server = ThreadingHTTPServer((host, port), Handler)
    server.log = lambda msg: print(msg, flush=True)  # type: ignore[attr-defined]
    return server
