"""Command-line entry point: ``python -m checkpoint_service ...``.

Subcommands mirror the HTTP routes so request samples work without a server:

    create-run   --run-id ID [config overrides]
    train        --run-id ID [--steps N]
    status       --run-id ID
    inspect      --run-id ID
    list-runs
    serve        [--host H] [--port P]
"""

from __future__ import annotations

import argparse
import json
from typing import Any

from .api import serve
from .config import TrainConfig
from .errors import CheckpointError, ServiceError
from .service import TrainingService


def _print(payload: dict[str, Any]) -> None:
    import numpy as np

    def default(obj: Any) -> Any:
        if isinstance(obj, np.ndarray):
            return obj.tolist()
        if isinstance(obj, np.generic):
            return obj.item()
        return str(obj)

    print(json.dumps(payload, indent=2, default=default))


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="checkpoint_service", description=__doc__)
    parser.add_argument("--runs-dir", default="./runs", help="directory holding run state")
    sub = parser.add_subparsers(dest="command", required=True)

    sub.add_parser("list-runs")

    p_create = sub.add_parser("create-run")
    p_create.add_argument("--run-id", required=True)
    p_create.add_argument("--overwrite", action="store_true")
    for field in TrainConfig.__dataclass_fields__.values():  # type: ignore[attr-defined]
        p_create.add_argument(f"--{field.name.replace('_', '-')}", type=type(field.default))

    p_train = sub.add_parser("train")
    p_train.add_argument("--run-id", required=True)
    p_train.add_argument("--steps", type=int, default=None, help="stop_after for this call")

    p_status = sub.add_parser("status")
    p_status.add_argument("--run-id", required=True)

    p_inspect = sub.add_parser("inspect")
    p_inspect.add_argument("--run-id", required=True)

    p_serve = sub.add_parser("serve")
    p_serve.add_argument("--host", default="127.0.0.1")
    p_serve.add_argument("--port", type=int, default=8080)

    return parser


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    service = TrainingService(args.runs_dir)

    try:
        if args.command == "serve":
            serve(args.host, args.port, args.runs_dir)
            return 0
        if args.command == "list-runs":
            _print({"runs": service.list_runs()})
            return 0
        if args.command == "create-run":
            overrides = {
                name: getattr(args, name)
                for name in TrainConfig.__dataclass_fields__  # type: ignore[attr-defined]
                if getattr(args, name) is not None
            }
            cfg = TrainConfig(**overrides)
            _print(service.create_run(args.run_id, cfg, overwrite=args.overwrite))
            return 0
        if args.command == "train":
            _print(service.train(args.run_id, stop_after=args.steps))
            return 0
        if args.command == "status":
            _print(service.status(args.run_id))
            return 0
        if args.command == "inspect":
            _print(service.inspect_checkpoint(args.run_id))
            return 0
    except (ServiceError, CheckpointError, ValueError) as exc:
        print(json.dumps({"error": type(exc).__name__, "detail": str(exc)}))
        return 1
    return 2


if __name__ == "__main__":
    raise SystemExit(main())
