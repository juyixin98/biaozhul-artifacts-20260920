#!/usr/bin/env python3
"""Drive a real cross-process crash / restart demo.

Spawns ``crash_worker.py`` subprocesses: the first worker is killed (hard
exit, no cleanup) at CRASH_AT_STEP, a second worker restarts and finishes
from the committed checkpoint. Then trains an uninterrupted reference in a
separate directory and compares final parameters/loss.

Unlike run_demo.py (same-process interruption), the checkpoint bytes here
cross real process boundaries. Exit code 137 from the crashed worker is the
expected, asserted-on outcome.
"""

from __future__ import annotations

import argparse
import json
import os
import subprocess
import sys

import numpy as np

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(HERE)
WORKER = os.path.join(HERE, "crash_worker.py")

sys.path.insert(0, ROOT)
from checkpoint_service.config import PARAM_ATOL, TrainConfig  # noqa: E402
from checkpoint_service.trainer import Trainer  # noqa: E402


def _env(run_dir: str, crash_at: int | None) -> dict[str, str]:
    env = dict(os.environ)
    env["RUN_DIR"] = run_dir
    env.pop("CRASH_AT_STEP", None)
    if crash_at is not None:
        env["CRASH_AT_STEP"] = str(crash_at)
    return env


def run_worker(run_dir: str, crash_at: int | None = None) -> subprocess.CompletedProcess[str]:
    proc = subprocess.run(
        [sys.executable, WORKER],
        env=_env(run_dir, crash_at),
        capture_output=True,
        text=True,
        check=False,
    )
    return proc


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--out", default="./crash_demo_out")
    parser.add_argument("--crash-at", type=int, default=13)
    args = parser.parse_args()

    os.makedirs(args.out, exist_ok=True)
    crash_dir = os.path.join(args.out, "crashed_run")
    ref_dir = os.path.join(args.out, "reference_run")

    print(f"--- worker 1: will hard-exit after global step {args.crash_at} ---")
    first = run_worker(crash_dir, crash_at=args.crash_at)
    print(first.stdout, end="")
    if first.stderr.strip():
        print("[stderr]", first.stderr.strip())
    assert first.returncode == 137, f"expected hard exit 137, got {first.returncode}"
    print(f"worker 1 exit code: {first.returncode} (137 = killed, as expected)\n")

    print("--- worker 2: new process resumes from checkpoint and finishes ---")
    second = run_worker(crash_dir)
    print(second.stdout, end="")
    if second.stderr.strip():
        print("[stderr]", second.stderr.strip())
    assert second.returncode == 0, f"resume worker failed: {second.returncode}"

    recovered = Trainer.resume(crash_dir)
    assert recovered.cursor.global_step == recovered.cfg.total_steps

    print("--- uninterrupted reference (separate process directory) ---")
    cfg = TrainConfig()
    ref = run_worker(ref_dir)
    print(ref.stdout.splitlines()[-1])
    reference = Trainer.resume(ref_dir)

    w_diff = float(np.max(np.abs(recovered.model.W - reference.model.W)))
    b_diff = abs(float(recovered.model.b) - float(reference.model.b))
    loss_diff = abs(recovered.evaluate() - reference.evaluate())
    ref_hist = [x["loss"] for x in reference.loss_history]
    rec_hist = [x["loss"] for x in recovered.loss_history]
    hist_diff = max(abs(a - b) for a, b in zip(ref_hist, rec_hist))

    result = {
        "crash_at_step": args.crash_at,
        "checkpoint_every": cfg.checkpoint_every,
        "total_steps": cfg.total_steps,
        "first_worker_exit_code": first.returncode,
        "W_max_abs_diff": w_diff,
        "b_abs_diff": b_diff,
        "eval_loss_ref": reference.evaluate(),
        "eval_loss_recovered": recovered.evaluate(),
        "eval_loss_abs_diff": loss_diff,
        "history_max_abs_diff": hist_diff,
        "param_atol": PARAM_ATOL,
        "passed": w_diff <= PARAM_ATOL and b_diff <= PARAM_ATOL and loss_diff <= 1e-6
        and hist_diff <= 1e-6,
    }
    with open(os.path.join(args.out, "crash_report.json"), "w") as f:
        json.dump(result, f, indent=2)
        f.write("\n")

    print("\n--- comparison after real process kill + restart ---")
    print(json.dumps(result, indent=2))
    print("RESULT:", "PASS" if result["passed"] else "FAIL")
    return 0 if result["passed"] else 1


if __name__ == "__main__":
    raise SystemExit(main())
