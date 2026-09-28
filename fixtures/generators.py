"""Reusable synthetic fixtures.

All cases are deterministic (seeded) and local — no external data. Every
case carries its expected answer from an *independent* construction where
one exists (impulse cases read the answer straight off c and r by the
index rule, with no multiplication at all).
"""

from __future__ import annotations

from dataclasses import dataclass, field

import numpy as np


@dataclass
class Case:
    """One verification case: inputs plus an optional independent expected answer."""

    name: str
    c: np.ndarray
    r: np.ndarray
    x: np.ndarray  # (n,) vector or (n, k) batch
    mode: str = "auto"
    expected: np.ndarray | None = None  # independent answer, if cheaply known
    tags: list[str] = field(default_factory=list)


def _rng(seed: int) -> np.random.Generator:
    return np.random.default_rng(seed)


def asymmetric_real_case(seed: int = 1, m: int = 7, n: int = 5) -> Case:
    """Non-square (m != n), non-power-of-two sizes, float64."""
    g = _rng(seed)
    c = g.standard_normal(m)
    r = g.standard_normal(n)
    r[0] = c[0]  # shared element
    x = g.standard_normal(n)
    return Case("asymmetric_real", c, r, x, tags=["real", "asymmetric", "non-pow2"])


def complex_case(seed: int = 2, m: int = 6, n: int = 4) -> Case:
    """Complex c, r, x with non-power-of-two sizes."""
    g = _rng(seed)
    c = g.standard_normal(m) + 1j * g.standard_normal(m)
    r = g.standard_normal(n) + 1j * g.standard_normal(n)
    r[0] = c[0]
    x = g.standard_normal(n) + 1j * g.standard_normal(n)
    return Case("complex", c, r, x, tags=["complex", "non-pow2"])


def impulse_cases(seed: int = 3, m: int = 7, n: int = 5) -> list[Case]:
    """One case per impulse e_j; expected = column j of T, read directly off c, r.

    Column j of T is [r[j], r[j-1], ..., r[1], c[0], c[1], ..., c[m-1-j]],
    so the expected answer is constructed by indexing alone — no arithmetic
    shared with the kernel under test.
    """
    g = _rng(seed)
    c = g.standard_normal(m)
    r = g.standard_normal(n)
    r[0] = c[0]
    cases = []
    for j in range(n):
        x = np.zeros(n)
        x[j] = 1.0
        expected = np.empty(m)
        for i in range(m):
            expected[i] = c[i - j] if i >= j else r[j - i]
        cases.append(
            Case(f"impulse_j{j}", c, r, x, expected=expected, tags=["real", "impulse"])
        )
    return cases


def tiny_cases() -> list[Case]:
    """Minimal sizes, including 1x1 — must still go through the FFT path."""
    return [
        Case("tiny_1x1", np.array([2.5]), np.array([2.5]), np.array([3.0]),
             expected=np.array([7.5]), tags=["real", "tiny"]),
        Case("tiny_2x1", np.array([1.0, -2.0]), np.array([1.0]), np.array([4.0]),
             expected=np.array([4.0, -8.0]), tags=["real", "tiny"]),
        Case("tiny_1x2", np.array([1.0]), np.array([1.0, 3.0]), np.array([2.0, 5.0]),
             expected=np.array([17.0]), tags=["real", "tiny"]),
    ]


def batch_case(seed: int = 4, m: int = 9, n: int = 6, k: int = 4) -> Case:
    """Batched matmat: X is (n, k), non-power-of-two everything."""
    g = _rng(seed)
    c = g.standard_normal(m)
    r = g.standard_normal(n)
    r[0] = c[0]
    X = g.standard_normal((n, k))
    return Case("batch_real", c, r, X, tags=["real", "batch", "non-pow2"])


def hand_case() -> Case:
    """Fully hand-computed 3x2 case: T = [[1, 4], [2, 1], [3, 2]], x = [5, 6]."""
    c = np.array([1.0, 2.0, 3.0])
    r = np.array([1.0, 4.0])
    x = np.array([5.0, 6.0])
    expected = np.array([29.0, 16.0, 27.0])  # 1*5+4*6, 2*5+1*6, 3*5+2*6
    return Case("hand_3x2", c, r, x, expected=expected, tags=["real", "hand"])


def all_standard_cases() -> list[Case]:
    """The full reusable suite used by the verification script."""
    return (
        [hand_case(), asymmetric_real_case(), complex_case(), batch_case()]
        + impulse_cases()
        + tiny_cases()
    )
