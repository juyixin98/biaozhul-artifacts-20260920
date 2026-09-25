"""Local HTTP front end for the enrange encrypted object store.

Pure standard-library server (http.server), bound to localhost by default.
Nothing here ever returns plaintext that the store has not authenticated.

Routes
------
POST   /objects?block_size=N     create object, server-generated id, body=data
PUT    /objects/{id}?block_size=N  store body under an explicit id
GET    /objects                  list object ids (JSON array)
GET    /objects/{id}             full object (200) or ranged read (206)
HEAD   /objects/{id}             authenticated length headers, no body
GET    /objects/{id}/meta        authenticated metadata (JSON)
GET    /healthz                  liveness probe

Range selection for GET /objects/{id}:
  * HTTP style:        Range: bytes=START-END   (inclusive, RFC 7233 single range)
  * half-open query:   ?start=START&end=END     (end omitted => to end of object)

Status codes: 200/201/206 success; 400 bad request; 404 unknown object;
411 length required; 416 unsatisfiable range; 409 authentication failure
(tampering or corruption); 501 multi-range Range headers are not supported.
"""

from __future__ import annotations

import json
import re
from http import HTTPStatus
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from typing import Optional, Tuple
from urllib.parse import parse_qs, urlsplit

from .errors import (
    AuthenticationError,
    InvalidRangeError,
    NotFoundError,
    StoreError,
)
from .store import DEFAULT_BLOCK_SIZE, MAX_BLOCK_SIZE, ObjectStore

_RANGE_RE = re.compile(r"^bytes=(\d*)-(\d*)$")


def _parse_range_header(value: str, total: int) -> Tuple[int, int]:
    """Parse one RFC 7233 ``bytes=a-b`` range into half-open [start, end)."""
    match = _RANGE_RE.match(value.replace(" ", ""))
    if not match:
        # Multi-range ("bytes=0-1,3-4") or suffix ("bytes=-5") are rejected
        # explicitly rather than silently misread.
        raise InvalidRangeError("only a single 'bytes=START-END' range is supported")
    start_s, end_s = match.groups()
    if start_s == "" and end_s == "":
        raise InvalidRangeError("malformed Range header")
    if start_s == "":
        # suffix range bytes=-N: last N bytes
        suffix = int(end_s)
        if suffix <= 0:
            raise InvalidRangeError("suffix range must be positive")
        start = max(0, total - suffix)
        return start, total
    start = int(start_s)
    end = int(end_s) if end_s != "" else total - 1
    if start > end or start >= total:
        raise InvalidRangeError(f"range not satisfiable for length {total}")
    return start, min(end + 1, total)


