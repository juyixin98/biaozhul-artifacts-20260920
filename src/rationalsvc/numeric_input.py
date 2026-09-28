"""System boundary: parse and validate JSON-shaped linear-system payloads.

Contract (JSON)::

    {
      "A": [[1, "2/3", "-0.25"], ...],   # rows of exact scalars
      "b": [1, ...] | [[1,2],[3,4],...], # vector (one rhs) or matrix (many)
      "digit_budget": 1000,              # optional cap per intermediate integer
      "want": "solve" | "rank" | "both"  # optional, default "both"
    }

Accepted exact scalars (see :func:`rationalsvc.frac.parse_exact`): JSON ints,
decimal/exponent strings and ``"p/q"`` ratio strings.  JSON floats and
booleans are rejected with a distinct error class - the service must never
silently treat an approximation as exact.

Nothing past this module sees raw user types: the output is a fully typed
:class:`ParsedSystem` of ``Fraction`` matrices.
"""
from __future__ import annotations

from dataclasses import dataclass
from fractions import Fraction

from .errors import (
    InputEmpty,
    InputMalformed,
    InputNotRepresentable,
    InputPrecisionUnsupported,
    InputShapeMismatch,
    StateConflict,
)
from .frac import parse_exact

DEFAULT_DIGIT_BUDGET = 4096
MAX_DIGIT_BUDGET = 1_000_000


@dataclass(frozen=True)
class ParsedSystem:
    """Validated exact system."""

    A: list[list[Fraction]]
    b: list[list[Fraction]]  # always 2-D: one column per right-hand side
    m: int
    n: int
    rhs_count: int
    digit_budget: int
    want: str


def _parse_scalar(token: object) -> Fraction:
    try:
        return parse_exact(token)
    except TypeError as exc:
        # float/bool/unsupported type: precision-class error, not malformed.
        if isinstance(token, float):
            raise InputPrecisionUnsupported(
                "floating-point literals are refused: resend the value as an "
                "integer, a decimal string, or a ratio string",
                {"offender": repr(token)},
            ) from exc
        raise InputMalformed(
            f"unsupported scalar type: {type(token).__name__}",
            {"offender": repr(token)},
        ) from exc
    except (ValueError, ZeroDivisionError) as exc:
        raise InputNotRepresentable(
            f"scalar cannot be represented as an exact rational: {token!r}",
            {"offender": repr(token), "reason": str(exc)},
        ) from exc


def _parse_matrix(block: object, name: str) -> list[list[Fraction]]:
    if not isinstance(block, list):
        raise InputMalformed(f"{name} must be an array", {"got": type(block).__name__})
    if len(block) == 0:
        raise InputEmpty(f"{name} must contain at least one row")
    rows: list[list[Fraction]] = []
    width: int | None = None
    for i, raw_row in enumerate(block):
        if not isinstance(raw_row, list) or len(raw_row) == 0:
            raise InputMalformed(
                f"{name}[{i}] must be a non-empty array", {"row_index": i}
            )
        if width is None:
            width = len(raw_row)
        elif len(raw_row) != width:
            raise InputShapeMismatch(
                f"{name} has ragged rows",
                {"row_index": i, "expected": width, "got": len(raw_row)},
            )
        rows.append([_parse_scalar(v) for v in raw_row])
    return rows


def _coerce_b(block: object, m: int) -> list[list[Fraction]]:
    """Accept b as a vector or a matrix; always return a 2-D row-major list."""
    if not isinstance(block, list) or len(block) == 0:
        raise InputMalformed("b must be a non-empty array")

    vector_form = all(not isinstance(v, list) for v in block)
    matrix_form = all(isinstance(v, list) for v in block)
    if not (vector_form or matrix_form):
        raise InputMalformed(
            "b must be either a flat vector or an array of row vectors"
        )

    if vector_form:
        if len(block) != m:
            raise InputShapeMismatch(
                "b length does not match A row count",
                {"expected": m, "got": len(block)},
            )
        return [[_parse_scalar(v)] for v in block]

    rows = _parse_matrix(block, "b")
    if len(rows) != m:
        raise InputShapeMismatch(
            "b row count does not match A row count",
            {"expected": m, "got": len(rows)},
        )
    return rows


def parse_request(payload: object) -> ParsedSystem:
    """Validate and parse an entire request payload."""
    if not isinstance(payload, dict):
        raise InputMalformed("request body must be a JSON object",
                             {"got": type(payload).__name__})
    if "A" not in payload:
        raise InputMalformed("missing required field 'A'")
    if "b" not in payload:
        raise InputMalformed("missing required field 'b'")

    A = _parse_matrix(payload["A"], "A")
    m, n = len(A), len(A[0])
    b = _coerce_b(payload["b"], m)

    budget = payload.get("digit_budget", DEFAULT_DIGIT_BUDGET)
    if not isinstance(budget, int) or isinstance(budget, bool):
        raise InputMalformed("digit_budget must be an integer")
    if budget <= 0:
        raise StateConflict(
            "digit_budget must be positive", {"digit_budget": budget}
        )
    if budget > MAX_DIGIT_BUDGET:
        raise StateConflict(
            "digit_budget exceeds server ceiling",
            {"digit_budget": budget, "ceiling": MAX_DIGIT_BUDGET},
        )

    want = payload.get("want", "both")
    if want not in {"solve", "rank", "both"}:
        raise StateConflict(
            "want must be one of: solve, rank, both", {"got": want}
        )

    return ParsedSystem(A=A, b=b, m=m, n=n, rhs_count=len(b[0]),
                        digit_budget=budget, want=want)
