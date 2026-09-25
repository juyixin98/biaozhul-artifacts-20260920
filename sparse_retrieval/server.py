"""Local HTTP service for the sparse-vector retrieval index.

Stdlib ``http.server`` only — no web framework, no external services.

Endpoints
---------
GET    /health              -> {"status": "ok"}
GET    /stats               -> {"num_docs": N, "num_dimensions": D}
POST   /documents           {"doc_id": 7, "vector": [[dim, weight], ...]}
                             -> upsert; reports merged duplicates and norm
DELETE /documents/<doc_id>  -> {"removed": true|false}
POST   /query               {"vector": [[dim, weight], ...], "k": 10}
                             -> {"results": [{"doc_id", "score"}...],
                                 "candidates_examined": C, "num_docs": N}
"""

from __future__ import annotations

import json
import re
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from typing import Any, Tuple

from sparse_retrieval.index import InvertedIndex
from sparse_retrieval.vector import SparseVector

_MAX_BODY_BYTES = 1_000_000
_DOC_PATH = re.compile(r"^/documents/(\d+)$")


def _error(message: str) -> bytes:
    return json.dumps({"error": message}).encode("utf-8")


class RetrievalHandler(BaseHTTPRequestHandler):
    server: "RetrievalServer"

    # -- helpers ---------------------------------------------------------
    def _send_json(self, status: int, payload: dict) -> None:
        body = json.dumps(payload).encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def _read_json(self) -> Tuple[Any, str | None]:
        length = self.headers.get("Content-Length")
        if length is None:
            return None, "missing Content-Length"
        try:
            n = int(length)
        except ValueError:
            return None, "invalid Content-Length"
        if n > _MAX_BODY_BYTES:
            return None, "body too large"
        try:
            return json.loads(self.rfile.read(n)), None
        except json.JSONDecodeError as exc:
            return None, f"invalid JSON: {exc}"

    @staticmethod
    def _parse_vector(raw: Any) -> SparseVector:
        if not isinstance(raw, list):
            raise ValueError("vector must be a list of [dim, weight] pairs")
        pairs = []
        for item in raw:
            if not isinstance(item, (list, tuple)) or len(item) != 2:
                raise ValueError("each vector entry must be [dim, weight]")
            pairs.append((item[0], item[1]))
        return SparseVector(pairs)

    def log_message(self, fmt: str, *args: Any) -> None:  # quieter tests
        pass

    # -- routes ----------------------------------------------------------
    def do_GET(self) -> None:
        if self.path == "/health":
            self._send_json(200, {"status": "ok"})
        elif self.path == "/stats":
            index = self.server.index
            self._send_json(
                200,
                {"num_docs": len(index), "num_dimensions": index.num_dimensions},
            )
        else:
            self._send_json(404, {"error": f"unknown path: {self.path}"})

    def do_POST(self) -> None:
        body, err = self._read_json()
        if err is not None:
            self._send_json(400, {"error": err})
            return
        if self.path == "/documents":
            self._handle_add_document(body)
        elif self.path == "/query":
            self._handle_query(body)
        else:
            self._send_json(404, {"error": f"unknown path: {self.path}"})

    def do_DELETE(self) -> None:
        match = _DOC_PATH.match(self.path)
        if not match:
            self._send_json(404, {"error": f"unknown path: {self.path}"})
            return
        removed = self.server.index.remove(int(match.group(1)))
        self._send_json(200, {"removed": removed})

    # -- handlers --------------------------------------------------------
    def _handle_add_document(self, body: Any) -> None:
        if not isinstance(body, dict) or "doc_id" not in body or "vector" not in body:
            self._send_json(400, {"error": "body must contain doc_id and vector"})
            return
        try:
            vector = self._parse_vector(body["vector"])
            self.server.index.add(body["doc_id"], vector)
        except (ValueError, TypeError) as exc:
            self._send_json(400, {"error": str(exc)})
            return
        self._send_json(
            200,
            {
                "doc_id": body["doc_id"],
                "nnz": vector.nnz,
                "norm": vector.norm,
                "duplicates_merged": vector.duplicates_merged,
            },
        )

    def _handle_query(self, body: Any) -> None:
        if not isinstance(body, dict) or "vector" not in body:
            self._send_json(400, {"error": "body must contain vector (and optional k)"})
            return
        try:
            vector = self._parse_vector(body["vector"])
            k = body.get("k", 10)
            result = self.server.index.query(vector, k)
        except (ValueError, TypeError) as exc:
            self._send_json(400, {"error": str(exc)})
            return
        self._send_json(
            200,
            {
                "results": [
                    {"doc_id": doc_id, "score": score}
                    for doc_id, score in result.results
                ],
                "candidates_examined": result.candidates_examined,
                "num_docs": result.num_docs,
            },
        )


class RetrievalServer(ThreadingHTTPServer):
    daemon_threads = True

    def __init__(self, host: str = "127.0.0.1", port: int = 8080) -> None:
        super().__init__((host, port), RetrievalHandler)
        self.index = InvertedIndex()


def main() -> None:
    import argparse

    parser = argparse.ArgumentParser(description="Sparse vector retrieval service")
    parser.add_argument("--host", default="127.0.0.1")
    parser.add_argument("--port", type=int, default=8080)
    args = parser.parse_args()

    server = RetrievalServer(args.host, args.port)
    print(f"serving on http://{args.host}:{args.port}")
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        pass
    finally:
        server.server_close()


if __name__ == "__main__":
    main()
