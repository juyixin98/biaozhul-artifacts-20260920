"""Peak detection on a magnitude spectrum."""

from __future__ import annotations

import numpy as np


def find_local_maxima(mag: np.ndarray) -> np.ndarray:
    """Indices k where mag[k] strictly exceeds both neighbours.

    Endpoints (DC, Nyquist) are included if they exceed their single
    neighbour, so boundary tones are still reported (as boundary peaks).
    """
    n = mag.size
    if n < 2:
        return np.array([], dtype=int)
    interior = np.where((mag[1:-1] > mag[:-2]) & (mag[1:-1] >= mag[2:]))[0] + 1
    ends = []
    if mag[0] > mag[1]:
        ends.append(0)
    if mag[-1] > mag[-2]:
        ends.append(n - 1)
    return np.sort(np.concatenate([interior, np.array(ends, dtype=int)]))


def detect_peaks(
    mag: np.ndarray,
    min_peak_ratio: float = 0.01,
    max_peaks: int = 16,
) -> list[int]:
    """Return peak bin indices, strongest first.

    min_peak_ratio is relative to the strongest local maximum; peaks below
    it are treated as noise/sidelobes and dropped.
    """
    idx = find_local_maxima(mag)
    if idx.size == 0:
        return []
    strongest = mag[idx].max()
    if strongest <= 0.0:
        return []
    keep = idx[mag[idx] >= min_peak_ratio * strongest]
    order = np.argsort(mag[keep])[::-1]
    return [int(keep[i]) for i in order[:max_peaks]]


def has_neighbour_peak(peak_bins: list[int], k: int, width_bins: int) -> bool:
    """True if another detected peak lies within width_bins of bin k."""
    return any(other != k and abs(other - k) < width_bins for other in peak_bins)
