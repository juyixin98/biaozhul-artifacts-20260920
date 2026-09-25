"""Minimal JSON HTTP service around the Mini parser.

Pure backend, Python standard library only.  Run with::

    python server.py --port 8000

Endpoints
---------

POST /parse
    Body: ``{"text": "<source>"}``
    One-shot full parse.  Returns tree + diagnostics.

POST /documents
    Body: ``{"text": "<source>"}``
    Create an editable document.  Returns ``id`` plus the initial parse.

GET /documents/<id>
    Current text, tree and diagnostics of a document.

POST /documents/<id>/edit
    Body: ``{"start": int, "delete": int, "insert": "<text>"}``
    Replace ``text[start:start+delete]`` with ``insert`` and incrementally
    reparse.  The response includes ``stats`` with the number of
    declarations that were reused vs freshly parsed.

All tree nodes carry source spans as half-open character offsets plus
0-based line/column pairs.
"""
from __future__ import annotations

import argparse
import json
import threading
import uuid
from http import HTTPStatus
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

from minilang import Document, EditError, result_to_dict


class Store:
    """Thread-safe in-memory document store."""

    def __init__(self) -> None:
        self._docs = {}
        self._lock = threading.Lock()

    def create(self, text: str) -> tuple:
        doc_id = uuid.uuid4().hex[:12]
        with self._lock:
            self._docs[doc_id] = Document(text)
        return doc_id, self._docs[doc_id]

    def get(self, doc_id: str):
        return self._docs.get(doc_id)


STORE = Store()


class Handler(BaseHTTPRequestHandler):
    server_version = "MiniParser/1.0"

    # -- helpers ---------------------------------------------------------------

    def _send_json(self, payload: dict, status: HTTPStatus = HTTPStatus.OK) -> None:
        body = json.dumps(payload, ensure_ascii=False).encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def _read_json(self) -> dict:
        length = int(self.headers.get("Content-Length", 0))
        raw = self.rfile.read(length) if length else b"{}"
        return json.loads(raw.decode("utf-8"))

    def log_message(self, fmt, *args):  # quieter logging
        if self.server.verbose:  # type: ignore[attr-defined]
            super().log_message(fmt, *args)

    # -- routing ---------------------------------------------------------------

    def do_GET(self) -> None:
        parts = self.path.strip("/").split("/")
        if len(parts) == 2 and parts[0] == "documents":
            doc = STORE.get(parts[1])
            if doc is None:
                self._send_json({"error": "document not found"},
                                HTTPStatus.NOT_FOUND)
                return
            payload = {"id": parts[1], "text": doc.text}
            payload.update(result_to_dict(doc.result))
            self._send_json(payload)
            return
        self._send_json({"error": "not found"}, HTTPStatus.NOT_FOUND)

    def do_POST(self) -> None:
        parts = self.path.strip("/").split("/")
        try:
            data = self._read_json()
        except (json.JSONDecodeError, UnicodeDecodeError) as exc:
            self._send_json({"error": f"invalid JSON body: {exc}"},
                            HTTPStatus.BAD_REQUEST)
            return

        if parts == ["parse"]:
            text = data.get("text", "")
            if not isinstance(text, str):
                self._send_json({"error": "'text' must be a string"},
                                HTTPStatus.BAD_REQUEST)
                return
            self._send_json(result_to_dict(Document(text).result))
            return

        if parts == ["documents"]:
            text = data.get("text", "")
            if not isinstance(text, str):
                self._send_json({"error": "'text' must be a string"},
                                HTTPStatus.BAD_REQUEST)
                return
            doc_id, doc = STORE.create(text)
            payload = {"id": doc_id, "text": doc.text}
            payload.update(result_to_dict(doc.result))
            self._send_json(payload, HTTPStatus.CREATED)
            return

        if len(parts) == 3 and parts[0] == "documents" and parts[2] == "edit":
            doc = STORE.get(parts[1])
            if doc is None:
                self._send_json({"error": "document not found"},
                                HTTPStatus.NOT_FOUND)
                return
            try:
                start = int(data["start"])
                delete = int(data.get("delete", 0))
                insert = data.get("insert", "")
            except (KeyError, TypeError, ValueError):
                self._send_json(
                    {"error": "need integer 'start'/'delete' and string 'insert'"},
                    HTTPStatus.BAD_REQUEST,
                )
                return
            if not isinstance(insert, str):
                self._send_json({"error": "'insert' must be a string"},
                                HTTPStatus.BAD_REQUEST)
                return
            try:
                result = doc.apply_edit(start, delete, insert)
            except EditError as exc:
                self._send_json({"error": str(exc)}, HTTPStatus.BAD_REQUEST)
                return
            payload = {"id": parts[1], "text": doc.text}
            payload.update(result_to_dict(result))
            self._send_json(payload)
            return

        self._send_json({"error": "not found"}, HTTPStatus.NOT_FOUND)


def serve(host: str = "127.0.0.1", port: int = 8000, verbose: bool = False) -> ThreadingHTTPServer:
    httpd = ThreadingHTTPServer((host, port), Handler)
    httpd.verbose = verbose  # type: ignore[attr-defined]
    return httpd


def main() -> None:
    parser = argparse.ArgumentParser(description="Mini parser JSON service")
    parser.add_argument("--host", default="127.0.0.1")
    parser.add_argument("--port", type=int, default=8000)
    parser.add_argument("--verbose", action="store_true")
    args = parser.parse_args()
    httpd = serve(args.host, args.port, args.verbose)
    print(f"Mini parser service listening on http://{args.host}:{args.port}")
    try:
        httpd.serve_forever()
    except KeyboardInterrupt:
        pass
    finally:
        httpd.server_close()


if __name__ == "__main__":
    main()
