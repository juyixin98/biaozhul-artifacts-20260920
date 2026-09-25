"""本地 HTTP 服务（标准库 http.server，无第三方框架）。

端点：

* ``GET  /health``                 —— 健康检查
* ``POST /v1/compile``             —— 仅编译规则，返回规范化规则与校验结果
* ``POST /v1/mask``                —— {rules, document, key_file?} 一步脱敏
* ``POST /v1/mask-compiled``       —— {compiled, document} 复用已编译规则

安全：
* 请求体大小硬上限（默认 1 MiB）；
* 错误响应只含错误类型、规则 id、路径等 schema 信息，**绝不回显文档值**；
* 日志经过 SecretScrubFilter，且服务层从不记录请求体。
"""

from __future__ import annotations

import json
from dataclasses import dataclass
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Any

from .crypto import CryptoProvider
from .engine import apply_rules
from .errors import DMSError, InvalidPayloadError
from .logging_utils import get_logger
from .rules import compile_rules

MAX_BODY_BYTES = 1 * 1024 * 1024
logger = get_logger("dms.server")


@dataclass
class CompiledEnvelope:
    """线上格式：编译结果的可序列化表示。"""

    rules: list[dict[str, Any]]


def serialize_compiled(compiled) -> dict[str, Any]:
    from .paths import format_segments  # 局部导入避免循环

    return {
        "version": 1,
        "rules": [
            {
                "id": r.id,
                "action": r.action,
                "path": format_segments(r.segments),
                "priority": r.priority,
                "require_match": r.require_match,
                "options": r.options,
            }
            for r in compiled.rules
        ],
    }


def _read_json(handler: BaseHTTPRequestHandler) -> Any:
    length = int(handler.headers.get("Content-Length", "0"))
    if length <= 0:
        raise InvalidPayloadError("请求体为空")
    if length > MAX_BODY_BYTES:
        raise InvalidPayloadError("请求体超过 1 MiB 上限")
    raw = handler.rfile.read(length)
    try:
        return json.loads(raw.decode("utf-8"))
    except (UnicodeDecodeError, json.JSONDecodeError):
        raise InvalidPayloadError("请求体不是合法 UTF-8 JSON")


def _send_json(handler: BaseHTTPRequestHandler, status: int, payload: Any) -> None:
    body = json.dumps(payload, ensure_ascii=False, allow_nan=False).encode("utf-8")
    handler.send_response(status)
    handler.send_header("Content-Type", "application/json; charset=utf-8")
    handler.send_header("Content-Length", str(len(body)))
    handler.end_headers()
    handler.wfile.write(body)


def _error_payload(exc: DMSError) -> dict[str, Any]:
    return {"ok": False, "error": {"code": exc.code, "message": str(exc),
                                   "details": exc.details}}


def _mask_from_payload(payload: Any) -> tuple[Any, Any]:
    if not isinstance(payload, dict):
        raise InvalidPayloadError("请求必须是 JSON 对象")
    if "rules" not in payload:
        raise InvalidPayloadError("缺少 rules 字段")
    if "document" not in payload:
        raise InvalidPayloadError("缺少 document 字段")
    return payload["rules"], payload["document"]


def make_handler(key_path: Path) -> type[BaseHTTPRequestHandler]:
    class Handler(BaseHTTPRequestHandler):
        server_version = "DMS/0.1"

        def log_message(self, fmt: str, *args: Any) -> None:
            # 覆盖默认访问日志：不记录 query/body，仅记录方法与路径模板
            logger.info("%s %s", self.command, self.path.split("?")[0])

        def _respond_error(self, exc: Exception) -> None:
            if isinstance(exc, DMSError):
                logger.warning("请求失败 code=%s details=%s", exc.code,
                               list(exc.details.keys()))
                _send_json(self, _status_for(exc), _error_payload(exc))
            else:
                logger.exception("未预期错误类型=%s", type(exc).__name__)
                _send_json(self, 500, {"ok": False,
                                       "error": {"code": "internal_error",
                                                 "message": "内部错误",
                                                 "details": {}}})

        def do_GET(self) -> None:  # noqa: N802
            if self.path.split("?")[0] == "/health":
                _send_json(self, 200, {"ok": True, "service": "dms", "version": "0.1.0"})
            else:
                _send_json(self, 404, {"ok": False,
                                       "error": {"code": "not_found",
                                                 "message": "未知端点", "details": {}}})

        def do_POST(self) -> None:  # noqa: N802
            path = self.path.split("?")[0]
            try:
                payload = _read_json(self)
                if path == "/v1/compile":
                    compiled = compile_rules(payload)
                    _send_json(self, 200, {"ok": True,
                                           "compiled": serialize_compiled(compiled)})
                    return
                if path == "/v1/mask":
                    rules_spec, document = _mask_from_payload(payload)
                    compiled = compile_rules(rules_spec)
                    crypto = None
                    if compiled.needs_crypto:
                        crypto = CryptoProvider.load_or_create(
                            Path(payload.get("key_file") or key_path)
                        )
                    result = apply_rules(compiled, document, crypto)
                    _send_json(self, 200, {"ok": True,
                                           "document": result.document,
                                           "stats": result.stats})
                    return
                if path == "/v1/mask-compiled":
                    if not isinstance(payload, dict) or "compiled" not in payload:
                        raise InvalidPayloadError("缺少 compiled 字段")
                    # 重新走编译器校验，杜绝客户端绕过默认拒绝
                    compiled = compile_rules(payload["compiled"])
                    crypto = None
                    if compiled.needs_crypto:
                        crypto = CryptoProvider.load_or_create(
                            Path(payload.get("key_file") or key_path)
                        )
                    result = apply_rules(compiled, payload.get("document"), crypto)
                    _send_json(self, 200, {"ok": True,
                                           "document": result.document,
                                           "stats": result.stats})
                    return
                _send_json(self, 404, {"ok": False,
                                       "error": {"code": "not_found",
                                                 "message": "未知端点", "details": {}}})
            except Exception as exc:  # noqa: BLE001 - 统一错误出口
                self._respond_error(exc)

    return Handler


def _status_for(exc: DMSError) -> int:
    if exc.code in ("invalid_payload",):
        return 400
    if exc.code in ("rule_compile_error", "unknown_rule", "rule_conflict",
                    "missing_field", "transform_error"):
        return 422
    return 500


def serve(host: str = "127.0.0.1", port: int = 8080,
          key_path: Path | str = ".secrets/dms-test-key.json") -> None:
    httpd = ThreadingHTTPServer((host, port), make_handler(Path(key_path)))
    logger.info("DMS 服务监听 http://%s:%s（仅本地绑定）", host, port)
    try:
        httpd.serve_forever()
    except KeyboardInterrupt:
        logger.info("收到中断信号，关闭服务")
    finally:
        httpd.server_close()
