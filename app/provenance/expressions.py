"""N[X] provenance polynomial semiring.

A provenance polynomial is a finite map from *monomials* to positive integer
coefficients::

    {("r1", "s1"): 2, ("t3",): 1}   ==   2 * r1 * s1 + t3

A monomial is a tuple of input-row witness ids.  Multiplication of witnesses is
NOT idempotent: joining a row with itself keeps the repeated factor, so
``x * x`` is the monomial ``("x", "x")`` (rendered ``x^2``), never ``x``.

Semiring laws relied on by the relational engine:

* ``p + 0 = p``          (union with an empty set adds nothing)
* ``p * 1 = p``          (join with a unit relation is identity)
* ``p * 0 = 0``          (a failed join contributes nothing)
* ``(a + b) * c = a*c + b*c``  (distributivity lets the engine emit one term
  per successful join binding without changing the value)

Canonicalization (sorting witnesses inside a monomial, summing coefficients of
equal monomials, dropping zero coefficients) bounds expression growth but does
not change the polynomial's value: addition and multiplication in N[X] are
commutative, so reordering factors is semantics-preserving.
"""
from __future__ import annotations

from collections import defaultdict
from typing import Mapping

# A monomial is a sorted tuple of witness (input row) ids.
Monomial = tuple[str, ...]


class MissingWeightError(KeyError):
    """Raised when numeric evaluation has no weight for a witness."""


class Polynomial:
    """Immutable provenance polynomial over the N[X] semiring."""

    __slots__ = ("terms",)

    def __init__(self, terms: Mapping[Monomial, int] | None = None) -> None:
        # Always store a canonical copy: sorted monomial, positive coefficients.
        canonical: dict[Monomial, int] = {}
        if terms:
            for monomial, coeff in terms.items():
                if coeff == 0:
                    continue
                key = tuple(sorted(monomial))
                canonical[key] = canonical.get(key, 0) + coeff
                if canonical[key] == 0:
                    del canonical[key]
        object.__setattr__(self, "terms", canonical)

    def __setattr__(self, name: str, value: object) -> None:
        raise AttributeError("Polynomial is immutable")

    # ---- constructors -------------------------------------------------

    @classmethod
    def zero(cls) -> "Polynomial":
        return cls()

    @classmethod
    def one(cls) -> "Polynomial":
        return cls({(): 1})

    @classmethod
    def var(cls, witness_id: str) -> "Polynomial":
        return cls({(witness_id,): 1})

    @classmethod
    def from_witnesses(cls, witness_ids: tuple[str, ...]) -> "Polynomial":
        """Build a single monomial from several joint witnesses."""
        if not witness_ids:
            return cls.one()
        return cls({tuple(witness_ids): 1})

    # ---- semiring operations ------------------------------------------

    def __add__(self, other: "Polynomial") -> "Polynomial":
        merged: dict[Monomial, int] = defaultdict(int)
        for monomial, coeff in self.terms.items():
            merged[monomial] += coeff
        for monomial, coeff in other.terms.items():
            merged[monomial] += coeff
        return Polynomial(merged)

    def __mul__(self, other: "Polynomial") -> "Polynomial":
        product: dict[Monomial, int] = defaultdict(int)
        for m1, c1 in self.terms.items():
            for m2, c2 in other.terms.items():
                product[tuple(sorted(m1 + m2))] += c1 * c2
        return Polynomial(product)

    # ---- predicates / introspection -----------------------------------

    def is_zero(self) -> bool:
        return not self.terms

    def witnesses(self) -> set[str]:
        """All input row ids appearing anywhere in the polynomial."""
        return {w for monomial in self.terms for w in monomial}

    # ---- numeric evaluation -------------------------------------------

    def evaluate(self, weights: Mapping[str, float]) -> float:
        """Inject a numeric weight per witness and evaluate the polynomial.

        Raises MissingWeightError if any witness has no supplied weight, so a
        numeric check can never silently treat an unknown input as zero.
        """
        total = 0
        for monomial, coeff in self.terms.items():
            factor = 1
            for witness in monomial:
                if witness not in weights:
                    raise MissingWeightError(
                        f"no numeric weight provided for witness {witness!r}"
                    )
                factor *= weights[witness]
            total += coeff * factor
        return total

    # ---- canonical rendering -------------------------------------------

    def render(self) -> str:
        """Deterministic, human-readable canonical form."""
        if not self.terms:
            return "0"
        parts = [self._render_monomial(m, c) for m, c in self._sorted_terms()]
        return " + ".join(parts)

    def _sorted_terms(self) -> list[tuple[Monomial, int]]:
        return sorted(self.terms.items(), key=lambda kv: (len(kv[0]), kv[0]))

    @staticmethod
    def _render_monomial(monomial: Monomial, coeff: int) -> str:
        # collapse consecutive equal factors into exponent notation
        factors: list[str] = []
        run: list[str] = []
        for witness in monomial:
            if run and witness != run[0]:
                factors.append(Polynomial._run(run))
                run = []
            run.append(witness)
        if run:
            factors.append(Polynomial._run(run))
        body = "*".join(factors) if factors else "1"
        return body if coeff == 1 else f"{coeff}*{body}"

    @staticmethod
    def _run(run: list[str]) -> str:
        return run[0] if len(run) == 1 else f"{run[0]}^{len(run)}"

    # ---- equality / repr -----------------------------------------------

    def __eq__(self, other: object) -> bool:
        return isinstance(other, Polynomial) and other.terms == self.terms

    def __hash__(self) -> int:
        return hash(tuple(self.terms.items()))

    def __repr__(self) -> str:
        return f"Polynomial({self.render()!r})"
