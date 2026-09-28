"""Numeric input parsing and validation - the system boundary.

Wire payloads (JSON-decoded Python structures) are converted here into
validated :class:`Problem` instances.  Nothing past this layer trusts
external data: shapes, finiteness, sizes and the shared ``T[0,0]`` element
are all checked and failures get an explicit :class:`InputError` category.

Real/complex mode and output precision are *explicit* request fields:
    mode      = "real"    -> float32/float64 arithmetic and real FFT (rfft)
    mode      = "complex" -> complex64/complex128 arithmetic
    precision = "single"  -> 32-bit storage and FFT work
    precision = "double"  -> 64-bit storage and FFT work
"""

from __future__ import annotations

from dataclasses import dataclass
from numbers import Real
from typing import Any, Literal

import numpy as np

from .config import SETTINGS
from .errors import ErrorCode, InputError

Mode = Literal["real", "complex"]
Precision = Literal["double", "single"]
KernelChoice = Literal["auto", "embedding_fft", "tiny_explicit"]

_REAL_DTYPES: dict[str, np.dtype] = {
    "double": np.dtype(np.float64),
    "single": np.dtype(np.float32),
}
_COMPLEX_DTYPES: dict[str, np.dtype] = {
    "double": np.dtype(np.complex128),
    "single": np.dtype(np.complex64),
}


@dataclass(frozen=True)
class Problem:
    """A validated Toeplitz multiplication request.

    Attributes:
        first_column: ``c`` length n, ``T[i, 0] = c[i]``.
        first_row:    ``r`` length n, ``T[0, j] = r[j]``, ``r[0] == c[0]``.
        vectors:      shape ``(batch, n)``, one vector per row.
        mode / precision / kernel: explicit execution parameters.
    """

    first_column: np.ndarray
    first_row: np.ndarray
    vectors: np.ndarray
    mode: Mode
    precision: Precision
    kernel: KernelChoice

    @property
    def n(self) -> int:
        return int(self.first_column.shape[0])

    @property
    def batch(self) -> int:
        return int(self.vectors.shape[0])

    @property
    def is_complex(self) -> bool:
        return self.mode == "complex"

    @property
    def value_dtype(self) -> np.dtype:
        """Dtype of coefficients/vectors (real or complex storage)."""
        return self.first_column.dtype

    @property
    def result_dtype(self) -> np.dtype:
        return self.value_dtype


def real_dtype(precision: Precision) -> np.dtype:
    return _REAL_DTYPES[precision]


def complex_dtype(precision: Precision) -> np.dtype:
    return _COMPLEX_DTYPES[precision]


# --------------------------------------------------------------------------- #
# Sequence parsing
# --------------------------------------------------------------------------- #

def _is_real_number(x: Any) -> bool:
    # bool is a Real subclass; reject it to avoid True/False sneaking in.
    return isinstance(x, Real) and not isinstance(x, bool)


def _parse_real_vector(value: Any, name: str, dtype: np.dtype) -> np.ndarray:
    if not isinstance(value, list) or not value:
        raise InputError(
            ErrorCode.EMPTY_INPUT if isinstance(value, list) else ErrorCode.SHAPE_INVALID,
            f"{name} must be a non-empty list of numbers",
        )
    if not all(_is_real_number(x) for x in value):
        raise InputError(
            ErrorCode.COMPLEX_SPEC_INVALID,
            f"{name} contains non-numeric entries in real mode "
            "(pairs/objects are only valid in complex mode)",
        )
    arr = np.asarray(value, dtype=np.float64)
    if arr.ndim != 1:
        raise InputError(ErrorCode.SHAPE_INVALID, f"{name} must be 1-dimensional")
    return _finish(arr.astype(dtype, copy=False), name)


def _pair_to_complex(pair: Any) -> complex:
    if not (isinstance(pair, list) and len(pair) == 2) or not all(
        _is_real_number(x) for x in pair
    ):
        raise ValueError("expected [real, imag] pair")
    return complex(float(pair[0]), float(pair[1]))


