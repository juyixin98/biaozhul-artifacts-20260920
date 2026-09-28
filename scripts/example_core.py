"""Minimal example driving the numerical core directly (no HTTP server).

Run from the repository root:
    python scripts/example_core.py
"""

from __future__ import annotations

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

import numpy as np

from sparse_embedding.config import (
    ClipConfig,
    ClipMode,
    OptimizerConfig,
    ServiceConfig,
    TableSpec,
)
from sparse_embedding.errors import EmptyBatchError, ValidationBatchRejectedError
from sparse_embedding.service import SparseEmbeddingService
from sparse_embedding.journal import RunJournal


def main() -> int:
    cfg = ServiceConfig(
        table=TableSpec(num_rows=8, dim=3),
        optimizer=OptimizerConfig(lr=0.1, momentum=0.9),
        clip=ClipConfig(ClipMode.GLOBAL, max_norm=5.0),  # declared global only
        state_dir="var/example_state",
        seed=20260928,
    )
    svc = SparseEmbeddingService(cfg, RunJournal("var/example_state/journal.jsonl"))

    # Duplicate IDs: row 0 appears three times and is summed first.
    report = svc.apply(
        [0, 2, 0, 0],
        [[1, 1, 1], [0, 2, 0], [1, 0, 0], [0, 0, 1]],
        run_id="core-ex-1",
        batch_id="dup",
    )
    print("active rows     :", report.active_indices.tolist())
    print("global clip     :", report.clip_report.get("scale"))
    print("row 0 steps     :", svc.state.row_steps[0])
    print("row 0 momentum  :", np.round(svc.state.momentum[0], 4).tolist())
    print("untouched row 3 :", svc.state.row_steps[3], "steps")

    # Zero-sum row 5 is seen but takes no step.
    r2 = svc.apply([5, 5], [[3, 3, 3], [-3, -3, -3]], run_id="core-ex-2")
    print("zero-skipped    :", r2.zero_skipped_indices.tolist(),
          "| row5 steps:", svc.state.row_steps[5])

    # Empty batch is an explicit category, not a silent success.
    try:
        svc.apply([], [], run_id="core-ex-3")
    except EmptyBatchError as exc:
        print("empty batch     :", exc.code, "| global_step:", svc.state.global_step)

    # Out-of-range index rejects the whole batch; nothing changed.
    before = svc.state.global_step
    try:
        svc.apply([0, 99], [[1, 1, 1], [1, 1, 1]], run_id="core-ex-4")
    except ValidationBatchRejectedError as exc:
        print("rejected batch  :", exc.code, "| step unchanged:",
              svc.state.global_step == before)

    manifest = svc.checkpoint(run_id="core-ex-5")
    print("checkpoint      :", manifest)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
