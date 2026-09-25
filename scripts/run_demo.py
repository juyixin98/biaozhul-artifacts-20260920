#!/usr/bin/env python3
"""Run the acceptance comparison and emit JSON + human-readable report.

Usage::

    python scripts/run_demo.py --out ./demo_out

Trains a reference run with no interruptions, then interrupted runs resumed
from checkpoints, and verifies final parameters/loss agree within tolerance.
Interruption schedules cover commit boundaries, mid-interval crashes (which
force deterministic replay of uncommitted batches), an epoch boundary (the
batch-end cursor position) and the last batches.
"""

from __future__ import annotations

import argparse
import json
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from checkpoint_service.compare import compare
from checkpoint_service.config import TrainConfig


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--out", default="./demo_out")
    parser.add_argument("--epochs", type=int, default=4)
    parser.add_argument("--samples", type=int, default=512)
    parser.add_argument("--batch-size", type=int, default=32)
    parser.add_argument("--checkpoint-every", type=int, default=8)
    parser.add_argument("--seed", type=int, default=42)
    args = parser.parse_args()

    cfg = TrainConfig(
        n_samples=args.samples,
        n_features=8,
        data_seed=20260925,
        train_seed=args.seed,
        batch_size=args.batch_size,
        lr=0.05,
        momentum=0.9,
        l2=1e-4,
        n_epochs=args.epochs,
        checkpoint_every=args.checkpoint_every,
    )

    spe = cfg.steps_per_epoch
    total = cfg.total_steps
    schedules = [
        [1],  # very early, mid-interval
        [7],  # one step before first commit at 8 -> replays steps 1..7
        [8],  # exactly on a commit boundary
        [spe],  # epoch boundary (batch-end cursor, pos==0)
        [spe + 1],  # just after the epoch boundary
        [total - 1],  # one step before the end
        [8, 24, spe * 2 + 3],  # multiple interruptions, one mid-interval
    ]

    os.makedirs(args.out, exist_ok=True)
    report = compare(cfg, schedules, runs_root=os.path.join(args.out, "runs"))
    summary = {
        "config": report.config,
        "total_steps": total,
        "steps_per_epoch": spe,
        "param_atol": report.param_atol,
        "loss_atol": report.loss_atol,
        "all_passed": report.passed,
        "results": report.results,
        "reference_eval_loss": report.reference.eval_loss,
        "reference_W": report.reference.W.tolist(),
        "reference_b": report.reference.b,
    }

    out_json = os.path.join(args.out, "report.json")
    with open(out_json, "w") as f:
        json.dump(summary, f, indent=2)
        f.write("\n")

    width = 96
    print("=" * width)
    print("Training checkpoint recovery - acceptance report")
    print("=" * width)
    print(f"dataset: {cfg.n_samples} samples x {cfg.n_features} features, seed={cfg.train_seed}")
    print(f"total optimizer steps: {total} (steps/epoch={spe})")
    print(
        f"checkpoint every {cfg.checkpoint_every} steps; "
        f"tolerances: W/b<={report.param_atol:g}, loss<={report.loss_atol:g}"
    )
    print("-" * width)
    print(f"{'schedule':<28} {'W diff':>12} {'b diff':>10} {'loss diff':>12} {'hist diff':>12} verdict")
    for r in report.results:
        schedule = "@" + ",".join(str(s) for s in r["interrupt_steps"])
        print(
            f"{schedule:<28} {r['W_max_abs_diff']:>12.3e} {r['b_abs_diff']:>10.3e} "
            f"{r['eval_loss_abs_diff']:>12.3e} {r['history_max_abs_diff']:>12.3e} "
            f"{'PASS' if r['passed'] else 'FAIL'}"
        )
    print("-" * width)
    print(f"reference final eval loss: {report.reference.eval_loss:.8f}")
    print(f"all schedules passed: {report.passed}")
    print(f"written: {out_json}")
    return 0 if report.passed else 1


if __name__ == "__main__":
    raise SystemExit(main())
