"""Float filter design helpers (NumPy only, no SciPy).

Analog lowpass prototypes are mapped to digital second-order sections with
the bilinear transform and frequency pre-warping.

``cutoff`` is a fraction of Nyquist (0 < cutoff < 1); digital edge frequency
is ``omega = pi * cutoff`` rad/sample.
"""

from __future__ import annotations

import numpy as np

from .biquad import SOS


def _butter_poles(n: int) -> np.ndarray:
    """Poles of the normalized (3 dB edge at 1 rad/s) analog Butterworth LPF."""
    k = np.arange(n)
    # Equally spaced on the left-half unit circle, starting at pi/2 + pi/(2n).
    angles = np.pi * (2 * k + n + 1) / (2 * n)
    return np.exp(1j * angles)


def _cheby1_poles(n: int, rp_db: float) -> np.ndarray:
    """Poles of the normalized (ripple edge at 1 rad/s) analog Chebyshev I LPF."""
    eps = np.sqrt(10.0 ** (rp_db / 10.0) - 1.0)
    mu = np.arcsinh(1.0 / eps) / n
    k = np.arange(n)
    theta = np.pi * (2 * k + 1) / (2 * n)
    return -np.sinh(mu) * np.sin(theta) + 1j * np.cosh(mu) * np.cos(theta)


def _conjugate_pairs(poles: np.ndarray) -> list[tuple[complex, complex | None]]:
    """Upper-half-plane conjugate pairs, plus one lone real pole if odd."""
    real = [p for p in poles if abs(p.imag) < 1e-10]
    upper = sorted(
        (p for p in poles if p.imag > 1e-10),
        key=lambda p: abs(p.real),  # closest to jw axis first
    )
    pairs: list[tuple[complex, complex | None]] = [
        (p, np.conj(p)) for p in upper
    ]
    if len(real) % 2:
        pairs.append((real[0].real + 0j, None))
    return pairs


def _sections_from_analog(
    poles: np.ndarray, omega: float, dc_gain_target: float = 1.0
) -> np.ndarray:
    """Bilinear-transform analog poles (zeros at infinity) to SOS rows.

    Each analog section ``1/((s-p1)(s-p2))`` maps with ``s = c(z-1)/(z+1)``
    to numerator ``(1+z^-1)^2 / c^2``; a lone real pole contributes
    ``(1+z^-1) / c``.  One overall gain factor then sets the cascade DC
    gain to ``dc_gain_target`` (Chebyshev even-order prototypes have a
    natural DC gain below unity).
    """
    c = 1.0 / np.tan(omega / 2.0)

    def map_pole(p: complex) -> complex:
        return (c + p) / (c - p)

    built: list[tuple[float, np.ndarray]] = []
    for p1, p2 in _conjugate_pairs(poles):
        d1 = map_pole(p1)
        if p2 is None:
            b = np.array([1.0 / c, 1.0 / c, 0.0])
            a = np.array([1.0, -d1.real, 0.0])
            order_key = abs(d1)
        else:
            d2 = map_pole(p2)
            b = np.array([1.0, 2.0, 1.0]) / (c * c)
            a = np.array([1.0, -(d1 + d2).real, (d1 * d2).real])
            order_key = max(abs(d1), abs(d2))
        built.append((order_key, np.concatenate([b, a])))

    built.sort(key=lambda t: -t[0])
    rows = [row for _, row in built]

    # Single overall gain normalization at DC (z = 1).
    actual = 1.0
    for r in rows:
        actual *= float(np.sum(r[0:3]) / np.sum(r[3:6]))
    k = dc_gain_target / actual
    rows[0][0:3] *= k
    return np.asarray(rows, dtype=np.float64)


def _check_order_cutoff(order: int, cutoff: float) -> None:
    if order < 1:
        raise ValueError("order must be >= 1")
    if not 0.0 < cutoff < 1.0:
        raise ValueError("cutoff must be in (0, 1) as a fraction of Nyquist")


def butter_lowpass(order: int, cutoff: float) -> SOS:
    """Butterworth lowpass as cascaded sections (maximally flat passband)."""
    _check_order_cutoff(order, cutoff)
    poles = _butter_poles(order)
    # Unit-circle poles give unity prototype DC gain.
    return SOS(_sections_from_analog(poles, np.pi * cutoff, 1.0))


def cheby1_lowpass(order: int, cutoff: float, rp_db: float = 1.0) -> SOS:
    """Chebyshev type-I lowpass (``rp_db`` dB passband ripple), steeper skirt.

    Odd orders have unity DC gain; even orders have a natural DC gain of
    ``-rp_db`` dB, and the passband edge sits at ``-rp_db`` in both cases.
    """
    _check_order_cutoff(order, cutoff)
    if rp_db <= 0:
        raise ValueError("rp_db must be > 0")
    eps = np.sqrt(10.0 ** (rp_db / 10.0) - 1.0)
    g0 = 1.0 if order % 2 else 1.0 / np.sqrt(1.0 + eps * eps)
    return SOS(_sections_from_analog(
        _cheby1_poles(order, rp_db), np.pi * cutoff, g0
    ))


def dc_gain(sos: SOS) -> float:
    """Cascade gain at z = 1 (DC)."""
    return float(
        np.prod(
            np.sum(sos.sections[:, 0:3], axis=1)
            / np.sum(sos.sections[:, 3:6], axis=1)
        )
    )
