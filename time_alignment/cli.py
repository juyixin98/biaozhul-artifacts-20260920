"""Command line: replay scenarios, verify evidence chains, generate keys.

Examples
--------
    python -m time_alignment.cli genkey --out examples/hmac.key
    python -m time_alignment.cli replay examples/burst.json \
        --db build/burst.db --key examples/hmac.key --export build/burst.jsonl
    python -m time_alignment.cli verify build/burst.db --key examples/hmac.key
    python -m time_alignment.cli show build/burst.db
"""
from __future__ import annotations

import argparse
import json
import os
import secrets
import sys
from pathlib import Path

from .scenario import evaluate, load_scenario
from .storage import EvidenceStore, load_key
from .model import AlignParams


def cmd_genkey(args: argparse.Namespace) -> int:
    key = secrets.token_bytes(32)  # cryptographically secure via os.urandom
    out = Path(args.out)
    out.parent.mkdir(parents=True, exist_ok=True)
    out.write_bytes(key.hex().encode("ascii"))
    os.chmod(out, 0o600)
    print(f"wrote 64 hex chars (32 random bytes) to {out} (mode 0600)")
    return 0


def cmd_replay(args: argparse.Namespace) -> int:
    scenario = load_scenario(args.scenario)
    ev = evaluate(scenario)
    print(f"scenario: {scenario.get('name', args.scenario)}")
    print(ev.render())

    if args.db:
        db_path = Path(args.db)
        db_path.parent.mkdir(parents=True, exist_ok=True)
        key = (load_key(args.key) if args.key
               else secrets.token_bytes(32))
        key_id = Path(args.key).name if args.key else "ephemeral"
        # Re-run once while persisting, so seq/mac cover exactly the DB rows.
        from .scenario import events_of, build_matcher
        m = build_matcher(scenario)
        with EvidenceStore(db_path, key, key_id=key_id) as store:
            for x in events_of(scenario):
                if hasattr(x, "params_dict"):
                    m.request_params(AlignParams.from_dict(x.params_dict))
                else:
                    store.append_many(m.register(x).outcomes)
            store.append_many(m.finalize().outcomes)
            report = store.verify()
            if args.export:
                n = store.export_jsonl(args.export)
                print(f"exported {n} records -> {args.export}")
        if not args.key:
            print("WARNING: --key not given; DB signed with an ephemeral key "
                  "that is now unrecoverable (verify later with --key file)")
        if not report.ok:
            print(f"chain self-check FAILED: {report.first_error}")
            return 2
        print(f"evidence: {db_path} ({report.total} rows, HMAC chain valid)")

    if args.check_expect and not ev.ok:
        return 1
    return 0


def cmd_verify(args: argparse.Namespace) -> int:
    key = load_key(args.key)
    with EvidenceStore(args.db, key) as store:
        report = store.verify()
    status = "VALID" if report.ok else "INVALID"
    print(f"chain {status}: {report.total} records, counts={report.counts}")
    if not report.ok:
        print(f"first error: {report.first_error}")
        return 1
    return 0


def cmd_show(args: argparse.Namespace) -> int:
    with EvidenceStore(args.db, b"\x00" * 32, ) as store:
        for r in store.fetch_records():
            p = json.loads(r["payload"])
            print(f"#{r['seq']:>3} {r['kind']:<6} ep={r['epoch']} "
                  f"v={r['params_version']} {json.dumps(p, ensure_ascii=False)}")
    return 0


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(prog="time_alignment")
    sub = ap.add_subparsers(dest="cmd", required=True)

    g = sub.add_parser("genkey", help="generate a random HMAC key")
    g.add_argument("--out", required=True)
    g.set_defaults(func=cmd_genkey)

    r = sub.add_parser("replay", help="replay a scenario JSON through matcher")
    r.add_argument("scenario")
    r.add_argument("--db", help="SQLite output path (optional)")
    r.add_argument("--key", help="HMAC key file (ephemeral if omitted)")
    r.add_argument("--export", help="also export JSONL evidence")
    r.add_argument("--no-check", dest="check_expect", action="store_false",
                   default=True, help="do not fail on unmet expectations")
    r.set_defaults(func=cmd_replay)

    v = sub.add_parser("verify", help="verify SQLite HMAC chain")
    v.add_argument("db")
    v.add_argument("--key", required=True)
    v.set_defaults(func=cmd_verify)

    s = sub.add_parser("show", help="print records (opens with dummy key)")
    s.add_argument("db")
    s.set_defaults(func=cmd_show)

    args = ap.parse_args(argv)
    return args.func(args)


if __name__ == "__main__":
    sys.exit(main())
