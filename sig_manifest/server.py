"""仅监听 loopback 的本地 HTTP 服务（标准库实现，无第三方 Web 框架）。

端点（均为 POST JSON，除 healthz 外）::

    GET  /healthz           存活探针
    POST /sign              {"artifact_root","name","private_key_pem",
                              "password"?,"entrypoint"?} → 清单信封
    POST /verify            {"artifact_root","manifest": <信封对象或文本>}
                             → {ok,errors,warnings,verified_key_ids}
    POST /run               同 verify，外加 "dry_run" / "extra_args" /
                             "timeout" → 验证通过才执行 entrypoint

安全约束：
- 只绑定 127.0.0.1；Host 头不是本机地址则拒绝，避免通过域名重放访问；
- 请求体上限 16 MiB，超时 30 秒；
- 本服务不提供读取/回传私钥文件的能力，签名私钥由请求体直接提交 PEM
  （调用方负责本地管理），不落盘。
"""

from __future__ import annotations

import json
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Any
from urllib.parse import urlparse

from cryptography.hazmat.primitives.serialization import load_pem_private_key

from .errors import HTTPError, ManifestError
from .keys import TrustStore
from .manifest import sign_artifact
from .runner import verify_then_run
from .verify import verify_artifact

MAX_BODY_BYTES = 16 * 1024 * 1024
# 允许的 Host；loopback 的常见写法。
_LOOPBACK_HOSTS = {"127.0.0.1", "localhost", "::1", "[::1]"}


def _problem(code: str, message: str) -> dict[str, Any]:
    return {"code": code, "message": message}


