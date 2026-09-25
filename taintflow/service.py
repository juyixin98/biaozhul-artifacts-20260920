"""对外统一 API 与纯 JSON HTTP 服务。

- 库用法::

      from taintflow import analyze_source
      result = analyze_source(src)

- 服务用法::

      python -m taintflow.cli serve --port 8000
      POST /analyze  {"source": "...", "config": {...}}

响应结构见 README“JSON 服务”。任何词法/语法/语义错误都结构化为
``{"ok": false, "error": {...}}``，服务进程不抛栈。
"""

from __future__ import annotations

import json
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from typing import Any, Dict, Optional

from .version import __version__
from .analyzer import InterproceduralAnalyzer
from .config import AnalysisConfig
from .errors import TaintflowError
from .ir import IRBuilder
from .lexer import Lexer
from .parser import Parser


def analyze_source(
    source: str,
    config: Optional[AnalysisConfig] = None,
    *,
    filename: str = "<input>",
    entry: str = "main",
    include_ir: bool = False,
) -> Dict[str, Any]:
    """分析一段源码，返回可 JSON 序列化的结果字典。

    出错时不抛异常，而是返回 ``{"ok": False, "error": {...}}``（与 HTTP 层一致）；
    ``ValueError``（非法配置）同样结构化。
    """
    cfg = config or AnalysisConfig()
    try:
        tokens = Lexer(source, filename=filename).tokenize()
        program = Parser(tokens).parse_program()
        ir_program = IRBuilder(program, cfg).build()
        analyzer = InterproceduralAnalyzer(ir_program, cfg, entry=entry)
        analyzer.run()
        alerts = analyzer.collect_alerts()

        result: Dict[str, Any] = {
            "ok": True,
            "tool": "taintflow",
            "version": __version__,
            "entry": entry if entry in ir_program.functions else None,
            "summary": {
                "alerts": len(alerts),
                "true_positives": sum(not a.may_be_false_positive for a in alerts),
                "possible_false_positives": sum(a.may_be_false_positive for a in alerts),
                "function_contexts": analyzer.stats.summaries,
                "reanalyses": analyzer.stats.reanalyses,
                "intra_iterations": analyzer.stats.intra_iterations,
                "max_call_depth_seen": analyzer.stats.max_call_depth,
            },
            "alerts": [_alert_dict(a, ir_program) for a in alerts],
            "warnings": _merge_warnings(ir_program, analyzer),
            "config": {
                "sources": sorted(cfg.sources),
                "sinks": sorted(cfg.sinks),
                "sanitizers": sorted(cfg.sanitizers),
                "context_k": cfg.context_k,
                "max_trace": cfg.max_trace,
                "conservative_unknown_calls": cfg.conservative_unknown_calls,
            },
        }
        if not result["entry"]:
            result["summary"]["note"] = (
                "未找到 main()：以全干净参数分析所有函数，仅发现其内部 source->sink"
            )
        if include_ir:
            result["ir"] = _ir_dict(ir_program)
        return result
    except TaintflowError as exc:
        return {
            "ok": False,
            "error": {
                "type": type(exc).__name__,
                "message": getattr(exc, "message", str(exc)),
                "span": str(exc.span) if getattr(exc, "span", None) else None,
            },
        }
    except ValueError as exc:
        return {"ok": False, "error": {"type": "ConfigError", "message": str(exc)}}


def _alert_dict(alert, ir_program) -> Dict[str, Any]:
    fn_name, block_label, instr_index = alert.origin
    source_line = source_col = None
    src_fn = ir_program.functions.get(fn_name)
    if src_fn is not None:
        ins = src_fn.blocks[block_label].instructions[instr_index]
        source_line = ins.span.start.line
        source_col = ins.span.start.col
    return {
        "classification": alert.classification,
        "may_be_false_positive": alert.may_be_false_positive,
        "classification_note": alert.classification_note,
        "source_point": {
            "function": fn_name,
            "block": block_label,
            "instruction": instr_index,
            "line": source_line,
            "col": source_col,
        },
        "sink": {
            "function": alert.sink_fn,
            "name": alert.sink_name,
            "line": alert.sink_line,
            "col": alert.sink_col,
        },
        "context": list(alert.context),
        "path_length": len(alert.trace),
        "path": [s.as_dict() for s in alert.trace],
    }


