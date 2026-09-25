"""Synthetic signal generators for detector validation.

Every generator returns the same structure:

    SignalScenario(signal, sample_rate, truth)

where ``truth`` lists ground-truth events with sample indices, so detection
delay and false-alarm counts can be measured automatically.

All noise is generated from a caller-supplied (or internally seeded)
``numpy.random.Generator`` for reproducibility.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import List, Optional

import numpy as np


@dataclass(frozen=True)
class EventTruth:
    """Ground-truth abnormal segment.

    Attributes:
        kind: ``"step"`` | ``"drift"`` | ``"spike"``.
        start: first anomalous sample index.
        end: last anomalous sample index (inclusive).
        amplitude: step offset / final drift offset / spike height.
    """

    kind: str
    start: int
    end: int
    amplitude: float


@dataclass(frozen=True)
class SignalScenario:
    name: str
    signal: np.ndarray
    sample_rate: float
    truth: List[EventTruth] = field(default_factory=list)
    description: str = ""

    @property
    def n_samples(self) -> int:
        return self.signal.size

    def anomaly_mask(self) -> np.ndarray:
        """Boolean mask of every sample covered by a ground-truth event."""
        mask = np.zeros(self.n_samples, dtype=bool)
        for ev in self.truth:
            mask[ev.start : ev.end + 1] = True
        return mask

    def event_regions(self) -> List[tuple[int, int]]:
        return [(ev.start, ev.end) for ev in self.truth]


def _rng(seed: Optional[int]) -> np.random.Generator:
    return np.random.default_rng(seed)


def make_step(
    n_samples: int = 2000,
    *,
    noise_std: float = 1.0,
    step_at: int = 1000,
    step_amplitude: float = 6.0,
    baseline: float = 0.0,
    sample_rate: float = 100.0,
    seed: Optional[int] = 1001,
) -> SignalScenario:
    """Gaussian noise followed by a persistent level jump at ``step_at``."""
    r = _rng(seed)
    x = baseline + r.normal(0.0, noise_std, size=n_samples)
    x[step_at:] += step_amplitude
    truth = [EventTruth("step", step_at, n_samples - 1, step_amplitude)]
    return SignalScenario(
        "step",
        x,
        sample_rate,
        truth,
        f"level step of {step_amplitude} sigma at sample {step_at}",
    )


def make_drift(
    n_samples: int = 2000,
    *,
    noise_std: float = 1.0,
    drift_from: int = 1000,
    drift_slope: float = 0.004,
    sample_rate: float = 100.0,
    seed: Optional[int] = 1002,
) -> SignalScenario:
    """Gaussian noise followed by a slow linear ramp starting at ``drift_from``."""
    r = _rng(seed)
    x = r.normal(0.0, noise_std, size=n_samples)
    ramp = np.arange(n_samples - drift_from, dtype=float) * drift_slope
    x[drift_from:] += ramp
    final_offset = float(ramp[-1])
    truth = [EventTruth("drift", drift_from, n_samples - 1, final_offset)]
    return SignalScenario(
        "drift",
        x,
        sample_rate,
        truth,
        f"linear drift of slope {drift_slope}/sample from sample {drift_from}",
    )


def make_spikes(
    n_samples: int = 2000,
    *,
    noise_std: float = 1.0,
    spike_indices: Optional[List[int]] = None,
    spike_amplitude: float = 12.0,
    sample_rate: float = 100.0,
    seed: Optional[int] = 1003,
) -> SignalScenario:
    """Gaussian noise with isolated one-sample impulses."""
    if spike_indices is None:
        spike_indices = [400, 900, 1500]
    r = _rng(seed)
    x = r.normal(0.0, noise_std, size=n_samples)
    truth: List[EventTruth] = []
    for idx in spike_indices:
        x[idx] += spike_amplitude
        truth.append(EventTruth("spike", idx, idx, spike_amplitude))
    return SignalScenario(
        "spikes",
        x,
        sample_rate,
        truth,
        f"isolated spikes of {spike_amplitude} sigma at {spike_indices}",
    )


def make_missing(
    base: SignalScenario,
    *,
    missing_rate: float = 0.05,
    seed: Optional[int] = 2001,
) -> SignalScenario:
    """Wrap a scenario and replace a random fraction of samples with NaN."""
    r = _rng(seed)
    x = base.signal.copy()
    n_drop = int(round(missing_rate * x.size))
    drop_idx = r.choice(x.size, size=n_drop, replace=False)
    x[drop_idx] = np.nan
    return SignalScenario(
        base.name + "_missing",
        x,
        base.sample_rate,
        list(base.truth),
        base.description + f"; {missing_rate:.0%} randomly missing samples",
    )


SCENARIO_BUILDERS = {
    "step": make_step,
    "drift": make_drift,
    "spikes": make_spikes,
}
