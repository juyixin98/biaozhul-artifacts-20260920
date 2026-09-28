"""Framework-agnostic numeric input parsing and validation.

The public HTTP layer speaks pydantic; the kernel speaks exact
:class:`~fractions.Fraction`. This module owns the boundary between the two:

* parse integers / decimal strings / fraction strings *exactly*;
* reject binary floats and other non-exact representations outright;
* enforce the degree and coefficient-size budgets;
* produce a redacted fingerprint for safe logging.
"""

from __future__ import annotations

from dataclasses import dataclass
from fractions import Fraction
from typing import Any, Mapping, Sequence

from config.settings import BudgetConfig
from root_isolator.errors import ErrorCategory, IsolationError
from root_isolator.kernel.polynomial import RationalPoly

# bool is a subclass of int; a literal true/false coefficient is almost always
# a client mistake rather than a deliberate 1/0, so reject it explicitly.
_EXACT_INT_TYPES: tuple[type, ...] = (int,)


@dataclass(frozen=True)
class ParsedInput:
    """Result of a successful parse."""

    polynomial: RationalPoly
    is_zero: bool
    declared_degree: int
    effective_degree: int
    max_coefficient_bits: int
    nonzero_terms: int
    fingerprint: str


def parse_fraction(value: Any, *, index: int | str) -> Fraction:
    """Parse one coefficient into an exact :class:`Fraction`.

    Accepted: :class:`int`, decimal strings (``"1.5"``, ``"-0.25"``),
    fraction strings (``"3/7"``) and exponent strings (``"1e-3"``).

    Rejected with :class:`IsolationError`: :class:`float`/``complex``/mpmath
    values (non-exact), non-numeric types, malformed strings.
    """

    if isinstance(value, bool) or not isinstance(value, _EXACT_INT_TYPES + (str, Fraction)):
        kind = type(value).__name__
        if isinstance(value, (float,)) or (
            type(value).__name__ in {"mpf", "mpc"} and kind not in {"int", "str", "Fraction"}
        ):
            raise IsolationError(
                ErrorCategory.NON_EXACT_COEFFICIENT,
                f"coefficient at position {index} is a binary float ({kind}); "
                "send an integer, a decimal string or a fraction string instead "
                "so coefficients remain exact",
                state={"index": index, "type": kind},
            )
        raise IsolationError(
            ErrorCategory.MALFORMED_COEFFICIENTS,
            f"coefficient at position {index} has unsupported type {kind}; "
            "expected integer or exact decimal/fraction string",
            state={"index": index, "type": kind},
        )

    if isinstance(value, Fraction):
        return value
    if isinstance(value, int):
        return Fraction(value)

    text = value.strip()
    if not text:
        raise IsolationError(
            ErrorCategory.MALFORMED_COEFFICIENTS,
            f"coefficient at position {index} is an empty string",
            state={"index": index},
        )
    try:
        return Fraction(text)
    except (ValueError, ZeroDivisionError) as exc:
        raise IsolationError(
            ErrorCategory.MALFORMED_COEFFICIENTS,
            f"coefficient at position {index} is not a valid exact rational: {text!r}",
            state={"index": index},
        ) from exc


def _check_budget(fractions: Sequence[Fraction], budget: BudgetConfig) -> int:
    declared_degree = len(fractions) - 1
    if declared_degree > budget.max_degree:
        raise IsolationError(
            ErrorCategory.DEGREE_EXCEEDED,
            f"polynomial degree {declared_degree} exceeds budget max_degree={budget.max_degree}",
            state={"degree": declared_degree, "max_degree": budget.max_degree},
        )
    max_bits = 0
    for i, frac in enumerate(fractions):
        bits = max(abs(frac.numerator).bit_length(), frac.denominator.bit_length())
        if bits > budget.max_coefficient_bits:
            raise IsolationError(
                ErrorCategory.COEFFICIENT_TOO_LARGE,
                f"coefficient at position {i} uses {bits} bits, exceeding "
                f"max_coefficient_bits={budget.max_coefficient_bits}",
                state={"index": i, "bits": bits, "max_bits": budget.max_coefficient_bits},
            )
        max_bits = max(max_bits, bits)
    return max_bits