def _parse_complex_vector(value: Any, name: str, dtype: np.dtype) -> np.ndarray:
    """Complex encodings: list of [re, im] pairs, or {real: [...], imag: [...]}."""
    if isinstance(value, dict):
        real_part = value.get("real")
        imag_part = value.get("imag", [])
        if not isinstance(real_part, list) or not isinstance(imag_part, list):
            raise InputError(
                ErrorCode.COMPLEX_SPEC_INVALID,
                f"{name} object must contain numeric lists 'real' and 'imag'",
            )
        if not real_part:
            raise InputError(ErrorCode.EMPTY_INPUT, f"{name}.real must be non-empty")
        if imag_part and len(imag_part) != len(real_part):
            raise InputError(
                ErrorCode.COMPLEX_SPEC_INVALID,
                f"{name}: real/imag parts differ in length "
                f"({len(real_part)} != {len(imag_part)})",
            )
        if not all(_is_real_number(x) for x in real_part + imag_part):
            raise InputError(
                ErrorCode.COMPLEX_SPEC_INVALID,
                f"{name} object parts must contain real numbers only",
            )
        imag_part = imag_part or [0.0] * len(real_part)
        arr = np.asarray(real_part, dtype=np.float64) + 1j * np.asarray(
            imag_part, dtype=np.float64
        )
    elif isinstance(value, list) and value:
        try:
            arr = np.asarray([_pair_to_complex(x) for x in value], dtype=np.complex128)
        except ValueError:
            raise InputError(
                ErrorCode.COMPLEX_SPEC_INVALID,
                f"{name} entries must be [real, imag] pairs in complex mode",
            ) from None
    elif isinstance(value, list):
        raise InputError(ErrorCode.EMPTY_INPUT, f"{name} must be non-empty")
    else:
        raise InputError(
            ErrorCode.COMPLEX_SPEC_INVALID,
            f"{name} must be pairs or a real/imag object in complex mode",
        )
    if arr.ndim != 1:
        raise InputError(ErrorCode.SHAPE_INVALID, f"{name} must be 1-dimensional")
    return _finish(arr.astype(dtype, copy=False), name)


def _finish(arr: np.ndarray, name: str) -> np.ndarray:
    if not np.all(np.isfinite(arr).astype(bool) if np.iscomplexobj(arr)
                  else np.isfinite(arr)):
        raise InputError(
            ErrorCode.NONFINITE_INPUT, f"{name} contains NaN or infinite values"
        )
    return np.ascontiguousarray(arr)


def parse_sequence(value: Any, name: str, mode: Mode, precision: Precision) -> np.ndarray:
    if mode == "real":
        return _parse_real_vector(value, name, real_dtype(precision))
    return _parse_complex_vector(value, name, complex_dtype(precision))


# --------------------------------------------------------------------------- #
# Vector batches
# --------------------------------------------------------------------------- #

def _is_vector_of_numbers(value: Any) -> bool:
    return (
        isinstance(value, list)
        and len(value) > 0
        and all(_is_real_number(x) for x in value)
    )


def _is_vector_of_pairs(value: Any) -> bool:
    return (
        isinstance(value, list)
        and len(value) > 0
        and all(isinstance(x, list) and len(x) == 2 for x in value)
    )


