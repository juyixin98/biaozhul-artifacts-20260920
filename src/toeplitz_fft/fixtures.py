"""Reusable deterministic synthetic fixtures.

Every fixture is generated locally from a seed - no external data or
accounts.  The important cases required for evidence are first-class:

* asymmetric (non-symmetric) Toeplitz matrices;
* non-power-of-two lengths (and lengths just below/above fast FFT sizes);
* complex coefficients/vectors (non-Hermitian);
* impulse vectors (``e_k``), where ``T e_k`` is a matrix column and the
  answer can be stated structurally as well as numerically.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any

import numpy as np

DEFAULT_SEED = 20260927


@dataclass(frozen=True)
class NumpyFixture:
    case_id: str
    first_column: np.ndarray
    first_row: np.ndarray
    vectors: np.ndarray
    description: str

    @property
    def n(self) -> int:
        return int(self.first_column.shape[0])

    @property
    def batch(self) -> int:
        return int(self.vectors.shape[0])


def _rng(seed: int) -> np.random.Generator:
    return np.random.default_rng(seed)


def make_asymmetric_real(n: int = 13, *, seed: int = DEFAULT_SEED,
                         batch: int = 3) -> NumpyFixture:
    """Non-symmetric real Toeplitz: r[1:] deliberately differs from c[1:]."""
    rng = _rng(seed)
    scale = 1.0
    c0 = rng.normal(0, scale)
    col = np.concatenate([[c0], rng.normal(0, scale, n - 1)])
    row = np.concatenate([[c0], rng.normal(0, scale, n - 1) + 5.0])
    vectors = rng.normal(0, 1, (batch, n))
    return NumpyFixture(
        case_id=f"asym_real_n{n}",
        first_column=col,
        first_row=row,
        vectors=vectors,
        description=("asymmetric real Toeplitz, non-power-of-two n; "
                     "first_row shifted by +5 so T != T.T"),
    )


def make_complex_non_hermitian(n: int = 17, *, seed: int = DEFAULT_SEED + 1,
                               batch: int = 2) -> NumpyFixture:
    """Complex, non-Hermitian Toeplitz (independent real/imagary draws)."""
    rng = _rng(seed)
    c0 = complex(rng.normal(), rng.normal())
    col = np.concatenate([[c0], rng.normal(size=n - 1) + 1j * rng.normal(
        size=n - 1)])
    row = np.concatenate([[c0], rng.normal(size=n - 1) + 1j * rng.normal(
        size=n - 1)])
    vectors = (rng.normal(size=(batch, n))
               + 1j * rng.normal(size=(batch, n)))
    return NumpyFixture(
        case_id=f"complex_nonhermitian_n{n}",
        first_column=col,
        first_row=row,
        vectors=vectors,
        description="complex non-Hermitian Toeplitz, non-power-of-two n",
    )


def make_impulse_case(n: int = 15, *, seed: int = DEFAULT_SEED + 2,
                      positions: tuple[int, ...] = (0, 7, 14)
                      ) -> NumpyFixture:
    """Stacked impulse vectors e_k: output must equal columns of T.

    For a Toeplitz matrix ``T``, column ``k`` is
    ``[ zeros(k); c ; zeros(n-1-k) ]`` with the upper entries filled from
    ``r``:  ``(T e_k)[i] = T[i, k]`` - the fixture's vectors are the
    standard basis and any correct kernel must reproduce that exactly (up to
    rounding), giving a structural assertion independent of multiplication.
    """
    rng = _rng(seed)
    c0 = rng.normal()
    col = np.concatenate([[c0], rng.normal(size=n - 1)])
    row = np.concatenate([[c0], rng.normal(size=n - 1) - 2.0])
    vectors = np.zeros((len(positions), n))
    for row_idx, k in enumerate(positions):
        vectors[row_idx, k] = 1.0
    return NumpyFixture(
        case_id=f"impulse_real_n{n}",
        first_column=col,
        first_row=row,
        vectors=vectors,
        description=f"impulse vectors e_k at k={positions}; outputs are T columns",
    )


def make_tiny_case(n: int = 1) -> NumpyFixture:
    """Smallest possible problem (n=1) for the explainable tiny path."""
    col = np.array([3.5])
    row = np.array([3.5])
    vectors = np.array([[2.0], [-4.0]])
    return NumpyFixture(
        case_id="tiny_n1",
        first_column=col,
        first_row=row,
        vectors=vectors,
        description="n=1 scalar multiplication, explainable path",
    )


def expected_impulse_columns(col: np.ndarray, row: np.ndarray,
                             positions: tuple[int, ...]) -> np.ndarray:
    """Structural expectation for ``T e_k`` built straight from the definition."""
    n = col.shape[0]
    out = np.zeros((len(positions), n), dtype=col.dtype)
    for b, k in enumerate(positions):
        for i in range(n):
            out[b, i] = col[i - k] if i >= k else row[k - i]
    return out


def all_cases() -> list[NumpyFixture]:
    return [
        make_tiny_case(1),
        make_asymmetric_real(13),
        make_complex_non_hermitian(17),
        make_impulse_case(15),
    ]


# --------------------------------------------------------------------------- #
# Wire-format conversion (service payloads)
# --------------------------------------------------------------------------- #

def _complex_pairs(arr: np.ndarray) -> list[list[float]]:
    return [[float(z.real), float(z.imag)] for z in arr]


def fixture_to_payload(fx: NumpyFixture, *, mode: str | None = None,
                       precision: str = "double",
                       kernel: str = "auto") -> dict[str, Any]:
    complex_mode = mode == "complex" or (mode is None and np.iscomplexobj(
        fx.first_column))
    if complex_mode:
        payload = {
            "first_column": _complex_pairs(fx.first_column),
            "first_row": _complex_pairs(fx.first_row),
            "vectors": [_complex_pairs(v) for v in fx.vectors],
            "mode": "complex",
        }
    else:
        col = np.real_if_close(fx.first_column).astype(np.float64)
        row = np.real_if_close(fx.first_row).astype(np.float64)
        vecs = np.real_if_close(fx.vectors).astype(np.float64)
        payload = {
            "first_column": [float(v) for v in col],
            "first_row": [float(v) for v in row],
            "vectors": [[float(v) for v in vec] for vec in vecs],
            "mode": "real",
        }
    payload["precision"] = precision
    payload["kernel"] = kernel
    return payload
