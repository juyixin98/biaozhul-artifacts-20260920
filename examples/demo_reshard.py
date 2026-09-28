"""End-to-end demo: train, save with N processes, restore with M, reshard.

Run from the repo root:
    PYTHONPATH=src python3 examples/demo_reshard.py

This keeps the checkpoint on disk (unlike the ephemeral HTTP pipeline) so you
can inspect manifest.json and the shard files.
"""
from __future__ import annotations

import json
import os
import sys

import numpy as np

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "src"))

from adam_shards.adam import Adam, AdamConfig, OptimState  # noqa: E402
from adam_shards.graph import GraphSpec, MLPModel, synthetic_dataset  # noqa: E402
from adam_shards.logging_utils import get_logger  # noqa: E402
from adam_shards.pipeline import make_batches, moments_from_state, train  # noqa: E402
from adam_shards.sharding import read_manifest, reshard_checkpoint, restore_arrays  # noqa: E402

ROOT = os.path.join(os.path.dirname(__file__), "..")
SRC = os.path.join(ROOT, ".checkpoints", "saved_2proc")
DST = os.path.join(ROOT, ".checkpoints", "resharded_5proc")


def main() -> None:
    logger = get_logger("demo-reshard", "demo")
    spec = GraphSpec((3, 4, 2))
    data = np.load(os.path.join(ROOT, "examples", "sample_dataset.npz"))
    x, y = data["x"], data["y"]
    batches = make_batches(x, y, batch_size=8, n_steps=4)

    model = MLPModel(spec, seed=20260927)
    state = OptimState({n: spec.shape_of(n) for n in spec.param_names})
    train(model, Adam(AdamConfig(lr=0.05)), state, batches)

    from adam_shards.sharding import save_checkpoint
    os.makedirs(os.path.dirname(SRC), exist_ok=True)
    save_checkpoint(SRC, model.named_params(), moments_from_state(state),
                    world_size=2, request_id="demo-reshard", logger=logger)

    # Restore with a different process count (round-robin across readers).
    loaded = restore_arrays(SRC, world_size=3, request_id="demo-reshard",
                            logger=logger)
    print("restored step:", next(iter(loaded["steps"].values())),
          "source world:", loaded["source_world_size"])

    # Materialize a brand-new shard layout at world size 5.
    commit = reshard_checkpoint(SRC, DST, 5, request_id="demo-reshard",
                                logger=logger)
    new_manifest = read_manifest(DST)
    print("new commit:", commit, "new world_size:", new_manifest["world_size"])
    print("new shard files:",
          sorted(f for f in os.listdir(DST) if f.endswith(".bin"))[:4], "...")


if __name__ == "__main__":
    main()
