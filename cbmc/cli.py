"""Command-line interface for the contract bounded model checker.

Examples::

    python -m cbmc fixtures
    python -m cbmc check examples/escrow_missing_debit.json --steps 5
    python -m cbmc check --fixture safe-transfer --steps 8 --no-replay
    python -m cbmc replay path/to/contract.json path/to/trace.json
"""

from __future__ import annotations

import argparse
import json
import sys
from typing import Any

from .checker import CheckConfig, check_contract
from .errors import ContractError, ReplayError
from .fixtures import FIXTURES, get_fixture, list_fixtures, load_example
from .model import load_contract_file
from .replay import replay_trace


def _print_json(obj: Any) -> None:
    json.dump(obj, sys.stdout, indent=2, ensure_ascii=False)
    sys.stdout.write("\n")


def _resolve_contract(args: argparse.Namespace) -> Any:
    if args.fixture:
        return load_example(args.fixture), get_fixture(args.fixture)
    if not args.contract:
        raise SystemExit("provide a contract file or --fixture ID")
    return load_contract_file(args.contract), None


def cmd_fixtures(_args: argparse.Namespace) -> int:
    for item in list_fixtures():
        print(f"{item['id']:28s} {item['file']}")
        print(f"{'':28s} {item['summary']}")
    return 0


def cmd_check(args: argparse.Namespace) -> int:
    try:
        contract, raw = _resolve_contract(args)
        config = CheckConfig(
            steps=args.steps,
            timeout_ms=args.timeout_ms,
            checks=tuple(args.checks),
        ).validated()
    except ContractError as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 2

    result = check_contract(contract, config)
    payload = result.to_dict()

    print(f"contract : {result.contract}", file=sys.stderr)
    print(f"status   : {result.status.value}", file=sys.stderr)
    print(f"bound    : {result.bound} steps "
          f"({result.solver_calls} solver call(s), "
          f"{result.elapsed_ms} ms)", file=sys.stderr)
    if result.depth is not None:
        print(f"depth    : violation first reachable at step {result.depth}",
              file=sys.stderr)
        print(f"violation: {result.violation} — {result.detail}", file=sys.stderr)
    print(f"note     : {result.note}", file=sys.stderr)

    if result.trace and args.replay:
        try:
            report = replay_trace(contract, result.trace)
            payload["replay"] = report.to_dict()
            print("replay   : trace executed concretely, states match solver; "
                  f"verified violations: {report.violated}", file=sys.stderr)
        except ReplayError as exc:
            payload["replay_error"] = str(exc)
            print(f"replay   : FAILED — {exc}", file=sys.stderr)

    _print_json(payload)
    return 0 if result.status.value == "counterexample" else 1


def cmd_replay(args: argparse.Namespace) -> int:
    try:
        contract = load_contract_file(args.contract)
        with open(args.trace, "r", encoding="utf-8") as fh:
            data = json.load(fh)
    except (OSError, json.JSONDecodeError, ContractError) as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 2

    trace = data["trace"] if isinstance(data, dict) and "trace" in data else data
    try:
        report = replay_trace(contract, trace,
                              compare_to_solver=not args.no_compare)
    except ReplayError as exc:
        print(f"replay error: {exc}", file=sys.stderr)
        return 3

    for step in report.steps:
        print(f"step {step.step}: action={step.action} params={step.params}",
              file=sys.stderr)
        print(f"          state={step.state}", file=sys.stderr)
    print(f"violated: {report.violated}", file=sys.stderr)
    _print_json(report.to_dict())
    return 0 if report.violated else 1


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="cbmc",
        description="Bounded model checker for explicit JSON contracts.",
    )
    sub = parser.add_subparsers(dest="command", required=True)

    sub.add_parser("fixtures", help="list bundled example fixtures")

    p_check = sub.add_parser("check", help="bounded-check a contract")
    p_check.add_argument("contract", nargs="?", help="path to a contract JSON file")
    p_check.add_argument("--fixture", choices=sorted(FIXTURES),
                         help="use a bundled fixture instead of a file")
    p_check.add_argument("--steps", type=int, default=5)
    p_check.add_argument("--timeout-ms", type=int, default=10_000)
    p_check.add_argument("--checks", nargs="+",
                         default=["nonnegative", "conservation", "targets"],
                         choices=["nonnegative", "conservation", "targets"])
    p_check.add_argument("--no-replay", dest="replay", action="store_false")
    p_check.set_defaults(func=cmd_check)

    p_replay = sub.add_parser("replay", help="concretely replay a trace JSON")
    p_replay.add_argument("contract", help="contract JSON file")
    p_replay.add_argument("trace", help="check-result JSON or a bare trace list")
    p_replay.add_argument("--no-compare", action="store_true",
                          help="do not compare replayed states to the trace states")
    p_replay.set_defaults(func=cmd_replay)
    return parser


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    if args.command == "fixtures":
        return cmd_fixtures(args)
    return args.func(args)


if __name__ == "__main__":
    raise SystemExit(main())
