"""Command-line entry: run the full verification pipeline and print a report."""
from __future__ import annotations

import argparse
import json
import tempfile

from .config import AppConfig
from .logging_utils import get_logger
from .pipeline import run_pipeline


def main() -> int:
    parser = argparse.ArgumentParser(description="Adam sharding verification")
    parser.add_argument("--config", default="configs/default.json")
    parser.add_argument("--save-world", type=int, default=None)
    parser.add_argument("--restore-world", type=int, default=None)
    parser.add_argument("--ckpt-dir", default=None,
                        help="checkpoint dir (default: ephemeral temp dir)")
    parser.add_argument("--request-id", default=None)
    args = parser.parse_args()

    cfg = AppConfig.load(args.config)
    rid = args.request_id or "cli-demo"
    logger = get_logger(rid, stage="cli")
    save_world = args.save_world or cfg.save_world_size
    restore_world = args.restore_world or cfg.restore_world_size

    if args.ckpt_dir:
        ckpt_dir = args.ckpt_dir
        result = run_pipeline(
            dims=cfg.layer_dims, seed=cfg.seed,
            n_samples=cfg.batch_size * cfg.train_steps + cfg.batch_size,
            batch_size=cfg.batch_size, train_steps=cfg.train_steps,
            adam_cfg=cfg.adam, save_world_size=save_world,
            restore_world_size=restore_world, ckpt_dir=ckpt_dir,
            request_id=rid, tol=cfg.tight_tol, logger=logger,
        )
        rc = _print_report(result)
    else:
        with tempfile.TemporaryDirectory(prefix="adam-ckpt-") as ckpt_dir:
            result = run_pipeline(
                dims=cfg.layer_dims, seed=cfg.seed,
                n_samples=cfg.batch_size * cfg.train_steps + cfg.batch_size,
                batch_size=cfg.batch_size, train_steps=cfg.train_steps,
                adam_cfg=cfg.adam, save_world_size=save_world,
                restore_world_size=restore_world, ckpt_dir=ckpt_dir,
                request_id=rid, tol=cfg.tight_tol, logger=logger,
            )
            rc = _print_report(result)
    return rc


def _print_report(result) -> int:
    payload = result.to_dict()
    print(json.dumps(payload, indent=2, sort_keys=True))
    print("\n=== SUMMARY ===")
    print(f"request_id          : {payload['request_id']}")
    print(f"commit_id           : {payload['commit_id']}")
    print(f"save/restore procs  : {payload['save_world_size']} -> "
          f"{payload['restore_world_size']}")
    print(f"step after restore  : {payload['step_after_restore']}")
    print(f"finite-diff max rel : {payload['finite_difference_max_rel_error']:.3e}")
    for section in ("parameter_comparison", "moment1_comparison",
                    "moment2_comparison"):
        rep = payload[section]
        print(f"{section:22s}: passed={rep['passed']} "
              f"max_abs_diff={rep['max_abs_diff']:.3e} "
              f"failures={rep['failures']} uncertainties={rep['uncertainties']}")
    print(f"OVERALL PASSED      : {payload['passed']}")
    return 0 if payload["passed"] else 1


if __name__ == "__main__":
    raise SystemExit(main())
