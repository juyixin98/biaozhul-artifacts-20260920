"""Parsing and validation of numerical request payloads.

Entries may be JSON numbers or strings. Strings are parsed as *exact* decimals
by mpmath, which is the only way to carry more than ~16 significant digits
through JSON (JSON numbers are float64 on arrival in Python).
"""

from __future__ import annotations

from dataclasses import dataclass

from mpmath import matrix as mp_matrix

from .numerical import to_mp, workprec

MAX_DIMENSION = 256
MAX_NRHS = 32


class PayloadError(ValueError):
    """A 400-class error: the request payload is malformed or unsupported."""


@dataclass(frozen=True)
class ParsedSystem:
    a: mp_matrix
    b: mp_matrix
    n: int
    nrhs: int
    string_entries: int


def _parse_entry(value: object, counters: dict[str, int]):
    if isinstance(value, str):
        counters["strings"] += 1
    return to_mp(value)


def parse_system(payload: dict) -> ParsedSystem:
    """Validate and parse a request into high-precision A and B."""
    if not isinstance(payload, dict):
        raise PayloadError("request body must be a JSON object")
    a_rows = payload.get("A")
    b_rows = payload.get("B")
    if a_rows is None or b_rows is None:
        raise PayloadError("both 'A' (matrix) and 'B' (matrix or column) are required")

    counters = {"strings": 0}
    a_mp, n = _parse_square(a_rows, counters)
    b_mp, nrhs = _parse_b(b_rows, n, counters)
    return ParsedSystem(a=a_mp, b=b_mp, n=n, nrhs=nrhs, string_entries=counters["strings"])


def _parse_square(rows: object, counters: dict[str, int] | None = None):
    counters = counters if counters is not None else {"strings": 0}
    if not isinstance(rows, list) or not rows or not all(isinstance(r, list) for r in rows):
        raise PayloadError("'A' must be a non-empty list of row lists")
    n = len(rows)
    if n > MAX_DIMENSION:
        raise PayloadError(f"matrix dimension {n} exceeds maximum {MAX_DIMENSION}")
    width = {len(r) for r in rows}
    if width != {n}:
        raise PayloadError(f"'A' must be square; got rows of widths {sorted(width)} for n={n}")
    with workprec(120):
        a = mp_matrix(n, n)
        for i, row in enumerate(rows):
            for j, value in enumerate(row):
                try:
                    a[i, j] = _parse_entry(value, counters)
                except (ValueError, TypeError) as exc:
                    raise PayloadError(f"A[{i}][{j}]: {exc}") from exc
    return a, n


def _parse_b(rows: object, n: int, counters: dict[str, int] | None = None):
    counters = counters if counters is not None else {"strings": 0}
    # Accept either a flat column vector or a list-of-rows matrix.
    flat_vector = (
        isinstance(rows, list)
        and rows
        and not any(isinstance(v, list) for v in rows)
    )
    with workprec(120):
        if flat_vector:
            if len(rows) != n:
                raise PayloadError(f"'B' vector length {len(rows)} != n={n}")
            b = mp_matrix(n, 1)
            for i, value in enumerate(rows):
                try:
                    b[i, 0] = _parse_entry(value, counters)
                except (ValueError, TypeError) as exc:
                    raise PayloadError(f"B[{i}]: {exc}") from exc
            return b, 1

        if not isinstance(rows, list) or not rows or not all(isinstance(r, list) for r in rows):
            raise PayloadError("'B' must be a vector or a list of row lists")
        if len(rows) != n:
            raise PayloadError(f"'B' has {len(rows)} rows, expected n={n}")
        widths = {len(r) for r in rows}
        if len(widths) != 1:
            raise PayloadError("'B' rows must all have the same length")
        nrhs = widths.pop()
        if not 1 <= nrhs <= MAX_NRHS:
            raise PayloadError(f"number of right-hand sides must be in [1, {MAX_NRHS}]")
        b = mp_matrix(n, nrhs)
        for i, row in enumerate(rows):
            for j, value in enumerate(row):
                try:
                    b[i, j] = _parse_entry(value, counters)
                except (ValueError, TypeError) as exc:
                    raise PayloadError(f"B[{i}][{j}]: {exc}") from exc
    return b, nrhs
