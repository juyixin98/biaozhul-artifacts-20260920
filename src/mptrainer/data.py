"""Synthetic data fixtures: deterministic batches, no external data.

A fixed ground-truth linear map plus noise; ``amplify`` scales inputs up
to force low-precision overflow on demand (the controlled-overflow knob
used by tests, the demo, and the service).
"""

from __future__ import annotations

import numpy as np


class SyntheticBatchSource:
    """Seeded, reproducible batch generator for one run."""

    def __init__(self, n_features: int, n_targets: int, seed: int = 7) -> None:
        self._rng = np.random.default_rng(seed)
        truth_rng = np.random.default_rng(seed + 1)
        self._true_w = truth_rng.standard_normal((n_features, n_targets)).astype(np.float32)

    def batch(
        self, batch_size: int, amplify: float = 1.0
    ) -> tuple[np.ndarray, np.ndarray]:
        x = self._rng.standard_normal((batch_size, len(self._true_w))).astype(np.float32)
        x = (x * np.float32(amplify)).astype(np.float32)
        noise = self._rng.standard_normal((batch_size, self._true_w.shape[1])).astype(np.float32)
        y = (x @ self._true_w + np.float32(0.01) * noise).astype(np.float32)
        return x, y