def parse_vectors(value: Any, mode: Mode, precision: Precision) -> np.ndarray:
    """Return ``(batch, n)`` array; a bare 1-D vector means batch == 1."""
    if not isinstance(value, list) or not value:
        raise InputError(
            ErrorCode.EMPTY_INPUT if isinstance(value, list) else ErrorCode.SHAPE_INVALID,
            "vectors must be a non-empty list (1-D vector or list of vectors)",
        )

    if mode == "real":
        if _is_vector_of_numbers(value):
            rows = [np.asarray(value, dtype=np.float64)]
        elif all(isinstance(row, list) and row for row in value):
            rows = []
            for i, row in enumerate(value):
                if not all(_is_real_number(x) for x in row):
                    raise InputError(
                        ErrorCode.COMPLEX_SPEC_INVALID,
                        f"vectors[{i}] contains non-numeric entries in real mode",
                    )
                rows.append(np.asarray(row, dtype=np.float64))
        else:
            raise InputError(
                ErrorCode.SHAPE_INVALID,
                "vectors must be a number list or a list of number lists",
            )
        dtype = real_dtype(precision)
    else:
        if _is_vector_of_pairs(value):
            rows = [
                np.asarray([_pair_to_complex(x) for x in value], dtype=np.complex128)
            ]
        elif isinstance(value, list) and all(isinstance(row, (list, dict)) and row
                                             for row in value):
            rows = []
            for i, row in enumerate(value):
                try:
                    rows.append(
                        _parse_complex_vector(row, f"vectors[{i}]",
                                              complex_dtype(precision))
                        .astype(np.complex128, copy=False)
                    )
                except InputError as exc:
                    if exc.code is not ErrorCode.EMPTY_INPUT:
                        raise
                    raise InputError(
                        ErrorCode.SHAPE_INVALID,
                        f"vectors[{i}] must be a non-empty complex sequence",
                    ) from None
        else:
            raise InputError(
                ErrorCode.COMPLEX_SPEC_INVALID,
                "complex vectors must be pairs, a real/imag object, "
                "or a list of those",
            )
        dtype = complex_dtype(precision)

    widths = {row.shape[0] for row in rows}
    if len(widths) != 1:
        raise InputError(
            ErrorCode.BATCH_LENGTH_MISMATCH,
            f"all vectors must share one length, got widths {sorted(widths)}",
        )
    batch = np.stack(rows, axis=0).astype(dtype, copy=False)
    return _finish(np.ascontiguousarray(batch), "vectors")


# --------------------------------------------------------------------------- #
# Top-level validation
# --------------------------------------------------------------------------- #

def parse_problem(payload: dict[str, Any]) -> Problem:
    mode = payload.get("mode", "real")
    precision = payload.get("precision", "double")
    kernel = payload.get("kernel", "auto")
    if mode not in ("real", "complex"):
        raise InputError(
            ErrorCode.COMPLEX_SPEC_INVALID, "mode must be 'real' or 'complex'"
        )
    if precision not in ("double", "single"):
        raise InputError(
            ErrorCode.PRECISION_UNSUPPORTED, "precision must be 'double' or 'single'"
        )
    if kernel not in ("auto", "embedding_fft", "tiny_explicit"):
        raise InputError(ErrorCode.BAD_REQUEST,
                         "kernel must be 'auto', 'embedding_fft' or 'tiny_explicit'")

    col = parse_sequence(payload.get("first_column"), "first_column", mode, precision)
    row = parse_sequence(payload.get("first_row"), "first_row", mode, precision)
    if col.shape[0] != row.shape[0]:
        raise InputError(
            ErrorCode.SHAPE_INVALID,
            f"first_column/first_row lengths differ ({col.shape[0]} != {row.shape[0]})",
        )
    n = col.shape[0]
    if not np.isclose(col[0], row[0], rtol=0, atol=0, equal_nan=False):
        raise InputError(
            ErrorCode.FIRST_ELEMENT_MISMATCH,
            "shared element must match: first_column[0] != first_row[0]",
            details={"column_0": [round(float(col[0].real), 12),
                                  round(float(col[0].imag), 12)],
                     "row_0": [round(float(row[0].real), 12),
                               round(float(row[0].imag), 12)]},
        )
    if n > SETTINGS.max_n:
        raise InputError(
            ErrorCode.SIZE_LIMIT, f"n={n} exceeds TOEPLITZ_MAX_N={SETTINGS.max_n}"
        )

    vectors = parse_vectors(payload.get("vectors"), mode, precision)
    if vectors.shape[1] != n:
        raise InputError(
            ErrorCode.BATCH_LENGTH_MISMATCH,
            f"vector length {vectors.shape[1]} does not match Toeplitz size {n}",
        )
    if vectors.shape[0] > SETTINGS.max_batch:
        raise InputError(
            ErrorCode.SIZE_LIMIT,
            f"batch={vectors.shape[0]} exceeds TOEPLITZ_MAX_BATCH="
            f"{SETTINGS.max_batch}",
        )
    return Problem(
        first_column=col,
        first_row=row,
        vectors=vectors,
        mode=mode,
        precision=precision,
        kernel=kernel,
    )
