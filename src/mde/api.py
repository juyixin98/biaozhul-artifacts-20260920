"""本地 HTTP API（标准库实现，默认仅绑定 127.0.0.1）。

端点：
- GET  /health
- POST /policies                    发布策略（不可变，返回指纹）
- GET  /policies/{id}               列出某 policy_id 的版本
- GET  /policies/fingerprint/{fp}   读取已发布策略
- POST /exports                     按指定用途 + 策略指纹导出
- POST /verify                      复验导出包（可选附 records/keys 做端到端重放）

不做鉴权：这是本地服务，绑定回环地址；请勿暴露到网络。
"""

from __future__ import annotations

import json
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Any, Optional, Tuple
from urllib.parse import urlparse

from .exporter import ExportService, verify_package
from .keys import load_or_create
from .policy import PolicyError, PolicyStore

_MAX_BODY = 8 * 1024 * 1024


class ApiState:
    def __init__(self, base_dir: Path):
        self.base_dir = Path(base_dir)
        self.keys, created = load_or_create(self.base_dir / "keys")
        self.store = PolicyStore(self.base_dir / "policies")
        self.service = ExportService(self.store, self.keys)
        self.keys_created = created


def _json_response(handler: BaseHTTPRequestHandler, status: int, body: Any) -> None:
    data = json.dumps(body, ensure_ascii=False, indent=2).encode("utf-8")
    handler.send_response(status)
    handler.send_header("Content-Type", "application/json; charset=utf-8")
    handler.send_header("Content-Length", str(len(data)))
    handler.end_headers()
    handler.wfile.write(data)


class _Handler(BaseHTTPRequestHandler):
    state: ApiState = None  # type: ignore[assignment]

    def log_message(self, fmt: str, *args: Any) -> None:  # 安静一点
        if getattr(self.server, "quiet", False):
            return
        super().log_message(fmt, *args)

    def _read_json(self) -> Tuple[Optional[Any], Optional[str]]:
        try:
            length = int(self.headers.get("Content-Length", "0"))
        except ValueError:
            return None, "Content-Length 非法"
        if length > _MAX_BODY:
            return None, "请求体过大"
        raw = self.rfile.read(length) if length else b""
        try:
            return json.loads(raw.decode("utf-8") or "null"), None
        except (json.JSONDecodeError, UnicodeDecodeError) as e:
            return None, f"JSON 解析失败: {e}"

    def do_GET(self) -> None:  # noqa: N802
        path = urlparse(self.path).path
        state: ApiState = self.state
        if path == "/health":
            _json_response(self, 200, {"status": "ok", "key_id": state.keys.key_id})
            return
        if path.startswith("/policies/fingerprint/"):
            fp = path.rsplit("/", 1)[-1]
            try:
                stored = state.store.get(fp)
            except PolicyError as e:
                _json_response(self, 404, {"error": str(e)})
                return
            from .policy import to_plain

            _json_response(self, 200, {
                "policy": to_plain(stored.policy.doc),
                "policy_fingerprint": fp,
                "created_at": stored.created_at,
            })
            return
        if path.startswith("/policies/"):
            pid = path.rsplit("/", 1)[-1]
            try:
                revs = state.store.revisions(pid)
            except PolicyError as e:
                _json_response(self, 404, {"error": str(e)})
                return
            _json_response(self, 200, {
                "policy_id": pid,
                "revisions": [{"revision": r, "policy_fingerprint": fp} for r, fp in revs],
            })
            return
        _json_response(self, 404, {"error": "not found"})

    def do_POST(self) -> None:  # noqa: N802
        path = urlparse(self.path).path
        state: ApiState = self.state
        body, err = self._read_json()
        if err:
            _json_response(self, 400, {"error": err})
            return

        if path == "/policies":
            from .exporter import utc_now_iso

            try:
                stored = state.store.publish(body, utc_now_iso())
            except PolicyError as e:
                _json_response(self, 400, {"error": f"策略校验失败: {e}"})
                return
            from .policy import to_plain

            _json_response(self, 200, {
                "policy_fingerprint": stored.policy.policy_fingerprint,
                "policy_id": body["policy_id"],
                "revision": body["revision"],
                "created_at": stored.created_at,
                "policy": to_plain(stored.policy.doc),
            })
            return

        if path == "/exports":
            return self._handle_export(body, state)
        if path == "/verify":
            return self._handle_verify(body, state)
        _json_response(self, 404, {"error": "not found"})

    def _handle_export(self, body: Any, state: ApiState) -> None:
        if not isinstance(body, dict):
            _json_response(self, 400, {"error": "请求体必须是对象"})
            return
        records = body.get("records")
        purpose = body.get("purpose")
        fp = body.get("policy_fingerprint")
        if not isinstance(records, list):
            _json_response(self, 400, {"error": "records 必须是数组"})
            return
        if not isinstance(purpose, str):
            _json_response(self, 400, {"error": "purpose 必须是字符串"})
            return
        if not isinstance(fp, str):
            _json_response(self, 400, {"error": "policy_fingerprint 必须是字符串"})
            return
        try:
            package = state.service.export(
                records=records, purpose=purpose, policy_fingerprint=fp,
                task_id=body.get("task_id"), requester=body.get("requester", "local-test"),
            )
        except PolicyError as e:
            _json_response(self, 404, {"error": str(e)})
            return
        except (ValueError, KeyError) as e:
            _json_response(self, 400, {"error": str(e)})
            return
        _json_response(self, 200, package)

    def _handle_verify(self, body: Any, state: ApiState) -> None:
        if not isinstance(body, dict) or "package" not in body:
            _json_response(self, 400, {"error": "请求必须包含 package"})
            return
        records = body.get("records")
        keys = state.keys if body.get("use_local_keys") else None
        report = verify_package(body["package"], records=records, keys=keys)
        _json_response(self, 200 if report["overall_passed"] else 422, report)


def build_server(host: str = "127.0.0.1", port: int = 8390,
                 base_dir: Path = Path(".mde-data"), quiet: bool = False) -> ThreadingHTTPServer:
    state = ApiState(base_dir)

    class BoundHandler(_Handler):
        pass

    BoundHandler.state = state
    httpd = ThreadingHTTPServer((host, port), BoundHandler)
    httpd.quiet = quiet  # type: ignore[attr-defined]
    httpd.state = state  # type: ignore[attr-defined]
    return httpd


def serve(host: str = "127.0.0.1", port: int = 8390,
          base_dir: Path = Path(".mde-data")) -> None:
    httpd = build_server(host, port, base_dir)
    httpd.serve_forever()
