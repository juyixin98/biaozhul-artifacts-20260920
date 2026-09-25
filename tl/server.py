"""Local HTTP API for the transparent log (stdlib only; no Flask/aiohttp).

Endpoints
---------
GET  /health                                -> service status
GET  /v1/sth                                -> signed tree head
GET  /v1/public-key                         -> Ed25519 public key (raw b64 + PEM)
POST /v1/entries            {"data_b64": "..."}  or {"text": "..."}
                                            -> {"index": n}
GET  /v1/entries?index=n                    -> entry payload + metadata
GET  /v1/proof/inclusion?leaf_index=i&tree_size=t
GET  /v1/proof/consistency?old_size=a&new_size=b
GET  /v1/verify? ...                        -> server-side verification demo

All hashes/proofs are base64 (standard, padded) JSON strings.
The server binds 127.0.0.1 by default; it is a local test service.
"""

from __future__ import annotations

import argparse
import base64
import json
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from typing import Any, Dict, Optional, Tuple
from urllib.parse import parse_qs, urlparse

from . import merkle
from .log import TransparentLog
from .signing import verify_sth_signature
from .store import EntryTooLarge


def _b64(data: bytes) -> str:
    return base64.b64encode(data).decode("ascii")


def _unb64(value: str, what: str) -> bytes:
    try:
        return base64.b64decode(value, validate=True)
    except Exception as exc:
        raise _HttpError(400, f"invalid base64 for {what}: {exc}")


class _HttpError(Exception):
    def __init__(self, status: int, message: str):
        super().__init__(message)
        self.status = status
        self.message = message


def _parse_int(params: Dict[str, list], name: str, default: Optional[int] = None) -> int:
    raw = params.get(name)
    if raw is None:
        if default is not None:
            return default
        raise _HttpError(400, f"missing query parameter: {name}")
    try:
        return int(raw[0])
    except ValueError:
        raise _HttpError(400, f"parameter {name} must be an integer")