def make_handler(store: ObjectStore) -> type[BaseHTTPRequestHandler]:
    """Build a request handler class closed over one store instance."""

    class Handler(BaseHTTPRequestHandler):
        server_version = "enrange/0.1"
        protocol_version = "HTTP/1.1"

        # Silence noisy default access log; keep errors.
        def log_message(self, fmt: str, *args: object) -> None:
            return

        def handle_one_request(self) -> None:
            # Under HTTP/1.1 keep-alive the server waits for a second request
            # line; if the client has closed the connection that surfaces as
            # ConnectionResetError, which is normal teardown rather than a
            # request-handling failure.
            try:
                super().handle_one_request()
            except (ConnectionResetError, BrokenPipeError):
                self.close_connection = True

        # ------------------------------------------------------------ helpers

        def _json(self, status: int, payload: dict) -> None:
            body = json.dumps(payload).encode("utf-8")
            self.send_response(status)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)

        def _error(self, status: HTTPStatus, code: str, message: str) -> None:
            self._json(status, {"error": code, "message": message})

        def _block_size_param(self, query: dict) -> int:
            raw = query.get("block_size", [str(DEFAULT_BLOCK_SIZE)])[0]
            try:
                value = int(raw)
            except ValueError:
                raise ValueError("block_size must be an integer")
            if not 1 <= value <= MAX_BLOCK_SIZE:
                raise ValueError(f"block_size must be in [1, {MAX_BLOCK_SIZE}]")
            return value

        def _read_body(self) -> bytes:
            length = self.headers.get("Content-Length")
            if length is None:
                # Chunked transfer-encoding is deliberately not implemented:
                # this is a localhost service and all supplied clients set
                # Content-Length.
                raise MissingContentLength()
            try:
                n = int(length)
            except ValueError as exc:
                raise ValueError("invalid Content-Length") from exc
            if n < 0:
                raise ValueError("invalid Content-Length")
            return self.rfile.read(n)

        def _object_id_from_path(self, path: str) -> Optional[str]:
            # /objects/{id} or /objects/{id}/meta
            parts = [p for p in path.split("/") if p]
            if len(parts) == 2 and parts[0] == "objects":
                return parts[1]
            if len(parts) == 3 and parts[0] == "objects" and parts[2] == "meta":
                return parts[1]
            return None

        # --------------------------------------------------------------- GET

        def do_GET(self) -> None:
            try:
                parts_url = urlsplit(self.path)
                path, query_s = parts_url.path, parse_qs(parts_url.query)
                if path == "/healthz":
                    self._json(HTTPStatus.OK, {"status": "ok"})
                    return
                if path == "/objects":
                    ids = sorted(
                        p.name[:-4]
                        for p in store.objects_dir.iterdir()
                        if p.suffix == ".bin"
                    )
                    self._json(HTTPStatus.OK, {"objects": ids})
                    return

                object_id = self._object_id_from_path(path)
                if object_id is None:
                    self._error(HTTPStatus.NOT_FOUND, "not_found", "no such route")
                    return

                if path.endswith("/meta"):
                    self._json(HTTPStatus.OK, store.stat(object_id))
                    return

                self._serve_object(object_id, query_s)
            except NotFoundError as exc:
                self._error(HTTPStatus.NOT_FOUND, "not_found", str(exc))
            except AuthenticationError as exc:
                # Deliberately terse: no unauthenticated bytes or structural
                # hints are echoed back.
                self._error(
                    HTTPStatus.CONFLICT, "authentication_failed", "object failed verification"
                )
            except InvalidRangeError as exc:
                self._error(
                    HTTPStatus.REQUESTED_RANGE_NOT_SATISFIABLE, "bad_range", str(exc)
                )
            except ValueError as exc:
                self._error(HTTPStatus.BAD_REQUEST, "bad_request", str(exc))

        def _serve_object(self, object_id: str, query_s: dict) -> None:
            meta = store.stat(object_id)  # verifies header
            total = meta["length"]

            start, end = self._requested_bounds(query_s, total)
            if start == 0 and end == total:
                data = store.get(object_id)
                self.send_response(HTTPStatus.OK)
                self.send_header("Content-Type", "application/octet-stream")
                self.send_header("Content-Length", str(len(data)))
                self.send_header("Accept-Ranges", "bytes")
                self.end_headers()
                self.wfile.write(data)
                return

            data = store.read_range(object_id, start, end)
            self.send_response(HTTPStatus.PARTIAL_CONTENT)
            self.send_header("Content-Type", "application/octet-stream")
            self.send_header("Content-Length", str(len(data)))
            self.send_header(
                "Content-Range", f"bytes {start}-{end - 1}/{total}"
            )
            self.send_header("Accept-Ranges", "bytes")
            self.end_headers()
            self.wfile.write(data)

        def _requested_bounds(self, query_s: dict, total: int) -> Tuple[int, int]:
            range_header = self.headers.get("Range")
            if range_header is not None:
                if "start" in query_s or "end" in query_s:
                    raise ValueError("do not mix Range header with start/end query params")
                return _parse_range_header(range_header, total)

            start = 0
            end: Optional[int] = None
            if "start" in query_s:
                start = int(query_s["start"][0])
            if "end" in query_s:
                end = int(query_s["end"][0])
            if end is None:
                end = total
            if start < 0 or end < 0 or start > end or end > total:
                raise InvalidRangeError(
                    f"invalid range [{start}, {end}) for object of length {total}"
                )
            return start, end

        # -------------------------------------------------------------- HEAD

        def do_HEAD(self) -> None:
            try:
                object_id = self._object_id_from_path(urlsplit(self.path).path)
                if object_id is None:
                    self._error(HTTPStatus.NOT_FOUND, "not_found", "no such route")
                    return
                meta = store.stat(object_id)
                self.send_response(HTTPStatus.OK)
                self.send_header("Content-Type", "application/octet-stream")
                self.send_header("Content-Length", str(meta["length"]))
                self.send_header("X-Enrange-Blocks", str(meta["blocks"]))
                self.send_header("X-Enrange-Block-Size", str(meta["block_size"]))
                self.send_header("Accept-Ranges", "bytes")
                self.end_headers()
            except NotFoundError as exc:
                self._error(HTTPStatus.NOT_FOUND, "not_found", str(exc))
            except AuthenticationError:
                self._error(
                    HTTPStatus.CONFLICT, "authentication_failed", "object failed verification"
                )

        # ---------------------------------------------------------- PUT/POST

        def do_PUT(self) -> None:
            self._write_object(explicit_id=True)

        def do_POST(self) -> None:
            self._write_object(explicit_id=False)

        def _write_object(self, explicit_id: bool) -> None:
            from .store import new_object_id

            try:
                url = urlsplit(self.path)
                path_parts = [p for p in url.path.split("/") if p]
                query = parse_qs(url.query)

                if explicit_id:
                    if len(path_parts) != 2 or path_parts[0] != "objects":
                        self._error(HTTPStatus.NOT_FOUND, "not_found", "use PUT /objects/{id}")
                        return
                    object_id = path_parts[1]
                else:
                    if path_parts != ["objects"]:
                        self._error(
                            HTTPStatus.NOT_FOUND, "not_found", "use POST /objects"
                        )
                        return
                    object_id = new_object_id()

                block_size = self._block_size_param(query)
                data = self._read_body()
                meta = store.put(object_id, data, block_size=block_size)
                status = HTTPStatus.CREATED
                self.send_response(status)
                self.send_header("Content-Type", "application/json")
                body = json.dumps(meta).encode("utf-8")
                self.send_header("Content-Length", str(len(body)))
                self.end_headers()
                self.wfile.write(body)
            except MissingContentLength:
                self._error(
                    HTTPStatus.LENGTH_REQUIRED,
                    "length_required",
                    "Content-Length header is required",
                )
            except ValueError as exc:
                self._error(HTTPStatus.BAD_REQUEST, "bad_request", str(exc))
            except StoreError as exc:
                self._error(HTTPStatus.CONFLICT, "store_error", str(exc))

    return Handler


class MissingContentLength(Exception):
    pass


def build_server(store: ObjectStore, host: str = "127.0.0.1", port: int = 8080) -> ThreadingHTTPServer:
    return ThreadingHTTPServer((host, port), make_handler(store))
