"""Numeric input boundary.

Responsibilities:
- parse raw [[re, im], ...] coefficient pairs into a complex128 array
- reject malformed / non-finite input            -> INPUT_INVALID
- strip leading (near-)zero coefficients         -> normalization
- reject the zero polynomial explicitly          -> INPUT_INVALID
- reject degree 0 (no roots to compute)          -> INPUT_INVALID
- enforce the degree resource cap                -> RESOURCE_EXHAUSTED
"""

from __future__ import annotations

from typing import Sequence

import numpy as np

from .domain import NormalizedPolynomial
from .errors import InputInvalidError, ResourceExhaustedError

# A leading coefficient is treated as zero when |c| <= LEADING_ZERO_RTOL * scale.
LEADING_ZERO_RTOL = 1e-14


def parse_coefficients(raw: Sequence[Sequence[float]], *, run_id: str = None) -> np.ndarray:
    """Turn the wire format [[re, im], ...] into a complex128 vector."""
    if raw is None or len(raw) == 0:
        raise InputInvalidError(
            "coefficient list is empty; a polynomial needs at least one coefficient",
            run_id=run_id,
        )
    values = np.empty(len(raw), dtype=np.complex128)
    for i, pair in enumerate(raw):
        try:
            re, im = float(pair[0]), float(pair[1])
        except (TypeError, IndexError, ValueError) as exc:
            raise InputInvalidError(
                f"coefficient {i} is not a [re, im] pair of numbers",
                detail={"index": i, "raw": repr(pair)},
                run_id=run_id,
            ) from exc
        if not (np.isfinite(re) and np.isfinite(im)):
            raise InputInvalidError(
                f"coefficient {i} is not finite",
                detail={"index": i, "re": re, "im": im},
                run_id=run_id,
            )
        values[i] = complex(re, im)
    return values


def normalize(coeffs: np.ndarray, *, max_degree: int, run_id: str = None) -> NormalizedPolynomial:
    """Strip leading zeros, reject degenerate input, enforce the degree cap."""
    scale = float(np.max(np.abs(coeffs)))
    if scale == 0.0:
        raise InputInvalidError(
            "zero polynomial: all coefficients are zero; every z is a root, "
            "so 'all roots' is undefined and the request is rejected",
            run_id=run_id,
        )

    tol = LEADING_ZERO_RTOL * scale
    first = 0
    while first < len(coeffs) and abs(coeffs[first]) <= tol:
        first += 1
    if first == len(coeffs):
        raise InputInvalidError(
            "zero polynomial after normalization: every coefficient is below "
            f"the leading-zero tolerance ({tol:.3e}) relative to the input scale",
            detail={"leading_zero_tol": tol},
            run_id=run_id,
        )

    trimmed = coeffs[first:]
    degree = len(trimmed) - 1
    if degree == 0:
        raise InputInvalidError(
            "constant polynomial (degree 0) has no roots to compute",
            detail={"constant": [trimmed[0].real, trimmed[0].imag]},
            run_id=run_id,
        )
    if degree > max_degree:
        raise ResourceExhaustedError(
            f"degree {degree} exceeds the configured cap of {max_degree}",
            detail={"degree": degree, "max_degree": max_degree},
            run_id=run_id,
        )

    return NormalizedPolynomial(
        coeffs=trimmed.astype(np.complex128),
        degree=degree,
        leading_dropped=first,
        scale=scale,
    )
