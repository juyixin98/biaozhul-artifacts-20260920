"""本地安全数据处理服务 (HTTP) —— 仅监听回环地址, 离线运行。

设计边界:
  - 默认且强制绑定 127.0.0.1 (除非显式 --host, 但帮助文本会警告);
  - 不连接任何外部服务, 不使用任何生产账户;
  - /v1/sign 仅在服务启动时配置了本地签名私钥才可用, 否则 403;
  - /v1/verify 需要调用方提供可信公钥 (PEM, base64) —— 信任锚点
    由每次请求显式给出, 服务不内置任何隐式信任;
  - 请求体内联提供文件内容 (path -> base64), 服务在临时目录中
    物化制品, 处理完立即清理, 避免共享路径上的竞争。

接口:
  GET  /healthz
  POST /v1/sign    {files: {path: {"b64": ...}}, entrypoint?: str}
  POST /v1/verify  {envelope: {...}|b64|str, trusted_public_keys: [pem-b64...],
                    files?: {...}}
"""

from __future__ import annotations

import argparse
import base64
import json
import tempfile
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

from . import canonical
from .errors import SAMError
from .keys import TrustStore, load_private_key
from .sign import sign_artifact
from .verify import verify as verify_report

_MAX_BODY = 32 * 1024 * 1024  # 32 MiB, 防止本地服务被误塞超大请求


def _b64_decode_field(value: str, field: str) -> bytes:
    if not isinstance(value, str):
        raise SAMError(f"{field} 必须是 base64 字符串")
    try:
        return base64.b64decode(value, validate=True)
    except Exception as exc:
        raise SAMError(f"{field} 不是合法 base64") from exc


def _materialize_files(tmpdir: str, files: dict) -> None:
    """把请求里的 {path: {b64/text}} 物化到临时制品目录, 全程路径安全。"""
    from .safepaths import resolve_within

    if not isinstance(files, dict):
        raise SAMError("files 必须是 {path: {b64|text}} 对象")
    for rel, spec in files.items():
        target = resolve_within(tmpdir, rel)  # 词法 + 解析双重逃逸检查
        if isinstance(spec, dict) and "b64" in spec:
            data = _b64_decode_field(spec["b64"], f"files.{rel}.b64")
        elif isinstance(spec, dict) and "text" in spec:
            if not isinstance(spec["text"], str):
                raise SAMError(f"files.{rel}.text 必须是字符串")
            data = spec["text"].encode("utf-8")
        else:
            raise SAMError(f"files.{rel} 必须含 b64 或 text 字段")
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_bytes(data)


class _State:
    def __init__(self, signing_private_key_path: str | None) -> None:
        self.lock = threading.Lock()
        self.signing_key = None
        if signing_private_key_path:
            self.signing_key = load_private_key(signing_private_key_path)


