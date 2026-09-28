"""Precision primitives: mpmath contexts, machine epsilons, exact parsing.

The mpmath global ``mp.dps`` is saved/restored by ``workprec`` so that stages
running at different decimal precisions cannot leak state into each other.
"""

from __future__ import annotations

from contextlib import contextmanager
from typing import Iterator

import mpmath
import numpy as np

mp = mpmath.mp
mpf = mp.mpf

# Unit roundoff of the binary floating factorizations.
FLOAT_EPSILON = {
    "float32": mpf(float(np.finfo(np.float32).eps)),  # 2^-23 ~= 1.19e-7
    "float64": mpf(float(np.finfo(np.float64).eps)),  # 2^-52 ~= 2.22e-16
}


@contextmanager
def workprec(dps: int) -> Iterator[None]:
    """Run a block with mpmath decimal precision ``dps``, restoring after."""
    previous = mp.dps
    mp.dps = dps
    try:
        yield
    finally:
        mp.dps = previous


def mp_epsilon(dps: int) -> mpf:
    """Unit roundoff at decimal precision ``dps`` (~10**(1-dps))."""
    with workprec(dps):
        return mpf(10) ** (1 - dps)


def to_mp(value: object) -> mpf:
    """Parse one scalar entry to high precision.

    Strings are parsed as exact decimals; floats are interpreted as the exact
    value of the binary literal that was sent, so a client needing more than
    ~16 digits must send entries as JSON strings (documented in the README).
    """
    if isinstance(value, mpf):
        return value
    if isinstance(value, bool):
        return mpf(int(value))
    if isinstance(value, int):
        return mpf(value)
    if isinstance(value, float):
        return mpf(value)
    if isinstance(value, str):
        text = value.strip()
        try:
            return mpf(text)
        except Exception as exc:  # mpmath raises ValueError subclasses
            raise ValueError(f"cannot parse numeric entry {text[:32]!r}") from exc
    raise TypeError(f"unsupported numeric entry type: {type(value).__name__}")
