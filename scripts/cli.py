#!/usr/bin/env python3
"""Command-line entry point.

Examples
--------
python -m scripts.cli query data/example.dl "ancestor(ann, X)"
python -m scripts.cli compile data/example.dl
python -m scripts.cli materialize data/example.dl
python -m scripts.cli serve --host 127.0.0.1 --port 8000
"""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

from app.logging_setup import setup_logging
from app.service import DatalogService
from app.store.sqlite_store import EvidenceStore


def _service(args: argparse.Namespace) -> DatalogService:
    return DatalogService(EvidenceStore(args.db))


def _read(path: str) -> str:
    return Path(path).read_text(encoding="utf-8")


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="datalog", description="Restricted Datalog service")
    parser.add_argument("--db", default="data/evidence.db", help="SQLite evidence DB path")
    parser.add_argument("--log-level", default="INFO")
    sub = parser.add_subparsers(dest="cmd", required=True)

    q = sub.add_parser("query", help="compile, evaluate and answer one goal")
    q.add_argument("program")
    q.add_argument("goal")
    q.add_argument("--request-id")

    c = sub.add_parser("compile", help="compile only (safety + stratification)")
    c.add_argument("program")

    m = sub.add_parser("materialize", help="evaluate and persist the full closure")
    m.add_argument("program")

    s = sub.add_parser("serve", help="run the HTTP API")
    s.add_argument("--host", default="127.0.0.1")
    s.add_argument("--port", type=int, default=8000)

    args = parser.parse_args(argv)
    setup_logging(args.log_level)

    if args.cmd == "serve":
        import uvicorn

        uvicorn.run("app.api.server:app", host=args.host, port=args.port, reload=False)
        return 0

    svc = _service(args)
    if args.cmd == "query":
        resp = svc.run_query(_read(args.program), args.goal, request_id=args.request_id)
    elif args.cmd == "compile":
        resp = svc.compile_only(_read(args.program))
    else:
        resp = svc.materialize(_read(args.program))

    json.dump(resp.to_dict(), sys.stdout, indent=2, ensure_ascii=False)
    sys.stdout.write("\n")
    return 0 if resp.status == "ok" else 2


if __name__ == "__main__":
    raise SystemExit(main())
