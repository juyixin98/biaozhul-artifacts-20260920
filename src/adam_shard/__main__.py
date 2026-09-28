"""Command-line entry point: ``python -m adam_shard ...``."""

from __future__ import annotations

import argparse
import json
import sys
import uuid

from .fixtures import AppConfig, write_sample_dataset
from .logging_setup import configure_logging
from .service import describe_commits, train, validate_commit, verify_restore_parity


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="adam-shard")
    parser.add_argument("--config", default=None, help="path to config JSON")
    sub = parser.add_subparsers(dest="cmd", required=True)

    p_init = sub.add_parser("init-data", help="generate the synthetic sample dataset")

    p_train = sub.add_parser("train", help="run sharded training and commit a checkpoint")
    p_train.add_argument("--world-size", type=int, required=True)
    p_train.add_argument("--steps", type=int, default=None)
    p_train.add_argument("--model-commit", default=None)
    p_train.add_argument("--optim-commit", default=None)

    p_list = sub.add_parser("list", help="list commits")

    p_val = sub.add_parser("validate", help="strictly validate + reshard a commit")
    p_val.add_argument("commit_id")
    p_val.add_argument("--target-world-size", type=int, required=True)

    p_par = sub.add_parser("verify-parity", help="restore at new world size and compare one step")
    p_par.add_argument("source_commit")
    p_par.add_argument("--target-world-size", type=int, required=True)

    args = parser.parse_args(argv)
    configure_logging()
    cfg = AppConfig.load(args.config)

    if args.cmd == "init-data":
        path = write_sample_dataset(cfg)
        print(f"wrote {path}")
        return 0
    if args.cmd == "train":
        out = train(
            cfg, args.world_size, steps=args.steps,
            model_commit=args.model_commit, optim_commit=args.optim_commit,
        )
        print(json.dumps({"commit_id": out.commit_id, "step": out.step, "world_size": out.world_size}, indent=2))
        return 0
    if args.cmd == "list":
        print(json.dumps(describe_commits(cfg), indent=2))
        return 0
    if args.cmd == "validate":
        print(json.dumps(validate_commit(cfg, args.commit_id, args.target_world_size), indent=2))
        return 0
    if args.cmd == "verify-parity":
        report = verify_restore_parity(
            cfg, args.source_commit, args.target_world_size,
            f"req-cli-{uuid.uuid4().hex[:8]}",
        )
        print(json.dumps(report.to_dict(), indent=2))
        return 1 if report.status == "fail" else 0
    return 2


if __name__ == "__main__":
    sys.exit(main())
