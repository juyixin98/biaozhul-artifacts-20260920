"""Minimal JSON-over-HTTP service for the point-in-time join.

Stdlib only (http.server) — no web framework dependency. Endpoints:

    GET  /health   -> {"status": "ok"}
    GET  /stats    -> store size, entities, features
    POST /ingest   -> {"records": [FeatureRecord, ...]}  (append-only)
    POST /join     -> {"spine": [SpineRow, ...], "features": [...]}
                      returns {"results": [JoinedFeature, ...]}

Run:  python -m pitjoin.service --host 127.0.0.1 --port 8000 [--data seed.json]
"""

from __future__ import annotations

import argparse
import json
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

from .join import point_in_time_join
from .records import FeatureRecord, SpineRow
from .store import FeatureStore

MAX_BODY_BYTES = 10 * 1024 * 1024  # 10 MiB request cap


def parse_record(d: dict) -> FeatureRecord:
    return FeatureRecord(
        entity_id=str(d["entity_id"]),
        feature=str(d["feature"]),
        value=float(d["value"]),
        event_ts=int(d["event_ts"]),
        ingest_ts=int(d["ingest_ts"]),
    ).validate()


def parse_spine_row(d: dict) -> SpineRow:
    return SpineRow(entity_id=str(d["entity_id"]), event_ts=int(d["event_ts"])).validate()


def make_handler(store: FeatureStore) -> type[BaseHTTPRequestHandler]:
    class Handler(BaseHTTPRequestHandler):
        server_version = "pitjoin/0.1"

        def _send_json(self, status: int, payload: dict) -> None:
            body = json.dumps(payload).encode("utf-8")
            self.send_response(status)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)

        def _read_json_body(self) -> dict:
            length = int(self.headers.get("Content-Length") or 0)
            if length <= 0 or length > MAX_BODY_BYTES:
                raise ValueError("missing or oversized request body")
            return json.loads(self.rfile.read(length).decode("utf-8"))

        def do_GET(self) -> None:  # noqa: N802 (stdlib naming)
            if self.path == "/health":
                self._send_json(200, {"status": "ok"})
            elif self.path == "/stats":
                self._send_json(200, {
                    "records": len(store),
                    "entities": store.entities(),
                    "features": store.features(),
                })
            else:
                self._send_json(404, {"error": "not found"})

        def do_POST(self) -> None:  # noqa: N802 (stdlib naming)
            try:
                body = self._read_json_body()
                if self.path == "/ingest":
                    self._handle_ingest(body)
                elif self.path == "/join":
                    self._handle_join(body)
                else:
                    self._send_json(404, {"error": "not found"})
            except (KeyError, TypeError, ValueError) as exc:
                self._send_json(400, {"error": f"bad request: {exc}"})

        def _handle_ingest(self, body: dict) -> None:
            records = [parse_record(d) for d in body["records"]]
            n = store.ingest(records)
            self._send_json(200, {"ingested": n, "total": len(store)})

        def _handle_join(self, body: dict) -> None:
            spine = [parse_spine_row(d) for d in body["spine"]]
            features = [str(f) for f in body["features"]]
            results = point_in_time_join(store, spine, features)
            self._send_json(200, {"results": results})

        def log_message(self, fmt: str, *args: object) -> None:
            pass  # keep test/CI output clean

    return Handler


def load_seed_file(store: FeatureStore, path: str) -> int:
    with open(path, "r", encoding="utf-8") as fh:
        payload = json.load(fh)
    records = [parse_record(d) for d in payload["records"]]
    return store.ingest(records)


def main(argv: list[str] | None = None) -> None:
    parser = argparse.ArgumentParser(description="point-in-time feature join service")
    parser.add_argument("--host", default="127.0.0.1")
    parser.add_argument("--port", type=int, default=8000)
    parser.add_argument("--data", help="optional JSON seed file with a 'records' list")
    args = parser.parse_args(argv)

    store = FeatureStore()
    if args.data:
        n = load_seed_file(store, args.data)
        print(f"seeded {n} records from {args.data}")

    server = ThreadingHTTPServer((args.host, args.port), make_handler(store))
    print(f"pitjoin service listening on http://{args.host}:{args.port}")
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        pass
    finally:
        server.server_close()


if __name__ == "__main__":
    main()
