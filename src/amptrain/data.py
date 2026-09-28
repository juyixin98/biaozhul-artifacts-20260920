"""Local synthetic data fixtures.

Regression task:  y = X @ w* + b* + noise, with a hidden teacher vector.
Everything is generated on the fly from a seeded RNG -- no downloads, no
external participants, no real business data.

The fixture also provides a controllable *amplification* knob: inputs can be
scaled by an explicit multiplier so tests can deterministically drive the
low-precision forward pass into overflow.
"""

from __future__ import annotations

from dataclasses import dataclass

import numpy as np

from .config import DataConfig


@dataclass(frozen=True)
class SyntheticFixture:
    x: np.ndarray  # fp32, shape (n_samples, n_features)
    y: np.ndarray  # fp32, shape (n_samples, 1)
    teacher_w: np.ndarray
    teacher_b: float
    amplification: float

    def window(self, start: int, size: int) -> tuple[np.ndarray, np.ndarray]:
        """Return a contiguous micro-batch slice, wrapping around."""
        n = self.x.shape[0]
        idx = [(start + i) % n for i in range(size)]
        return self.x[idx], self.y[idx]


def make_fixture(
    cfg: DataConfig, n_samples: int, *, amplification: float = 1.0, rng: np.random.Generator | None = None
) -> SyntheticFixture:
    """Generate ``(X, y)``; ``amplification`` scales X for overflow tests.

    Note the targets are generated from the *unamplified* teacher signal and
    then amplified consistently with X, so amplified batches remain a
    coherent (but numerically huge) regression problem rather than NaNs.
    """
    rng = rng or np.random.default_rng(cfg.seed)
    base_x = rng.standard_normal((n_samples, cfg.n_features)).astype(np.float32)
    teacher_w = rng.standard_normal((cfg.n_features, 1)).astype(np.float32) * 0.5
    teacher_b = 0.25
    clean = base_x @ teacher_w + np.float32(teacher_b)
    noise = (rng.standard_normal((n_samples, 1)) * cfg.noise_std).astype(np.float32)
    y = (clean + noise).astype(np.float32) * np.float32(amplification)
    x = (base_x * np.float32(amplification)).astype(np.float32)
    return SyntheticFixture(
        x=x, y=y, teacher_w=teacher_w, teacher_b=teacher_b, amplification=float(amplification)
    )
