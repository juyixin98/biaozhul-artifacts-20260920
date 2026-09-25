"""本地 HTTP 服务:POST /evaluate 计算校准指标,GET /health 健康检查。

仅用标准库 http.server,不引入 Web 框架,保持依赖最小。
"""

from __future__ import annotations

import json
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

from calibration_eval.metrics import DEFAULT_N_BINS, evaluate

MAX_BODY_BYTES = 10 * 1024 * 1024  # 10 MB,防止超大请求体


def _json_response(handler: BaseHTTPRequestHandler, status: int, payload: dict) -> None:
    body = json.dumps(payload, ensure_ascii=False).encode("utf-8")
    handler.send_response(status)
    handler.send_header("Content-Type", "application/json; charset=utf-8")
    handler.send_header("Content-Length", str(len(body)))
    handler.end_headers()
    handler.wfile.write(body)


class CalibrationHandler(BaseHTTPRequestHandler):
    server_version = "CalibrationEval/0.1"

    def do_GET(self) -> None:  # noqa: N802 (http.server 约定)
        if self.path == "/health":
            _json_response(self, 200, {"status": "ok"})
        else:
            _json_response(self, 404, {"error": f"未知路径: {self.path}"})

    def do_POST(self) -> None:  # noqa: N802
        if self.path != "/evaluate":
            _json_response(self, 404, {"error": f"未知路径: {self.path}"})
            return

        try:
            length = int(self.headers.get("Content-Length") or 0)
        except ValueError:
            _json_response(self, 400, {"error": "Content-Length 非法"})
            return
        if length <= 0 or length > MAX_BODY_BYTES:
            _json_response(self, 400, {"error": "请求体为空或超过 10 MB 限制"})
            return

        try:
            payload = json.loads(self.rfile.read(length))
        except json.JSONDecodeError as exc:
            _json_response(self, 400, {"error": f"JSON 解析失败: {exc}"})
            return

        if not isinstance(payload, dict):
            _json_response(self, 400, {"error": "请求体必须是 JSON 对象"})
            return
        if "y_true" not in payload or "y_prob" not in payload:
            _json_response(self, 400, {"error": "缺少必需字段 y_true / y_prob"})
            return

        try:
            result = evaluate(
                y_true=payload["y_true"],
                y_prob=payload["y_prob"],
                sample_weight=payload.get("sample_weight"),
                n_bins=payload.get("n_bins", DEFAULT_N_BINS),
                endpoint_strategy=payload.get("endpoint_strategy", "clip"),
                epsilon=payload.get("epsilon", 1e-15),
            )
        except (ValueError, TypeError) as exc:
            _json_response(self, 400, {"error": str(exc)})
            return

        _json_response(self, 200, result)

    def log_message(self, format: str, *args: object) -> None:
        # 静默访问日志,避免污染测试输出;需要排障时可去掉此覆盖。
        return


def create_server(host: str = "127.0.0.1", port: int = 8000) -> ThreadingHTTPServer:
    return ThreadingHTTPServer((host, port), CalibrationHandler)


def main() -> None:
    server = create_server()
    host, port = server.server_address
    print(f"校准评估服务已启动: http://{host}:{port}  (POST /evaluate, GET /health)")
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        print("\n服务已停止")


if __name__ == "__main__":
    main()
