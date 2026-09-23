"""JSON 解析服务（纯后端，无前端）。

仅使用 Python 标准库。启动方式::

    python -m dl.server [--port 8000]

接口一览（请求/响应均为 JSON，字符位置一律为半开区间 [start,end)）：

GET  /health
    -> {"ok": true, "service": "dlang-incremental-parser"}

POST /parse                      一次性全量解析
    {"source": "...", "include_tokens": false}
    -> {"tree": ..., "errors": [...]}

POST /documents                  创建增量文档
    {"source": "..."}
    -> {"id": 1, "version": 0, "reuse_events": 0, ...解析结果...}

GET  /documents                  列出所有文档
    -> {"documents": [{"id": 1, "version": 3, "length": 42}]}

GET  /documents/{id}             查看当前解析结果

POST /documents/{id}/edits       应用一次或顺序多次编辑（增量解析）
    {"edits": [{"start": 0, "end": 0, "text": "x = 1;"}]}
    -> {"version": 1, "reuse_events": 3, ...新解析结果...}
    （单个编辑也可以写 {"start": 3, "end": 5, "text": "..."}）

DELETE /documents/{id}           删除文档

编辑字段名 ``text``；仅用于插入时可省略（默认空串）。
"""

from __future__ import annotations

import argparse
import json
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import urlsplit

from .diff import result_to_dict
from .incremental import Document, Edit
from .parser import parse


class Store:
    """线程安全的内存文档存储（重启即清空）。"""

    def __init__(self):
        self._lock = threading.Lock()
        self._docs: dict[int, Document] = {}
        self._next_id = 1

    def create(self, source: str) -> tuple[int, Document]:
        with self._lock:
            doc_id = self._next_id
            self._next_id += 1
            doc = Document(source=source)
            self._docs[doc_id] = doc
            return doc_id, doc

    def get(self, doc_id: int) -> Document | None:
        return self._docs.get(doc_id)

    def delete(self, doc_id: int) -> bool:
        with self._lock:
            return self._docs.pop(doc_id, None) is not None

    def list(self):
        with self._lock:
            return [
                {"id": i, "version": d.version, "length": len(d.source)}
                for i, d in sorted(self._docs.items())
            ]


STORE = Store()


def _validate_edits(payload) -> list[Edit]:
    raw = payload.get("edits")
    if raw is None and ("start" in payload):
        raw = [payload]
    if not isinstance(raw, list):
        raise ValueError("edits 必须是数组")
    edits: list[Edit] = []
    for item in raw:
        if not isinstance(item, dict) or "start" not in item or "end" not in item:
            raise ValueError("每个编辑需包含 start 与 end")
        text = item.get("text", "")
        if text is None:
            text = ""
        if not isinstance(text, str):
            raise ValueError("text 必须是字符串")
        edits.append(Edit(int(item["start"]), int(item["end"]), text))
    return edits


class Handler(BaseHTTPRequestHandler):
    server_version = "dlang/1.0"

    def log_message(self, fmt, *args):  # 安静一点：只记一行访问日志到 stderr
        super().log_message(fmt, *args)

    # ---- 工具 ----

    def _send_json(self, status: int, body: dict):
        data = json.dumps(body, ensure_ascii=False, indent=2).encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(data)))
        self.end_headers()
        self.wfile.write(data)

    def _read_json(self) -> dict:
        length = int(self.headers.get("Content-Length") or 0)
        raw = self.rfile.read(length) if length else b"{}"
        try:
            payload = json.loads(raw.decode("utf-8"))
        except (json.JSONDecodeError, UnicodeDecodeError) as exc:
            raise ValueError(f"请求体不是合法 JSON：{exc}")
        if not isinstance(payload, dict):
            raise ValueError("请求体必须是 JSON 对象")
        return payload

    def _bad(self, message: str, status: int = 400):
        self._send_json(status, {"error": message})

    # ---- 路由 ----

    def do_GET(self):
        parts = [p for p in urlsplit(self.path).path.split("/") if p]
        try:
            if parts == ["health"]:
                self._send_json(200, {"ok": True,
                                      "service": "dlang-incremental-parser"})
                return
            if parts == ["documents"]:
                self._send_json(200, {"documents": STORE.list()})
                return
            if len(parts) == 2 and parts[0] == "documents":
                doc = STORE.get(int(parts[1]))
                if doc is None:
                    self._bad("文档不存在", 404)
                    return
                body = {"id": int(parts[1]), "version": doc.version,
                        "reuse_events": doc.reuse_events, "source": doc.source}
                body.update(result_to_dict(doc.result))
                self._send_json(200, body)
                return
            self._bad("未知路径", 404)
        except ValueError:
            self._bad("路径参数非法", 400)

    def do_POST(self):
        parts = [p for p in urlsplit(self.path).path.split("/") if p]
        try:
            payload = self._read_json()
            if parts == ["parse"]:
                source = payload.get("source", "")
                if not isinstance(source, str):
                    self._bad("source 必须是字符串")
                    return
                result = parse(source)
                body = result_to_dict(
                    result,
                    include_tokens=bool(payload.get("include_tokens", False)),
                )
                self._send_json(200, body)
                return
            if parts == ["documents"]:
                source = payload.get("source", "")
                if not isinstance(source, str):
                    self._bad("source 必须是字符串")
                    return
                doc_id, doc = STORE.create(source)
                body = {"id": doc_id, "version": doc.version,
                        "reuse_events": 0, "source": source}
                body.update(result_to_dict(doc.result))
                self._send_json(201, body)
                return
            if len(parts) == 3 and parts[0] == "documents" \
                    and parts[2] == "edits":
                doc = STORE.get(int(parts[1]))
                if doc is None:
                    self._bad("文档不存在", 404)
                    return
                try:
                    edits = _validate_edits(payload)
                except ValueError as exc:
                    self._bad(str(exc))
                    return
                try:
                    doc.apply_edits(edits)
                except ValueError as exc:
                    self._bad(str(exc))
                    return
                body = {"id": int(parts[1]), "version": doc.version,
                        "reuse_events": doc.reuse_events,
                        "source": doc.source}
                body.update(result_to_dict(doc.result))
                self._send_json(200, body)
                return
            self._bad("未知路径", 404)
        except ValueError as exc:
            self._bad(str(exc))

    def do_DELETE(self):
        parts = [p for p in urlsplit(self.path).path.split("/") if p]
        if len(parts) == 2 and parts[0] == "documents":
            try:
                doc_id = int(parts[1])
            except ValueError:
                self._bad("路径参数非法", 400)
                return
            if STORE.delete(doc_id):
                self._send_json(200, {"deleted": doc_id})
            else:
                self._bad("文档不存在", 404)
            return
        self._bad("未知路径", 404)


def serve(host: str = "127.0.0.1", port: int = 8000) -> ThreadingHTTPServer:
    httpd = ThreadingHTTPServer((host, port), Handler)
    return httpd


def main(argv: list[str] | None = None):
    ap = argparse.ArgumentParser(description="dlang 增量解析 JSON 服务")
    ap.add_argument("--host", default="127.0.0.1")
    ap.add_argument("--port", type=int, default=8000)
    args = ap.parse_args(argv)
    httpd = serve(args.host, args.port)
    print(f"dlang 解析服务已启动: http://{args.host}:{args.port}", flush=True)
    try:
        httpd.serve_forever()
    except KeyboardInterrupt:
        pass
    finally:
        httpd.server_close()


if __name__ == "__main__":
    main()