def _fingerprint(fractions: Sequence[Fraction]) -> str:
    """Stable non-reversible descriptor safe for logs.

    Includes degree, term count, coefficient count and a cheap FNV-style hash
    of the exact coefficient tuple. It never contains the payload itself.
    """

    basis = 0xCBF29CE484222325
    prime = 0x100000001B3
    mask = (1 << 64) - 1
    digest = basis
    for frac in fractions:
        for token in (frac.numerator, frac.denominator):
            digest ^= token & mask
            digest = (digest * prime) & mask
    nonzero = sum(1 for f in fractions if f != 0)
    return f"deg={len(fractions) - 1};terms={nonzero};hash64={digest:016x}"


def parse_dense_coefficients(
    raw: Sequence[Any],
    *,
    order: str = "descending",
    budget: BudgetConfig,
) -> ParsedInput:
    """Parse a dense coefficient list.

    ``order`` is ``"descending"`` (``[a_n, ..., a_1, a_0]``, the conventional
    written order) or ``"ascending"`` (``[a_0, a_1, ..., a_n]``).
    """

    if not isinstance(raw, (list, tuple)):
        raise IsolationError(
            ErrorCategory.MALFORMED_COEFFICIENTS,
            f"coefficient payload must be a list, got {type(raw).__name__}",
            state={"type": type(raw).__name__},
        )
    if order not in {"descending", "ascending"}:
        raise IsolationError(
            ErrorCategory.MALFORMED_COEFFICIENTS,
            f"order must be 'descending' or 'ascending', got {order!r}",
            state={"order": order},
        )
    if len(raw) == 0:
        raise IsolationError(
            ErrorCategory.EMPTY_COEFFICIENTS,
            "coefficient list is empty; send at least one coefficient",
            state={},
        )

    fractions = [parse_fraction(v, index=i) for i, v in enumerate(raw)]
    if order == "descending":
        fractions.reverse()

    max_bits = _check_budget(fractions, budget)
    poly = RationalPoly(fractions)
    nonzero = sum(1 for f in fractions if f != 0)
    return ParsedInput(
        polynomial=poly,
        is_zero=poly.is_zero,
        declared_degree=len(fractions) - 1,
        effective_degree=poly.degree,
        max_coefficient_bits=max_bits,
        nonzero_terms=nonzero,
        fingerprint=_fingerprint(fractions),
    )


def parse_sparse_coefficients(
    raw: Mapping[str, Any],
    *,
    budget: BudgetConfig,
) -> ParsedInput:
    """Parse a sparse ``{power: coefficient}`` mapping (JSON objects have string keys)."""

    if not isinstance(raw, Mapping):
        raise IsolationError(
            ErrorCategory.MALFORMED_COEFFICIENTS,
            f"sparse payload must be an object mapping powers to coefficients, "
            f"got {type(raw).__name__}",
            state={"type": type(raw).__name__},
        )
    if len(raw) == 0:
        raise IsolationError(
            ErrorCategory.EMPTY_COEFFICIENTS,
            "coefficient mapping is empty; send at least one power/coefficient pair",
            state={},
        )

    pairs: dict[int, Fraction] = {}
    max_power = -1
    for key, value in raw.items():
        try:
            power = int(str(key).strip())
        except ValueError as exc:
            raise IsolationError(
                ErrorCategory.MALFORMED_COEFFICIENTS,
                f"coefficient power {key!r} is not a non-negative integer",
                state={"power": str(key)},
            ) from exc
        if power < 0:
            raise IsolationError(
                ErrorCategory.MALFORMED_COEFFICIENTS,
                f"coefficient power {power} is negative",
                state={"power": power},
            )
        if power in pairs:
            raise IsolationError(
                ErrorCategory.MALFORMED_COEFFICIENTS,
                f"coefficient power {power} is given more than once",
                state={"power": power},
            )
        pairs[power] = parse_fraction(value, index=power)
        max_power = max(max_power, power)

    if max_power > budget.max_degree:
        raise IsolationError(
            ErrorCategory.DEGREE_EXCEEDED,
            f"polynomial degree {max_power} exceeds budget max_degree={budget.max_degree}",
            state={"degree": max_power, "max_degree": budget.max_degree},
        )

    dense = [pairs.get(k, Fraction(0)) for k in range(max_power + 1)]
    max_bits = _check_budget(dense, budget)
    poly = RationalPoly(dense)
    nonzero = sum(1 for f in dense if f != 0)
    return ParsedInput(
        polynomial=poly,
        is_zero=poly.is_zero,
        declared_degree=max_power,
        effective_degree=poly.degree,
        max_coefficient_bits=max_bits,
        nonzero_terms=nonzero,
        fingerprint=_fingerprint(dense),
    )