def make_handler(state: _State) -> type[BaseHTTPRequestHandler]:
    class Handler(BaseHTTPRequestHandler):
        server_version = "SAM/1.0"

        def log_message(self, fmt, *args):  # 安静一点, 走 stderr 单行
            import sys

            sys.stderr.write("[sam-service] " + fmt % args + "\n")

        # ---- 工具 ----
        def _send_json(self, status: int, payload: dict) -> None:
            body = json.dumps(payload, ensure_ascii=False).encode("utf-8")
            self.send_response(status)
            self.send_header("Content-Type", "application/json; charset=utf-8")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)

        def _read_json(self) -> dict:
            length = int(self.headers.get("Content-Length", "0"))
            if length <= 0:
                raise SAMError("请求体为空")
            if length > _MAX_BODY:
                raise SAMError("请求体超过 32 MiB 上限")
            raw = self.rfile.read(length)
            try:
                data = json.loads(raw.decode("utf-8"))
            except (UnicodeDecodeError, json.JSONDecodeError) as exc:
                raise SAMError(f"请求体不是合法 JSON: {exc}") from exc
            if not isinstance(data, dict):
                raise SAMError("请求体顶层必须是 JSON 对象")
            return data

        # ---- 路由 ----
        def do_GET(self):
            if self.path.split("?")[0] == "/healthz":
                self._send_json(
                    200,
                    {
                        "ok": True,
                        "signing_enabled": state.signing_key is not None,
                    },
                )
            else:
                self._send_json(404, {"ok": False, "error": "not found"})

        def do_POST(self):
            path = self.path.split("?")[0]
            try:
                data = self._read_json()
                if path == "/v1/sign":
                    self._handle_sign(data)
                elif path == "/v1/verify":
                    self._handle_verify(data)
                else:
                    self._send_json(404, {"ok": False, "error": "not found"})
            except SAMError as exc:
                self._send_json(400, {"ok": False, "error": str(exc)})
            except Exception as exc:  # 服务不能因单个请求崩溃
                self._send_json(500, {"ok": False, "error": f"内部错误: {exc}"})

        # ---- /v1/sign ----
        def _handle_sign(self, data: dict) -> None:
            if state.signing_key is None:
                self._send_json(
                    403,
                    {
                        "ok": False,
                        "error": "服务未配置签名私钥, /v1/sign 已禁用",
                    },
                )
                return
            files = data.get("files")
            entrypoint = data.get("entrypoint")
            if entrypoint is not None and not isinstance(entrypoint, str):
                raise SAMError("entrypoint 必须是字符串")

            with state.lock, tempfile.TemporaryDirectory(prefix="sam-sign-") as td:
                _materialize_files(td, files if files is not None else {})
                envelope = sign_artifact(
                    td, state.signing_key, entrypoint=entrypoint
                )
            self._send_json(200, {"ok": True, "envelope": envelope})

        # ---- /v1/verify ----
        def _handle_verify(self, data: dict) -> None:
            env_field = data.get("envelope")
            if isinstance(env_field, dict):
                envelope_bytes = canonical.pretty_json(env_field)
            elif isinstance(env_field, str):
                # 可以是内嵌 JSON 文本, 也可以是 base64
                stripped = env_field.lstrip()
                if stripped.startswith("{"):
                    envelope_bytes = env_field.encode("utf-8")
                else:
                    envelope_bytes = _b64_decode_field(env_field, "envelope")
            else:
                raise SAMError("envelope 必须是对象、JSON 文本或 base64 字符串")

            trusted = data.get("trusted_public_keys")
            if not isinstance(trusted, list) or not trusted:
                raise SAMError("trusted_public_keys 必须是非空 PEM(base64) 数组")

            with tempfile.TemporaryDirectory(prefix="sam-verify-") as td:
                tdir = Path(td) / "trust"
                tdir.mkdir()
                for i, pk_b64 in enumerate(trusted):
                    pem = _b64_decode_field(pk_b64, f"trusted_public_keys[{i}]")
                    (tdir / f"key{i}.pem").write_bytes(pem)
                store = TrustStore.load(tdir)

                files = data.get("files")
                art_dir = Path(td) / "artifact"
                art_dir.mkdir()
                if files is not None:
                    _materialize_files(str(art_dir), files)

                report = verify_report(
                    artifact_root=art_dir,
                    envelope_bytes=envelope_bytes,
                    trust_store=store,
                    strict_extra=True,
                )
            # 业务结果恒为 200; ok=false 由 body 表达
            self._send_json(200, {"ok": True, "verify": report.to_dict()})

    return Handler


def serve(host: str, port: int, signing_private_key_path: str | None = None) -> None:
    state = _State(signing_private_key_path)
    httpd = ThreadingHTTPServer((host, port), make_handler(state))
    bound = httpd.server_address
    print(
        f"SAM 本地服务监听 http://{bound[0]}:{bound[1]} "
        f"(签名端点: {'启用' if state.signing_key else '禁用'})"
    )
    try:
        httpd.serve_forever()
    except KeyboardInterrupt:
        pass
    finally:
        httpd.server_close()


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="SAM 本地 HTTP 服务 (仅回环)")
    parser.add_argument("--host", default="127.0.0.1")
    parser.add_argument("--port", type=int, default=8088)
    parser.add_argument(
        "--signing-key", help="可选: 本地 Ed25519 私钥 PEM, 配置后启用 /v1/sign"
    )
    args = parser.parse_args(argv)
    if args.host not in ("127.0.0.1", "localhost", "::1"):
        print(
            f"警告: 绑定到非回环地址 {args.host}, 该服务无鉴权, 请确认网络可信",
            flush=True,
        )
    serve(args.host, args.port, args.signing_key)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
