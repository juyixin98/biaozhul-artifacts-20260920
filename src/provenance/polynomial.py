"""The semantic domain: polynomials with natural-number coefficients, N[X].

Why a polynomial and not a set of input ids
-------------------------------------------
* Duplicate contributions add: an output produced in two *different* ways from
  the same input tuples must read ``x + x = 2x``. A set collapses that to ``x``.
* Joint dependencies multiply: a join pairing two tuples reads ``x*y``, and a
  self-join pairing a tuple with itself reads ``x*x = x**2``.
* Evaluation is a semiring homomorphism N[X] -> (Q, +, *), so substituting a
  numeric weight per input tuple and evaluating must equal the answer's
  multiplicity / weighted score.

A :class:`Poly` is kept in canonical (normal) form: each monomial is a sorted
tuple of ``(variable, exponent)`` pairs mapped to a positive int coefficient,
with no zero entries. Normalisation controls growth and makes equality and
string output deterministic; it never changes the formal semantics (it is only
ring arithmetic).
"""
from __future__ import annotations

from fractions import Fraction
from typing import Iterable, Mapping

from .errors import WeightError

# Canonical monomial: sorted tuple of (variable, positive exponent).
Monomial = tuple[tuple[str, int], ...]
ONE_MONO: Monomial = ()


def mono(*names: str) -> Monomial:
    """Build a monomial from variable names, counting repeats into exponents."""
    counts: dict[str, int] = {}
    for name in names:
        counts[name] = counts.get(name, 0) + 1
    return tuple(sorted(counts.items()))


def _merge(left: Monomial, right: Monomial) -> Monomial:
    counts: dict[str, int] = dict(left)
    for name, exp in right:
        counts[name] = counts.get(name, 0) + exp
    return tuple(sorted(counts.items()))


class Poly:
    """Immutable polynomial in canonical normal form."""

    __slots__ = ("terms",)

    def __init__(self, terms: Mapping[Monomial, int] | Iterable[tuple[Monomial, int]] = ()):
        items = terms.items() if isinstance(terms, Mapping) else terms
        normalised: dict[Monomial, int] = {}
        for monomial, coeff in items:
            coeff = int(coeff)
            if coeff == 0:
                continue
            if coeff < 0:
                # Provenance polynomials carry non-negative multiplicities; a
                # negative coefficient signals a bug in the engine.
                raise ValueError(f"negative coefficient {coeff} for {monomial}")
            normalised[monomial] = normalised.get(monomial, 0) + coeff
        self.terms: dict[Monomial, int] = {
            m: c for m, c in normalised.items() if c > 0
        }

    # -- constructors -----------------------------------------------------
    @staticmethod
    def one() -> "Poly":
        return Poly({ONE_MONO: 1})

    @staticmethod
    def zero() -> "Poly":
        return Poly(())

    @staticmethod
    def var(name: str) -> "Poly":
        return Poly({mono(name): 1})

    @staticmethod
    def from_monomials(monomials: Iterable[Iterable[str]]) -> "Poly":
        return Poly((mono(*m), 1) for m in monomials)

    # -- algebra ----------------------------------------------------------
    def __add__(self, other: "Poly") -> "Poly":
        return add(self, other)

    def __mul__(self, other: "Poly") -> "Poly":
        return mul(self, other)

    def scale(self, factor: int) -> "Poly":
        if factor == 0:
            return Poly.zero()
        return Poly((m, c * factor) for m, c in self.terms.items())

    # -- introspection ----------------------------------------------------
    @property
    def is_zero(self) -> bool:
        return not self.terms

    @property
    def is_one(self) -> bool:
        return self.terms == {ONE_MONO: 1}

    def variables(self) -> set[str]:
        return {name for monomial in self.terms for name, _ in monomial}

    def degree(self) -> int:
        return max((sum(exp for _, exp in m) for m in self.terms), default=0)

    # -- evaluation -------------------------------------------------------
    def evaluate(self, weights: Mapping[str, object]) -> Fraction:
        """Substitute a numeric weight per variable and evaluate exactly.

        Uses :class:`Fraction` so hand checks compare exact rationals.
        A missing variable fails with the typed :class:`WeightError`.
        """
        env = {str(k): _to_fraction(v, str(k)) for k, v in weights.items()}
        total = Fraction(0)
        for monomial, coeff in self.terms.items():
            product = Fraction(coeff)
            for name, exp in monomial:
                if name not in env:
                    raise WeightError(
                        f"missing numeric weight for provenance variable {name!r}",
                        details={"variable": name, "known": sorted(env)},
                    )
                product *= env[name] ** exp
            total += product
        return total

    # -- canonical rendering ---------------------------------------------
    def to_string(self) -> str:
        if not self.terms:
            return "0"
        return " + ".join(
            _format_monomial(m, c) for m, c in _sorted_terms(self.terms)
        )

    def to_dict(self) -> list[dict]:
        """JSON-friendly canonical form: ``[{"vars": {"x": 2}, "coeff": n}]``."""
        return [
            {"vars": dict(m), "coeff": c}
            for m, c in _sorted_terms(self.terms)
        ]

    # -- dunder helpers ---------------------------------------------------
    def __eq__(self, other: object) -> bool:
        return isinstance(other, Poly) and self.terms == other.terms

    def __hash__(self) -> int:
        return hash((frozenset(self.terms.items())))

    def __repr__(self) -> str:
        return f"Poly({self.to_string()!r})"


def _sorted_terms(terms: Mapping[Monomial, int]) -> list[tuple[Monomial, int]]:
    # Constant first, then by total degree, then lexicographically.
    return sorted(
        terms.items(),
        key=lambda kv: (sum(e for _, e in kv[0]), [name for name, _ in kv[0]]),
    )


def _format_monomial(monomial: Monomial, coeff: int) -> str:
    if not monomial:
        return str(coeff)  # constant term

    def factor(name: str, exp: int) -> str:
        return name if exp == 1 else f"{name}**{exp}"

    body = "*".join(factor(name, exp) for name, exp in monomial)
    return body if coeff == 1 else f"{coeff}*{body}"


def _to_fraction(value: object, name: str) -> Fraction:
    try:
        if isinstance(value, Fraction):
            return value
        if isinstance(value, bool):  # bool is an int subclass; reject ambiguity
            raise ValueError
        if isinstance(value, int):
            return Fraction(value)
        # Route floats/strings through str() so 0.1 does not silently become a
        # long binary float; non-numeric input raises.
        return Fraction(str(value))
    except (ValueError, ZeroDivisionError, TypeError) as exc:
        raise WeightError(
            f"weight for {name!r} is not a finite number: {value!r}",
            details={"variable": name},
        ) from exc


def add(left: Poly, right: Poly) -> Poly:
    return Poly(list(left.terms.items()) + list(right.terms.items()))


def mul(left: Poly, right: Poly) -> Poly:
    if left.is_zero or right.is_zero:
        return Poly.zero()
    terms: list[tuple[Monomial, int]] = []
    for m1, c1 in left.terms.items():
        for m2, c2 in right.terms.items():
            terms.append((_merge(m1, m2), c1 * c2))
    return Poly(terms)


def sum_polys(polys: Iterable[Poly]) -> Poly:
    acc = Poly.zero()
    for polynomial in polys:
        acc = add(acc, polynomial)
    return acc
