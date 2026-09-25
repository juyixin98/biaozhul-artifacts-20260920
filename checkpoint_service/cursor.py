"""Data-iteration cursor.

The cursor remembers *where training is* in the dataset: current epoch,
position inside the epoch and the exact index permutation of the epoch.
Persisting it (plus RNG state) is what lets a resumed run visit samples in
precisely the same order as an uninterrupted run.

A trailing partial batch at the end of an epoch is dropped, matching
``n_samples // batch_size`` steps per epoch.
"""

from __future__ import annotations

from dataclasses import dataclass

import numpy as np


@dataclass
class DataCursor:
    n_samples: int
    epoch: int
    pos: int  # samples already consumed in the current epoch
    permutation: np.ndarray  # int64 index array for the current epoch
    global_step: int  # optimizer steps taken so far

    @classmethod
    def fresh(cls, n_samples: int, rng: np.random.Generator) -> "DataCursor":
        return cls(
            n_samples=n_samples,
            epoch=0,
            pos=0,
            permutation=rng.permutation(n_samples).astype(np.int64),
            global_step=0,
        )

    def next_indices(
        self, batch_size: int, rng: np.random.Generator
    ) -> np.ndarray | None:
        """Indices of the next mini-batch, advancing the cursor.

        Returns ``None`` only when the current epoch ends exactly at its
        boundary handled by the caller loop; here partial trailing batches
        roll into a new epoch.
        """
        if self.pos + batch_size > self.n_samples:
            # End of epoch: draw a fresh permutation from the training RNG.
            self.epoch += 1
            self.permutation = rng.permutation(self.n_samples).astype(np.int64)
            self.pos = 0
        idx = self.permutation[self.pos : self.pos + batch_size]
        self.pos += batch_size
        self.global_step += 1
        return idx

    def to_state(self) -> dict:
        return {
            "n_samples": int(self.n_samples),
            "epoch": int(self.epoch),
            "pos": int(self.pos),
            "permutation": self.permutation.astype(np.int64, copy=True),
            "global_step": int(self.global_step),
        }

    @classmethod
    def from_state(cls, state: dict) -> "DataCursor":
        required = {"n_samples", "epoch", "pos", "permutation", "global_step"}
        missing = required - set(state)
        if missing:
            raise ValueError(f"missing cursor fields: {sorted(missing)}")
        perm = np.asarray(state["permutation"], dtype=np.int64)
        n = int(state["n_samples"])
        if perm.shape != (n,):
            raise ValueError("cursor permutation has wrong shape")
        if sorted(int(i) for i in perm) != list(range(n)):
            raise ValueError("cursor permutation is not a valid index permutation")
        pos = int(state["pos"])
        if not 0 <= pos <= n:
            raise ValueError("cursor pos out of range")
        return cls(
            n_samples=n,
            epoch=int(state["epoch"]),
            pos=pos,
            permutation=perm,
            global_step=int(state["global_step"]),
        )
