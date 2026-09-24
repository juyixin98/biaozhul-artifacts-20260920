"""Command line interface (no ROS required).

Commands
--------
* ``align-replay SCENARIO.json [--db out.sqlite] [--hmac-env VAR]``
    Replay a scenario through the engine, print pairing evidence, verify
    expectations and the cryptographic chain. Exit code 2 on failure.
* ``align-gen-scenarios DIR``
    Write the built-in synthetic example scenarios as JSON files.
* ``align-verify DB.sqlite [--hmac-env VAR]``
    Recompute and verify the hash/HMAC chain of an existing database.
"""

from __future__ import annotations

import argparse
import json
import os
import sys
from typing import Sequence

from . import __version__
from .scenario import (
    check_expectations,
    format_evidence,
    load_scenario,
    run_scenario,
    write_example_scenarios,
)
from .storage import ChainVerifyError, Storage


def _secret_from_env(name: str | None) -> bytes | None:
    if not name:
        return None
    if name not in os.environ:
        raise SystemExit(f"error: environment variable {name!r} is not set")
    return os.environ[name].encode("utf-8")


def cmd_replay(args: argparse.Namespace) -> int:
    scenario = load_scenario(args.scenario)
    secret = _secret_from_env(args.hmac_env)
    summary = run_scenario(scenario, db_path=args.db, hmac_secret=secret)
    print(format_evidence(summary, limit=args.limit))
    print()
    failures = check_expectations(summary, scenario)
    if failures:
        print("EXPECTATIONS: FAIL")
        for f in failures:
            print(f"  - {f}")
        return 2
    print("EXPECTATIONS: PASS")
    if args.report:
        with open(args.report, "w", encoding="utf-8") as fh:
            json.dump(
                {
                    "scenario": scenario.get("name"),
                    "status_counts": summary["status_counts"],
                    "epochs": summary["epochs"],
                    "config_versions": summary["config_versions"],
                    "chain": summary["chain"],
                    "decisions": summary["decisions"],
                },
                fh, indent=2, ensure_ascii=False,
            )
            fh.write("\n")
        print(f"evidence report written to {args.report}")
    return 0


def cmd_gen(args: argparse.Namespace) -> int:
    paths = write_example_scenarios(args.directory)
    for p in paths:
        print(f"wrote {p}")
    return 0


def cmd_verify(args: argparse.Namespace) -> int:
    secret = _secret_from_env(args.hmac_env)
    storage = Storage(args.db, hmac_secret=secret)
    try:
        result = storage.verify_chain()
    except ChainVerifyError as exc:
        print(f"TAMPERED/INVALID: {exc}")
        return 2
    finally:
        storage.close()
    print(f"CHAIN OK: {result['rows']} rows, {result['algorithm']}, "
          f"tail={result['tail'][:16]}...")
    return 0


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="time-alignment",
        description="ROS2 camera/IMU time alignment - offline tools")
    parser.add_argument("--version", action="version", version=__version__)
    sub = parser.add_subparsers(dest="command", required=True)

    p_replay = sub.add_parser("replay", help="replay a scenario JSON")
    p_replay.add_argument("scenario")
    p_replay.add_argument("--db", default=":memory:",
                          help="SQLite output path (default: in-memory)")
    p_replay.add_argument("--hmac-env", default=None,
                          help="env var holding the HMAC secret")
    p_replay.add_argument("--limit", type=int, default=None,
                          help="print only the first N evidence rows")
    p_replay.add_argument("--report", default=None,
                          help="write a full JSON evidence report here")
    p_replay.set_defaults(func=cmd_replay)

    p_gen = sub.add_parser("gen-scenarios", help="write example scenario JSON files")
    p_gen.add_argument("directory")
    p_gen.set_defaults(func=cmd_gen)

    p_ver = sub.add_parser("verify", help="verify a database hash/HMAC chain")
    p_ver.add_argument("db")
    p_ver.add_argument("--hmac-env", default=None)
    p_ver.set_defaults(func=cmd_verify)

    return parser


def main(argv: Sequence[str] | None = None) -> int:
    args = build_parser().parse_args(argv if argv is not None else sys.argv[1:])
    return int(args.func(args))


if __name__ == "__main__":
    raise SystemExit(main())