def make_handler(tlog: TransparentLog):
    class Handler(BaseHTTPRequestHandler):
        server_version = "TransparentLog/1.0"

        def log_message(self, fmt, *args):  # quieter logs; single line each
            pass

        # -- helpers ------------------------------------------------------

        def _send_json(self, status: int, body: Dict[str, Any]) -> None:
            payload = json.dumps(body, separators=(",", ":")).encode("utf-8")
            self.send_response(status)
            self.send_header("Content-Type", "application/json; charset=utf-8")
            self.send_header("Content-Length", str(len(payload)))
            self.send_header("Cache-Control", "no-store")
            self.end_headers()
            self.wfile.write(payload)

        def _send_error(self, status: int, message: str) -> None:
            self._send_json(status, {"error": message, "status": status})

        def _query(self) -> Tuple[str, Dict[str, list]]:
            parsed = urlparse(self.path)
            return parsed.path.rstrip("/") or "/", parse_qs(parsed.query)

        def _read_json(self) -> Dict[str, Any]:
            try:
                length = int(self.headers.get("Content-Length", "0"))
            except ValueError:
                raise _HttpError(400, "bad Content-Length")
            if length <= 0:
                raise _HttpError(400, "empty request body")
            if length > 2 * 1024 * 1024:
                raise _HttpError(413, "request body too large")
            raw = self.rfile.read(length)
            try:
                body = json.loads(raw.decode("utf-8"))
            except (json.JSONDecodeError, UnicodeDecodeError) as exc:
                raise _HttpError(400, f"invalid JSON body: {exc}")
            if not isinstance(body, dict):
                raise _HttpError(400, "JSON body must be an object")
            return body

        # -- routing ------------------------------------------------------

        def do_GET(self) -> None:
            try:
                path, params = self._query()
                if path == "/health":
                    return self._send_json(200, {
                        "status": "ok",
                        "tree_size": tlog.size(),
                    })
                if path == "/v1/sth":
                    sth = tlog.current_sth()
                    return self._send_json(200, sth.to_dict())
                if path == "/v1/public-key":
                    raw = tlog.keys.public_key_raw()
                    return self._send_json(200, {
                        "algorithm": "Ed25519",
                        "public_key_b64": _b64(raw),
                        "public_key_pem": tlog.keys.export_public_pem().decode(),
                        "sth_context": "TL-STH-v1",
                    })
                if path == "/v1/entries":
                    return self._get_entry(params)
                if path == "/v1/proof/inclusion":
                    return self._inclusion(params)
                if path == "/v1/proof/consistency":
                    return self._consistency(params)
                self._send_error(404, f"unknown path: {path}")
            except _HttpError as e:
                self._send_error(e.status, e.message)
            except Exception as exc:  # never leak a traceback as 200/HTML
                self._send_error(500, f"internal error: {type(exc).__name__}: {exc}")

        def do_POST(self) -> None:
            try:
                path, _ = self._query()
                if path == "/v1/entries":
                    return self._add_entry()
                if path == "/v1/verify":
                    return self._verify_body()
                self._send_error(404, f"unknown path: {path}")
            except _HttpError as e:
                self._send_error(e.status, e.message)
            except EntryTooLarge as e:
                self._send_error(413, str(e))
            except Exception as exc:
                self._send_error(500, f"internal error: {type(exc).__name__}: {exc}")

        # -- endpoints ----------------------------------------------------

        def _add_entry(self) -> None:
            body = self._read_json()
            if "data_b64" in body:
                data = _unb64(str(body["data_b64"]), "data_b64")
            elif "text" in body:
                if not isinstance(body["text"], str):
                    raise _HttpError(400, "text must be a string")
                data = body["text"].encode("utf-8")
            else:
                raise _HttpError(
                    400, "body must contain 'data_b64' (base64 bytes) or 'text'"
                )
            index = tlog.add(data)
            self._send_json(201, {"index": index, "leaf_hash": _b64(merkle.leaf_hash(data))})

        def _get_entry(self, params) -> None:
            idx = _parse_int(params, "index")
            entry = tlog.entry(idx)
            if entry is None:
                raise _HttpError(404, f"no entry at index {idx}")
            self._send_json(200, {
                "index": entry.index,
                "timestamp": entry.ts,
                "data_b64": _b64(entry.data),
                "leaf_hash": _b64(merkle.leaf_hash(entry.data)),
            })

        def _inclusion(self, params) -> None:
            leaf_index = _parse_int(params, "leaf_index")
            tree_size = _parse_int(params, "tree_size", default=tlog.size())
            try:
                res = tlog.inclusion_by_index(leaf_index, tree_size)
            except IndexError as e:
                raise _HttpError(400, str(e))
            self._send_json(200, {
                "leaf_index": res.leaf_index,
                "tree_size": res.tree_size,
                "leaf_hash": _b64(res.leaf_hash),
                "root_hash": _b64(res.root_hash),
                "proof": [_b64(h) for h in res.proof],
                "hash_algorithm": "SHA256",
            })

        def _consistency(self, params) -> None:
            old_size = _parse_int(params, "old_size")
            new_size = _parse_int(params, "new_size", default=tlog.size())
            try:
                res = tlog.consistency(old_size, new_size)
            except IndexError as e:
                raise _HttpError(400, str(e))
            self._send_json(200, {
                "old_size": res.old_size,
                "new_size": res.new_size,
                "old_root": _b64(res.old_root),
                "new_root": _b64(res.new_root),
                "proof": [_b64(h) for h in res.proof],
                "hash_algorithm": "SHA256",
            })

        def _verify_body(self) -> None:
            """Independent server-side re-verification of client-supplied values.

            JSON body, one of:
              {"kind":"inclusion","leaf_index","tree_size","leaf_hash",
               "root_hash","proof":[...]}
              {"kind":"consistency","old_size","new_size","old_root",
               "new_root","proof":[...]}
              {"kind":"sth","tree_size","timestamp_us","root_hash","signature"}
            Clients can also verify locally with tl.merkle / tl.signing.
            """
            body = self._read_json()
            kind = body.get("kind")
            if kind == "inclusion":
                try:
                    ok = merkle.verify_inclusion(
                        int(body["leaf_index"]),
                        int(body["tree_size"]),
                        _unb64(str(body["leaf_hash"]), "leaf_hash"),
                        [_unb64(str(x), "proof") for x in body.get("proof", [])],
                        _unb64(str(body["root_hash"]), "root_hash"),
                    )
                except (KeyError, TypeError, ValueError) as e:
                    raise _HttpError(400, f"bad inclusion verification request: {e}")
                return self._send_json(200, {"verified": bool(ok)})
            if kind == "consistency":
                try:
                    ok = merkle.verify_consistency(
                        int(body["old_size"]),
                        int(body["new_size"]),
                        _unb64(str(body["old_root"]), "old_root"),
                        _unb64(str(body["new_root"]), "new_root"),
                        [_unb64(str(x), "proof") for x in body.get("proof", [])],
                    )
                except (KeyError, TypeError, ValueError) as e:
                    raise _HttpError(400, f"bad consistency verification request: {e}")
                return self._send_json(200, {"verified": bool(ok)})
            if kind == "sth":
                try:
                    ok, reason = verify_sth_signature(
                        tlog.keys.public_key_raw(),
                        int(body["tree_size"]),
                        _unb64(str(body["root_hash"]), "root_hash"),
                        int(body["timestamp_us"]),
                        _unb64(str(body["signature"]), "signature"),
                    )
                except (KeyError, TypeError, ValueError) as e:
                    raise _HttpError(400, f"bad sth verification request: {e}")
                return self._send_json(200, {"verified": ok, "reason": reason})
            raise _HttpError(400, "kind must be inclusion|consistency|sth")

    return Handler


def serve(tlog: TransparentLog, host: str, port: int) -> ThreadingHTTPServer:
    server = ThreadingHTTPServer((host, port), make_handler(tlog))
    server.daemon_threads = True
    return server


def build_log(data_dir: str) -> TransparentLog:
    import os
    from .signing import KeyManager
    from .store import LogStore

    store = LogStore(os.path.join(data_dir, "log.jsonl"))
    keys = KeyManager(os.path.join(data_dir, "ed25519_test_key.bin"))
    return TransparentLog(store, keys)


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(description="Run the local transparent log service")
    parser.add_argument("--host", default="127.0.0.1")
    parser.add_argument("--port", type=int, default=8088)
    parser.add_argument("--data-dir", default="./.tl-data")
    args = parser.parse_args(argv)

    tlog = build_log(args.data_dir)
    server = serve(tlog, args.host, args.port)
    print(f"transparent log on http://{args.host}:{args.port} (data: {args.data_dir}, size={tlog.size()})")
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        pass
    finally:
        server.server_close()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