def _merge_warnings(ir_program, analyzer) -> list:
    # unknown_call 已在 IR 构建阶段记录（含位置），这里直接返回构建期告警。
    return list(ir_program.warnings)


def _ir_dict(ir_program) -> Dict[str, Any]:
    out = {}
    for name, fn in ir_program.functions.items():
        out[name] = {
            "params": fn.params,
            "blocks": [
                {
                    "label": label,
                    "instructions": [ins.as_dict() for ins in fn.blocks[label].instructions],
                    "successors": list(fn.blocks[label].successors),
                }
                for label in fn.order
            ],
        }
    return out


# ---------------- HTTP 服务 ----------------


class _Handler(BaseHTTPRequestHandler):
    server_version = f"Taintflow/{__version__}"

    def log_message(self, fmt, *args):  # 安静日志
        pass

    def _send(self, status: int, body: Dict[str, Any]) -> None:
        data = json.dumps(body, ensure_ascii=False, indent=2).encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(data)))
        self.end_headers()
        self.wfile.write(data)

    def do_GET(self):  # noqa: N802
        if self.path.rstrip("/") in ("/", "/health"):
            self._send(200, {"ok": True, "tool": "taintflow", "version": __version__})
        else:
            self._send(404, {"ok": False, "error": {"type": "NotFound",
                                                    "message": "可用端点: POST /analyze, GET /health"}})

    def do_POST(self):  # noqa: N802
        if self.path.rstrip("/") != "/analyze":
            self._send(404, {"ok": False, "error": {"type": "NotFound",
                                                    "message": "可用端点: POST /analyze"}})
            return
        try:
            length = int(self.headers.get("Content-Length", "0"))
            raw = self.rfile.read(length) if length else b""
            payload = json.loads(raw.decode("utf-8"))
        except (json.JSONDecodeError, UnicodeDecodeError) as exc:
            self._send(400, {"ok": False,
                             "error": {"type": "BadRequest", "message": f"请求不是合法 JSON: {exc}"}})
            return
        if not isinstance(payload, dict) or "source" not in payload:
            self._send(400, {"ok": False, "error": {
                "type": "BadRequest",
                "message": "请求体必须是 JSON 对象且包含字符串字段 'source'"}})
            return
        source = payload.get("source")
        if not isinstance(source, str):
            self._send(400, {"ok": False, "error": {
                "type": "BadRequest", "message": "'source' 必须是字符串"}})
            return
        try:
            cfg = AnalysisConfig.from_dict(payload.get("config"))
        except (ValueError, TypeError) as exc:
            self._send(400, {"ok": False,
                             "error": {"type": "ConfigError", "message": str(exc)}})
            return
        entry = payload.get("entry", "main")
        include_ir = bool(payload.get("include_ir", False))
        result = analyze_source(source, cfg, entry=entry, include_ir=include_ir)
        self._send(200 if result.get("ok") else 400, result)


def create_server(host: str = "127.0.0.1", port: int = 8000) -> ThreadingHTTPServer:
    return ThreadingHTTPServer((host, port), _Handler)


def serve(host: str = "127.0.0.1", port: int = 8000) -> None:  # pragma: no cover
    httpd = create_server(host, port)
    print(f"taintflow JSON 服务监听 http://{host}:{port}  (POST /analyze, GET /health)")
    try:
        httpd.serve_forever()
    except KeyboardInterrupt:
        pass
    finally:
        httpd.server_close()
