"""Exact numeric input parsing and matrix validation.

Nothing in this module ever produces a binary float:

* ``int`` / ``Fraction`` / ``Decimal`` are accepted directly (exact).
* Strings are parsed with :class:`fractions.Fraction`, which handles integer,
  ``"p/q"``, decimal (``"3.14"``) and exponent (``"1.5e3"``) forms exactly.
* Python ``float`` / ``complex`` / mpmath ``mpf`` / NaN / infinity are
  *rejected* -- the service contract forbids silently converting to float.

Matrices are returned as lists of :class:`~fractions.Fraction`, which is the
sole element type used by the computation kernel.
"""

from __future__ import annotations

from decimal import Decimal, InvalidOperation
from fractions import Fraction
from typing import Any, Iterable

from .errors import InputError

# Types that carry exact rational information.
_EXACT_TYPES = (int, Fraction, Decimal)


def parse_exact(value: Any, *, path: str = "value") -> Fraction:
    """Parse one scalar into an exact :class:`Fraction`.

    ``bool`` is rejected even though it is an ``int`` subclass.
    """
    if isinstance(value, bool):
        raise InputError(
            f"{path}: booleans are not accepted as matrix entries",
            code="INVALID_NUMBER",
            details={"path": path, "value": value},
        )
    if isinstance(value, Fraction):
        result = value
    elif isinstance(value, int):
        result = Fraction(value)
    elif isinstance(value, Decimal):
        result = _fraction_from_decimal(value, path)
    elif isinstance(value, str):
        result = _fraction_from_string(value, path)
    else:
        raise InputError(
            f"{path}: only exact values are accepted (int, str of an exact "
            f"decimal/fraction, fractions.Fraction, decimal.Decimal); "
            f"got {type(value).__name__} -- float/mpf conversion is forbidden",
            code="NON_EXACT_NUMBER",
            details={"path": path, "type": type(value).__name__},
        )
    return result


def _fraction_from_string(raw: str, path: str) -> Fraction:
    text = raw.strip()
    if not text:
        raise InputError(
            f"{path}: empty string is not a number",
            code="INVALID_NUMBER",
            details={"path": path, "value": raw},
        )
    lowered = text.lower()
    if lowered in {"nan", "inf", "-inf", "+inf", "infinity", "-infinity"}:
        raise InputError(
            f"{path}: non-finite value {text!r} is not a rational number",
            code="NON_FINITE_NUMBER",
            details={"path": path, "value": raw},
        )
    try:
        return Fraction(text)
    except (ValueError, ZeroDivisionError):
        # Fraction accepts '3.14' and '1e3' on 3.12, but be defensive across
        # versions: route decimals through Decimal.
        try:
            return _fraction_from_decimal(Decimal(text), path, raw=raw)
        except InvalidOperation:
            raise InputError(
                f"{path}: {text!r} is not an exact integer, decimal or fraction",
                code="INVALID_NUMBER",
                details={"path": path, "value": raw},
            )


def _fraction_from_decimal(
    value: Decimal, path: str, raw: Any = None
) -> Fraction:
    if not value.is_finite():
        raise InputError(
            f"{path}: non-finite decimal is not a rational number",
            code="NON_FINITE_NUMBER",
            details={"path": path, "value": str(raw if raw is not None else value)},
        )
    return Fraction(value)


def parse_vector(values: Iterable[Any], *, path: str) -> list[Fraction]:
    """Parse a flat iterable of scalars."""
    parsed: list[Fraction] = []
    for j, v in enumerate(values):
        parsed.append(parse_exact(v, path=f"{path}[{j}]"))
    if not parsed:
        raise InputError(
            f"{path}: vector must not be empty",
            code="EMPTY_MATRIX",
            details={"path": path},
        )
    return parsed


def parse_matrix(rows: Any, *, name: str = "A") -> list[list[Fraction]]:
    """Validate and parse a 2-D rectangular matrix.

    Returns a fresh list-of-lists of :class:`Fraction`; the caller may mutate
    the returned matrix freely.
    """
    if not isinstance(rows, list) or (rows and not all(isinstance(r, list) for r in rows)):
        raise InputError(
            f"{name}: matrix must be a JSON array of arrays",
            code="INVALID_MATRIX_SHAPE",
            details={"path": name},
        )
    if not rows:
        raise InputError(
            f"{name}: matrix must have at least one row",
            code="EMPTY_MATRIX",
            details={"path": name},
        )
    width = len(rows[0])
    if width == 0:
        raise InputError(
            f"{name}: matrix rows must not be empty",
            code="EMPTY_MATRIX",
            details={"path": name},
        )
    matrix: list[list[Fraction]] = []
    for i, row in enumerate(rows):
        if len(row) != width:
            raise InputError(
                f"{name}: ragged matrix: row {i} has {len(row)} entries, "
                f"expected {width}",
                code="RAGGED_MATRIX",
                details={"path": f"{name}[{i}]", "width": width, "got": len(row)},
            )
        matrix.append(parse_vector(row, path=f"{name}[{i}]"))
    return matrix


def shape(matrix: list[list[Fraction]]) -> tuple[int, int]:
    return len(matrix), len(matrix[0])
