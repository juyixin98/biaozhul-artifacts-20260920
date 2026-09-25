"""Top-level analysis pipeline: window -> FFT -> detect -> interpolate.

Accuracy contract (see README): sub-bin interpolation assumes ONE dominant
tone per window mainlobe. Estimates are only as good as the sampled data;
no accuracy beyond what the samples support is claimed. Boundary bins
(DC, Nyquist) cannot be interpolated and are reported at bin centre.
"""

from __future__ import annotations

from dataclasses import dataclass, asdict

import numpy as np

from .detector import detect_peaks, has_neighbour_peak
from .interpolate import (
    clamp_delta,
    hann_ratio_delta,
    jacobsen_delta,
    log_parabolic_delta,
)
from .windows import get_window, mainlobe_width_bins

ESTIMATORS = ("auto", "hann_ratio", "jacobsen", "log_parabolic")

# Best estimator per window when estimator="auto".
_AUTO_FOR_WINDOW = {"hann": "hann_ratio", "rect": "jacobsen",
                    "blackmanharris": "log_parabolic"}


@dataclass
class Peak:
    bin: int
    freq_hz: float
    amplitude: float
    delta_bins: float
    estimator: str
    boundary: bool
    interference: bool
    clamped: bool


def _resolve_estimator(estimator: str, window: str) -> str:
    if estimator == "auto":
        return _AUTO_FOR_WINDOW[window]
    if estimator == "hann_ratio" and window != "hann":
        raise ValueError("hann_ratio estimator requires the hann window")
    if estimator not in ESTIMATORS:
        raise ValueError(f"unknown estimator {estimator!r}; choose from {ESTIMATORS}")
    return estimator


def _sub_bin_delta(
    spectrum: np.ndarray,
    mag: np.ndarray,
    k: int,
    estimator: str,
) -> tuple[float, bool]:
    """Return (delta_bins, clamped) for interior peak bin k."""
    if estimator == "hann_ratio":
        delta = hann_ratio_delta(mag[k - 1], mag[k], mag[k + 1])
    elif estimator == "jacobsen":
        delta = jacobsen_delta(spectrum[k - 1], spectrum[k], spectrum[k + 1])
    elif estimator == "log_parabolic":
        delta = log_parabolic_delta(mag[k - 1], mag[k], mag[k + 1])
    else:  # pragma: no cover - guarded by _resolve_estimator
        raise ValueError(f"unknown estimator {estimator!r}")
    return clamp_delta(delta)


def _dtft_magnitude(xw: np.ndarray, nu: float) -> float:
    """Magnitude of the windowed signal's DTFT at fractional bin nu."""
    n = np.arange(xw.size)
    return float(np.abs(np.sum(xw * np.exp(-2j * np.pi * nu * n / xw.size))))


def analyze(
    samples: np.ndarray,
    sample_rate: float,
    window: str = "hann",
    estimator: str = "auto",
    min_peak_ratio: float = 0.01,
    max_peaks: int = 16,
) -> dict:
    """Analyse a real-valued signal and return detected peaks as a dict.

    Amplitudes are peak amplitudes of the underlying sinusoids, corrected
    for window coherent gain by evaluating the windowed DTFT at the
    interpolated frequency (not spectral density).
    """
    x = np.asarray(samples, dtype=np.float64)
    n = x.size
    if n < 8:
        raise ValueError("need at least 8 samples")
    if sample_rate <= 0:
        raise ValueError("sample_rate must be positive")

    w = get_window(window, n)
    resolved = _resolve_estimator(estimator, window)
    xw = x * w
    spectrum = np.fft.rfft(xw)
    mag = np.abs(spectrum)
    bin_hz = sample_rate / n
    width = mainlobe_width_bins(window)
    w_sum = w.sum()

    peak_bins = detect_peaks(mag, min_peak_ratio=min_peak_ratio, max_peaks=max_peaks)
    peaks: list[Peak] = []
    for k in peak_bins:
        boundary = k == 0 or k == n // 2
        interference = has_neighbour_peak(peak_bins, k, width)
        if boundary:
            # One-sided neighbour set: sub-bin interpolation is not defined.
            delta, clamped = 0.0, False
            peak_mag = float(mag[k])
        else:
            delta, clamped = _sub_bin_delta(spectrum, mag, k, resolved)
            peak_mag = _dtft_magnitude(xw, k + delta)
        # DC and Nyquist bins are unpaired in the rfft: no factor of 2.
        amplitude = peak_mag / w_sum if boundary else 2.0 * peak_mag / w_sum
        peaks.append(
            Peak(
                bin=k,
                freq_hz=float((k + delta) * bin_hz),
                amplitude=float(amplitude),
                delta_bins=float(delta),
                estimator="none" if boundary else resolved,
                boundary=bool(boundary),
                interference=bool(interference),
                clamped=bool(clamped),
            )
        )
    peaks.sort(key=lambda p: p.freq_hz)
    return {
        "sample_rate": sample_rate,
        "n_samples": n,
        "bin_hz": bin_hz,
        "window": window,
        "estimator": resolved,
        "peaks": [asdict(p) for p in peaks],
    }
