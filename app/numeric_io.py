"""Numeric input boundary: parsing and validation of external coefficients.

External payloads carry rational coefficients in a few textual shapes:

* JSON integers:        ``7``
* JSON floats (decimal):``"0.25"`` or ``0.25`` — parsed exactly, never binary
* Fraction strings:     ``"3/7"``, ``"-5/9"``
* Scientific strings:   ``"1.25e-3"`` — converted exactly to a Fraction

Parsing is *exact*: decimals and scientific notation go through
:class:`fractions.Fraction` string constructors, so ``0.1`` is one tenth, not
its binary64 approximation. A float token that is not finite, or a value whose
bit length exceeds the configured budget, is rejected at the boundary with a
specific failure code.
"""
from __future__ import annotations

import math
from fractions import Fraction
from typing import Any

from .errors import FailureCode, InvalidPolynomialError
from .polynomial import Poly, trim
from .settings import KernelSettings


def parse_rational(token: Any, *, index: int) -> Fraction:
    """Parse one coefficient token into an exact :class:`Fraction`.

    ``index`` is the coefficient's power, used in rejection diagnostics.
    """
    if isinstance(token, bool):
        # bool is a subclass of int; reject it to avoid silent True -> 1.
        raise InvalidPolynomialError(
            f"coefficient at power {index} must be a number, not boolean",
            code=FailureCode.INVALID_COEFFICIENTS,
            state={"power": index, "kind": "boolean"},
        )
    if isinstance(token, int):
        return Fraction(token)
    if isinstance(token, Fraction):
        return token
    if isinstance(token, float):
        if not math.isfinite(token):
            raise InvalidPolynomialError(
                f"coefficient at power {index} is not finite",
                code=FailureCode.INVALID_COEFFICIENTS,
                state={"power": index, "kind": "non-finite-float"},
            )
        # Exact decimal via repr: repr(0.1) -> '0.1', Fraction('0.1') = 1/10.
        return Fraction(repr(token))
    if isinstance(token, str):
        text = token.strip()
        if not text:
            raise InvalidPolynomialError(
                f"coefficient at power {index} is an empty string",
                code=FailureCode.INVALID_COEFFICIENTS,
                state={"power": index},
            )
        try:
            return Fraction(text)
        except (ValueError, ZeroDivisionError):
            try:
                # Fraction handles '1.25e-3' from Python 3.11, but keep an
                # explicit decimal fallback for portability.
                return _parse_scientific(text)
            except ValueError as exc:
                raise InvalidPolynomialError(
                    f"coefficient at power {index} is not a valid rational: "
                    f"{_redact(token)!r}",
                    code=FailureCode.INVALID_COEFFICIENTS,
                    state={"power": index},
                ) from exc
    raise InvalidPolynomialError(
        f"coefficient at power {index} has unsupported type",
        code=FailureCode.INVALID_COEFFICIENTS,
        state={"power": index, "kind": type(token).__name__},
    )


def _parse_scientific(text: str) -> Fraction:
    mantissa_text, _, exponent_text = text.partition("e")
    if "E" in text and not exponent_text:
        mantissa_text, _, exponent_text = text.partition("E")
    if not exponent_text:
        raise ValueError("not a number")
    mantissa = Fraction(mantissa_text)
    exponent = int(exponent_text)
    if exponent >= 0:
        return mantissa * (10 ** exponent)
    return mantissa / (10 ** (-exponent))


def _redact(token: Any) -> str:
    """Never echo a raw coefficient value into diagnostics; show its shape."""
    if isinstance(token, str):
        return f"string(length={len(token)})"
    return type(token).__name__


def _check_coefficient_budget(value: Fraction, index: int,
                              settings: KernelSettings) -> None:
    bits = max(abs(value.numerator).bit_length(),
               abs(value.denominator).bit_length())
    if bits > settings.max_coeff_bits:
        raise InvalidPolynomialError(
            f"coefficient at power {index} exceeds the bit-length budget",
            code=FailureCode.COEFFICIENT_TOO_LARGE,
            state={"power": index, "bits": bits,
                   "budget_bits": settings.max_coeff_bits},
        )


def parse_polynomial(raw_coefficients: list[Any],
                     settings: KernelSettings) -> Poly:
    """Validate and parse an ascending-power coefficient list.

    The list must be non-empty; an all-zero list denotes the zero polynomial,
    which the service layer represents explicitly rather than as a root list.
    """
    if not isinstance(raw_coefficients, list):
        raise InvalidPolynomialError(
            "coefficients must be a JSON array in ascending power order",
            code=FailureCode.INVALID_COEFFICIENTS,
            state={"kind": type(raw_coefficients).__name__},
        )
    if len(raw_coefficients) == 0:
        raise InvalidPolynomialError(
            "coefficients array is empty; send at least one coefficient "
            "(all zeros denotes the zero polynomial explicitly)",
            code=FailureCode.EMPTY_POLYNOMIAL,
        )
    if len(raw_coefficients) > settings.max_coefficients:
        raise InvalidPolynomialError(
            "too many coefficients",
            code=FailureCode.TOO_MANY_COEFFICIENTS,
            state={"received": len(raw_coefficients),
                   "limit": settings.max_coefficients},
        )

    coeffs: list[Fraction] = []
    for power, token in enumerate(raw_coefficients):
        value = parse_rational(token, index=power)
        _check_coefficient_budget(value, power, settings)
        coeffs.append(value)

    poly = trim(tuple(coeffs))
    degree = len(poly) - 1
    if degree > settings.max_degree:
        raise InvalidPolynomialError(
            f"polynomial degree {degree} exceeds the configured maximum",
            code=FailureCode.DEGREE_EXCEEDED,
            state={"degree": degree, "limit": settings.max_degree},
        )
    return poly
