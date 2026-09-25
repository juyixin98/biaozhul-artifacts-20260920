"""Minimal JSON-over-HTTP streaming de-bias service (stdlib only).

Endpoints (all JSON):

* ``POST /sessions``            ``{"sample_rate": 48000, "cutoff_hz": 5}``
                                -> ``{"session_id": "...", ...}``
* ``POST /sessions/<id>/process`` ``{"samples": [0.1, ...]}``
                                -> ``{"samples": [...], "count": N}``
* ``POST /sessions/<id>/reset`` -> resets filter state
* ``POST /sessions/<id>/sample-rate`` ``{"sample_rate": 44100, "preserve_state": true}``
* ``DELETE /sessions/<id>``     -> drops the session

Each session owns one :class:`DCBlocker`, so successive ``process``
calls stream block-by-block with carried state (block-size invariant).
"""

from __future__ import annotations

import json
import uuid
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

from .filter import DCBlocker

MAX_SAMPLES_PER_REQUEST = 1_000_000


class _State:
    """Mutable container shared by request handlers."""

    def __init__(self) -> None:
        self.sessions: dict[str, DCBlocker] = {}


def _make_handler(state: _State):
    class Handler(BaseHTTPRequestHandler):
        server_version = "DCBlocker/0.1"

        # -- helpers ---------------------------------------------------
        def _send_json(self, code: int, payload: dict) -> None:
            body = json.dumps(payload).encode("utf-8")
            self.send_response(code)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)

        def _read_json(self) -> dict:
            length = int(self.headers.get("Content-Length") or 0)
            if length <= 0:
                return {}
            return json.loads(self.rfile.read(length))

        def _session(self, parts: list[str]):
            if len(parts) >= 2 and parts[0] == "sessions":
                flt = state.sessions.get(parts[1])
                if flt is None:
                    self._send_json(404, {"error": "unknown session_id"})
                    return None, None
                return flt, parts[2] if len(parts) > 2 else ""
            return None, None

        def log_message(self, fmt, *args):  # keep stdout clean
            pass

        # -- routing ----------------------------------------------------
        def do_POST(self) -> None:  # noqa: N802 (stdlib naming)
            parts = [p for p in self.path.strip("/").split("/") if p]
            try:
                body = self._read_json()
            except (json.JSONDecodeError, ValueError):
                self._send_json(400, {"error": "invalid JSON body"})
                return
            try:
                self._route_post(parts, body)
            except (ValueError, KeyError, TypeError) as exc:
                self._send_json(400, {"error": str(exc)})

        def _route_post(self, parts: list[str], body: dict) -> None:
            if parts == ["sessions"]:
                flt = DCBlocker(
                    sample_rate=body["sample_rate"],
                    cutoff_hz=body.get("cutoff_hz", 5.0),
                )
                sid = uuid.uuid4().hex
                state.sessions[sid] = flt
                self._send_json(201, {"session_id": sid, "filter": repr(flt)})
                return

            flt, action = self._session(parts)
            if flt is None:
                return
            if action == "process":
                samples = body.get("samples", [])
                if len(samples) > MAX_SAMPLES_PER_REQUEST:
                    raise ValueError("too many samples in one request")
                out = flt.process(samples)
                self._send_json(200, {"samples": out.tolist(), "count": len(out)})
            elif action == "reset":
                flt.reset()
                self._send_json(200, {"reset": True})
            elif action == "sample-rate":
                flt.set_sample_rate(
                    body["sample_rate"],
                    preserve_state=body.get("preserve_state", True),
                )
                self._send_json(200, {"sample_rate": flt.sample_rate, "R": flt.coefficient})
            else:
                self._send_json(404, {"error": "unknown action"})

        def do_DELETE(self) -> None:  # noqa: N802
            parts = [p for p in self.path.strip("/").split("/") if p]
            if len(parts) == 2 and parts[0] == "sessions" and state.sessions.pop(parts[1], None):
                self._send_json(200, {"deleted": True})
            else:
                self._send_json(404, {"error": "unknown session_id"})

    return Handler


def serve(host: str = "127.0.0.1", port: int = 8073) -> None:
    """Run the de-bias HTTP service (blocking)."""
    server = ThreadingHTTPServer((host, port), _make_handler(_State()))
    print(f"dc-blocker service listening on http://{host}:{port}")
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        pass
    finally:
        server.server_close()


if __name__ == "__main__":
    serve()
