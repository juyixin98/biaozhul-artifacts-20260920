"""First-order DC-blocking (high-pass) filter for chunked streaming audio.

Model (explicit first-order high-pass, a.k.a. DC blocker):

    y[n] = x[n] - x[n-1] + R * y[n-1]

with the pole

    R = exp(-2 * pi * fc / fs)

where ``fc`` is the -3 dB cutoff in Hz and ``fs`` the sample rate in Hz.

Properties of this model:

* DC gain is exactly 0, so a constant bias is fully removed at steady state
  (zero steady-state error for a DC input).
* The step response decays as R**n, i.e. with time constant
  ``tau = -1 / ln(R)`` samples (~ fs / (2*pi*fc) for fc << fs).
* The filter is strictly causal: output sample n depends only on inputs
  up to n. Processing a stream in blocks of any size yields bit-identical
  output to processing it in one shot (block invariance) — no block-level
  mean subtraction, so no future samples ever leak into the output.
"""

from __future__ import annotations

import math

import numpy as np

MIN_CUTOFF_HZ = 1e-6


class DCBlocker:
    """Stateful first-order high-pass filter for DC-bias removal.

    Parameters
    ----------
    sample_rate:
        Sample rate in Hz. Must be > 0.
    cutoff_hz:
        -3 dB cutoff frequency of the high-pass in Hz. Must satisfy
        0 < cutoff_hz < sample_rate / 2. Smaller values track slower
        (longer response time) but attenuate low-frequency signal less.
    """

    def __init__(self, sample_rate: float, cutoff_hz: float = 5.0) -> None:
        self._cutoff_hz = self._validate_cutoff(cutoff_hz)
        self._sample_rate = 0.0
        self._r = 0.0
        self.set_sample_rate(sample_rate)
        self.reset()

    # ------------------------------------------------------------------
    # configuration
    # ------------------------------------------------------------------
    @staticmethod
    def _validate_cutoff(cutoff_hz: float) -> float:
        fc = float(cutoff_hz)
        if not math.isfinite(fc) or fc < MIN_CUTOFF_HZ:
            raise ValueError(f"cutoff_hz must be finite and >= {MIN_CUTOFF_HZ}, got {cutoff_hz!r}")
        return fc

    @property
    def sample_rate(self) -> float:
        return self._sample_rate

    @property
    def cutoff_hz(self) -> float:
        return self._cutoff_hz

    @property
    def coefficient(self) -> float:
        """Pole R of the first-order high-pass."""
        return self._r

    def set_sample_rate(self, sample_rate: float, *, preserve_state: bool = True) -> None:
        """Reconfigure the sample rate; the pole R is recomputed.

        The cutoff frequency in Hz is kept, so the time constant in
        *seconds* is unchanged. By default the filter state is preserved
        (continuous output across the reconfiguration); pass
        ``preserve_state=False`` to also reset the state.
        """
        fs = float(sample_rate)
        if not math.isfinite(fs) or fs <= 0.0:
            raise ValueError(f"sample_rate must be finite and > 0, got {sample_rate!r}")
        if self._cutoff_hz >= fs / 2.0:
            raise ValueError(
                f"cutoff_hz ({self._cutoff_hz}) must be < Nyquist ({fs / 2.0}) "
                f"for sample_rate {fs}"
            )
        self._sample_rate = fs
        self._r = math.exp(-2.0 * math.pi * self._cutoff_hz / fs)
        if not preserve_state:
            self.reset()

    def set_cutoff(self, cutoff_hz: float, *, preserve_state: bool = True) -> None:
        """Change the cutoff frequency; the pole R is recomputed."""
        self._cutoff_hz = self._validate_cutoff(cutoff_hz)
        self.set_sample_rate(self._sample_rate, preserve_state=preserve_state)

    def reset(self) -> None:
        """Reset the filter state (previous input/output) to zero."""
        self._x_prev = 0.0
        self._y_prev = 0.0

    # ------------------------------------------------------------------
    # derived metrics
    # ------------------------------------------------------------------
    @property
    def time_constant_samples(self) -> float:
        """Step-response time constant in samples: -1 / ln(R)."""
        return -1.0 / math.log(self._r)

    def settling_samples(self, tol: float = 0.01) -> int:
        """Samples until a step response decays below ``tol`` of its start."""
        if not 0.0 < tol < 1.0:
            raise ValueError("tol must be in (0, 1)")
        return int(math.ceil(math.log(tol) / math.log(self._r)))

    # ------------------------------------------------------------------
    # processing
    # ------------------------------------------------------------------
    def process(self, block: np.ndarray | list[float]) -> np.ndarray:
        """Filter one block of samples and return the de-biased block.

        The block may have any length >= 0 (including very short blocks
        down to a single sample). State carries across calls, so chunked
        processing is bit-identical to one-shot processing.

        The input is never modified; a new float64 array is returned.
        """
        x = np.asarray(block, dtype=np.float64)
        if x.ndim != 1:
            raise ValueError(f"block must be 1-D, got shape {x.shape}")
        y = np.empty(x.shape[0], dtype=np.float64)
        r = self._r
        x_prev = self._x_prev
        y_prev = self._y_prev
        for n in range(x.shape[0]):
            xn = x[n]
            yn = xn - x_prev + r * y_prev
            y[n] = yn
            x_prev = xn
            y_prev = yn
        self._x_prev = x_prev
        self._y_prev = y_prev
        return y

    def process_stream(self, blocks) -> list[np.ndarray]:
        """Filter an iterable of blocks, returning one output block each."""
        return [self.process(b) for b in blocks]

    # ------------------------------------------------------------------
    # introspection
    # ------------------------------------------------------------------
    @property
    def state(self) -> dict[str, float]:
        """Current internal state (for diagnostics / persistence)."""
        return {"x_prev": self._x_prev, "y_prev": self._y_prev}

    def __repr__(self) -> str:
        return (
            f"DCBlocker(sample_rate={self._sample_rate}, "
            f"cutoff_hz={self._cutoff_hz}, R={self._r:.8f})"
        )
