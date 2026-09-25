"""本地训练服务：基于标准库 http.server 的 JSON API（无外部依赖）。

端点：
  GET  /health            存活检查
  POST /train             训练 N 步；body 可带 config 字段、steps、resume
  GET  /status            当前训练器状态
  GET  /checkpoints       列出检查点及完整性校验结果
  POST /verify            校验指定检查点 {"path": "..."}

训练是同步执行的（数据规模小，单步毫秒级），进程重启后
用相同的 checkpoint_dir 再调 /train {"resume": true} 即断点续训。
"""

from __future__ import annotations

import json
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

from .checkpoint import CorruptCheckpointError, list_checkpoints, verify_checkpoint
from .config import TrainConfig
from .trainer import Trainer


class _State:
    """服务级状态：当前训练器实例（若有）。"""

    def __init__(self):
        self.trainer: Trainer | None = None
        self.last_config: dict | None = None


def _json_response(handler: BaseHTTPRequestHandler, code: int, payload: dict) -> None:
    body = json.dumps(payload, ensure_ascii=False).encode("utf-8")
    handler.send_response(code)
    handler.send_header("Content-Type", "application/json; charset=utf-8")
    handler.send_header("Content-Length", str(len(body)))
    handler.end_headers()
    handler.wfile.write(body)


def _read_json(handler: BaseHTTPRequestHandler) -> dict:
    length = int(handler.headers.get("Content-Length") or 0)
    if length == 0:
        return {}
    raw = handler.rfile.read(length)
    try:
        data = json.loads(raw.decode("utf-8"))
    except (UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise ValueError(f"请求体不是合法 JSON: {exc}") from exc
    if not isinstance(data, dict):
        raise ValueError("请求体必须是 JSON 对象")
    return data


def make_handler(state: _State):
    class Handler(BaseHTTPRequestHandler):
        def log_message(self, fmt, *args):  # 静默访问日志
            pass

        def _error(self, code: int, message: str) -> None:
            _json_response(self, code, {"ok": False, "error": message})

        def do_GET(self):
            if self.path == "/health":
                _json_response(self, 200, {"ok": True, "status": "up"})
            elif self.path == "/status":
                if state.trainer is None:
                    _json_response(self, 200, {"ok": True, "trainer": None})
                else:
                    _json_response(self, 200, {"ok": True, "trainer": state.trainer.status()})
            elif self.path == "/checkpoints":
                self._list_checkpoints()
            else:
                self._error(404, f"未知路径: {self.path}")

        def do_POST(self):
            if self.path == "/train":
                self._train()
            elif self.path == "/verify":
                self._verify()
            else:
                self._error(404, f"未知路径: {self.path}")

        def _train(self):
            try:
                body = _read_json(self)
                cfg_dict = dict(state.last_config or {})
                cfg_dict.update(body.get("config", {}))
                config = TrainConfig.from_dict(cfg_dict)
                steps = int(body.get("steps", config.steps_per_epoch))
                resume = bool(body.get("resume", True))
                if steps <= 0:
                    raise ValueError("steps 必须为正")
            except ValueError as exc:
                self._error(400, str(exc))
                return
            try:
                trainer = Trainer(config, resume=resume)
                records = trainer.train(steps)
                trainer.save()
            except (ValueError, CorruptCheckpointError) as exc:
                self._error(409, str(exc))
                return
            state.trainer = trainer
            state.last_config = config.to_dict()
            _json_response(
                self,
                200,
                {
                    "ok": True,
                    "steps_run": len(records),
                    "resumed_from": trainer.resumed_from,
                    "status": trainer.status(),
                    "last_losses": [r["loss"] for r in records[-3:]],
                },
            )

        def _list_checkpoints(self):
            ckpt_dir = (
                state.trainer.config.checkpoint_dir
                if state.trainer is not None
                else (state.last_config or {}).get("checkpoint_dir", "checkpoints")
            )
            items = []
            for path in list_checkpoints(ckpt_dir):
                try:
                    verify_checkpoint(path)
                    items.append({"path": str(path), "valid": True})
                except CorruptCheckpointError as exc:
                    items.append({"path": str(path), "valid": False, "error": str(exc)})
            _json_response(self, 200, {"ok": True, "checkpoints": items})

        def _verify(self):
            try:
                body = _read_json(self)
                path = body["path"]
            except (ValueError, KeyError) as exc:
                self._error(400, f"缺少合法的 path 字段: {exc}")
                return
            try:
                verify_checkpoint(path)
            except CorruptCheckpointError as exc:
                _json_response(self, 200, {"ok": True, "valid": False, "error": str(exc)})
                return
            _json_response(self, 200, {"ok": True, "valid": True})

    return Handler


def serve(host: str = "127.0.0.1", port: int = 8377) -> ThreadingHTTPServer:
    server = ThreadingHTTPServer((host, port), make_handler(_State()))
    print(f"训练服务已启动: http://{host}:{port} (Ctrl+C 停止)")
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        pass
    finally:
        server.server_close()
    return server


if __name__ == "__main__":
    serve()
