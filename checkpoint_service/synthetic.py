"""Reproducible synthetic regression dataset.

No external data is downloaded. The dataset is generated deterministically
from ``data_seed`` with a dedicated generator, so recreating it after a crash
yields bit-identical arrays (verified by a SHA-256 fingerprint).
"""

from __future__ import annotations

import hashlib
from dataclasses import dataclass

import numpy as np

from .config import TrainConfig


@dataclass(frozen=True)
class Dataset:
    X: np.ndarray  # shape (n_samples, n_features), float32
    y: np.ndarray  # shape (n_samples,), float32
    fingerprint: str  # sha256 over the exact bytes of X and y

    def verify(self) -> None:
        """Raise ValueError if the arrays no longer match the fingerprint."""
        digest = hashlib.sha256(self.X.tobytes() + self.y.tobytes()).hexdigest()
        if digest != self.fingerprint:
            raise ValueError("dataset fingerprint mismatch")


def make_dataset(cfg: TrainConfig) -> Dataset:
    """Generate a noisy linear-regression dataset.

    Ground-truth weights/bias are also deterministic functions of
    ``data_seed``; the noise generator is independent of the training RNG.
    """
    rng = np.random.default_rng(cfg.data_seed)

    # Standard-normal features (float32 on purpose: that is the training dtype).
    X = rng.standard_normal((cfg.n_samples, cfg.n_features), dtype=np.float32)

    true_w = (
        rng.standard_normal(cfg.n_features, dtype=np.float32) * 2.0
    ).astype(np.float32)
    true_b = np.float32(0.5)

    noise = rng.standard_normal(cfg.n_samples, dtype=np.float32) * np.float32(0.1)
    y = (X @ true_w + true_b + noise).astype(np.float32)

    fingerprint = hashlib.sha256(X.tobytes() + y.tobytes()).hexdigest()
    return Dataset(X=X, y=y, fingerprint=fingerprint)