class _Handler(BaseHTTPRequestHandler):
    server_version = "SigManifest/1.0"
    timeout = 30

    # --- 基础工具 --------------------------------------------------------

    def _send_json(self, status: int, payload: dict[str, Any]) -> None:
        body = json.dumps(payload, ensure_ascii=False).encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", "no-store")
        self.end_headers()
        self.wfile.write(body)

    def _read_json(self) -> dict[str, Any]:
        try:
            length = int(self.headers.get("Content-Length", "0"))
        except ValueError:
            raise HTTPError("Content-Length 非法")
        if length <= 0:
            raise HTTPError("请求体为空")
        if length > MAX_BODY_BYTES:
            raise HTTPError(f"请求体超过 {MAX_BODY_BYTES} 字节上限")
        raw = self.rfile.read(length)
        if len(raw) != length:
            raise HTTPError("请求体读取不完整")
        try:
            data = json.loads(raw.decode("utf-8"))
        except (UnicodeDecodeError, json.JSONDecodeError) as exc:
            raise HTTPError(f"请求体不是合法 JSON: {exc}") from exc
        if not isinstance(data, dict):
            raise HTTPError("请求体顶层必须是 JSON 对象")
        return data

    def _check_host(self) -> None:
        host = self.headers.get("Host", "")
        parsed = host.split(":")[0]
        if parsed not in _LOOPBACK_HOSTS:
            raise HTTPError(f"拒绝非 loopback Host: {host!r}")

    def _trust_store(self) -> TrustStore:
        path = self.server.trust_store_path  # type: ignore[attr-defined]
        return TrustStore.load(path)

    # --- 路由 ------------------------------------------------------------

    def do_GET(self) -> None:  # noqa: N802 - http.server 命名要求
        try:
            self._check_host()
            if urlparse(self.path).path == "/healthz":
                self._send_json(200, {"ok": True, "service": "sig-manifest"})
            else:
                self._send_json(404, {"ok": False, "error": _problem("not_found", "未知路径")})
        except ManifestError as exc:
            self._send_json(400, {"ok": False, "error": _problem(exc.error_code, str(exc))})

    def do_POST(self) -> None:  # noqa: N802
        try:
            self._check_host()
            route = urlparse(self.path).path
            if route == "/sign":
                self._handle_sign()
            elif route == "/verify":
                self._handle_verify()
            elif route == "/run":
                self._handle_run()
            else:
                self._send_json(404, {"ok": False,
                                     "error": _problem("not_found", f"未知路径: {route}")})
        except ManifestError as exc:
            status = 400
            if exc.error_code in ("unknown_key", "bad_signature", "digest_mismatch",
                                  "unsafe_path", "file_error"):
                status = 422
            self._send_json(status, {"ok": False, "error": _problem(exc.error_code, str(exc))})
        except Exception as exc:  # noqa: BLE001 - 服务不能因单请求异常挂掉
            self._send_json(500, {"ok": False,
                                  "error": _problem("internal_error", str(exc))})

    # --- 业务处理 --------------------------------------------------------

    @staticmethod
    def _require(data: dict[str, Any], key: str) -> Any:
        if key not in data:
            raise HTTPError(f"缺少必填字段: {key}")
        return data[key]

    def _handle_sign(self) -> None:
        data = self._read_json()
        root = self._require(data, "artifact_root")
        name = self._require(data, "artifact_name")
        pem = self._require(data, "private_key_pem")
        if not isinstance(pem, str):
            raise HTTPError("private_key_pem 必须是 PEM 字符串")
        password = data.get("password")
        if password is not None and not isinstance(password, str):
            raise HTTPError("password 必须是字符串")
        entrypoint = data.get("entrypoint")
        if entrypoint is not None:
            if not isinstance(entrypoint, list) or not all(isinstance(x, str) for x in entrypoint):
                raise HTTPError("entrypoint 必须是字符串数组")
        if not Path(root).is_dir():
            raise HTTPError(f"artifact_root 不存在或不是目录: {root}")

        try:
            private_key = load_pem_private_key(
                pem.encode("utf-8"),
                password=password.encode("utf-8") if password else None,
            )
        except Exception as exc:  # noqa: BLE001
            raise HTTPError(f"私钥加载失败（口令错误或 PEM 损坏）: {exc}") from exc

        envelope = sign_artifact(root, name, private_key, entrypoint=entrypoint)
        self._send_json(200, {"ok": True, "manifest": envelope})

    def _handle_verify(self) -> None:
        data = self._read_json()
        root = self._require(data, "artifact_root")
        manifest = self._require(data, "manifest")
        allow_extra = bool(data.get("allow_extra_files", False))
        store = self._trust_store()
        # manifest 允许是对象（无法检测重复键）或原始文本（推荐）。
        if isinstance(manifest, str):
            manifest = manifest.encode("utf-8")
        report = verify_artifact(root, manifest, store, allow_extra_files=allow_extra)
        self._send_json(200 if report.ok else 422, {
            "ok": report.ok,
            "errors": [vars(p) for p in report.errors],
            "warnings": [vars(p) for p in report.warnings],
            "verified_key_ids": report.verified_key_ids,
        })

    def _handle_run(self) -> None:
        data = self._read_json()
        root = self._require(data, "artifact_root")
        manifest = self._require(data, "manifest")
        store = self._trust_store()
        if isinstance(manifest, str):
            manifest = manifest.encode("utf-8")
        dry_run = bool(data.get("dry_run", False))
        extra = data.get("extra_args") or []
        if not isinstance(extra, list) or not all(isinstance(x, str) for x in extra):
            raise HTTPError("extra_args 必须是字符串数组")
        timeout = data.get("timeout")
        if timeout is not None and not isinstance(timeout, (int, float)):
            raise HTTPError("timeout 必须是数字")
        env_extra = data.get("env")
        if env_extra is not None and not isinstance(env_extra, dict):
            raise HTTPError("env 必须是对象")
        result = verify_then_run(
            root,
            manifest,
            store,
            extra_args=extra,
            env_extra=env_extra,
            timeout=float(timeout) if timeout is not None else None,
            dry_run=dry_run,
        )
        if dry_run:
            self._send_json(200, {
                "ok": True,
                "dry_run": True,
                "argv": result.argv,
                "verified_key_ids": result.verified_key_ids,
            })
        else:
            self._send_json(200, {
                "ok": True,
                "returncode": result.returncode,
                "verified_key_ids": result.verified_key_ids,
            })

    # 安静一点的日志（默认打到 stderr，测试时会被线程吞掉或重定向）。
    def log_message(self, fmt: str, *args: Any) -> None:  # noqa: A003
        if self.server.verbose:  # type: ignore[attr-defined]
            super().log_message(fmt, *args)


def create_server(
    *,
    host: str = "127.0.0.1",
    port: int = 8080,
    trust_store_path: str,
    verbose: bool = True,
) -> ThreadingHTTPServer:
    if host not in ("127.0.0.1", "::1", "localhost"):
        raise HTTPError("本地服务只允许绑定 loopback 地址")
    httpd = ThreadingHTTPServer((host, port), _Handler)
    httpd.trust_store_path = trust_store_path  # type: ignore[attr-defined]
    httpd.verbose = verbose  # type: ignore[attr-defined]
    httpd.daemon_threads = True
    return httpd


def serve_in_thread(httpd: ThreadingHTTPServer) -> threading.Thread:
    """测试辅助：后台线程启动服务。"""
    thread = threading.Thread(target=httpd.serve_forever, daemon=True)
    thread.start()
    return thread
