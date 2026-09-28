"""Sturm-based exact real-root isolation kernel.

Input lives in Q[x]; every sign decision is made with exact rational
arithmetic (:mod:`app.polynomial`), so the answers carry a mathematical proof
rather than a statistical guarantee.

Pipeline
--------
1. Square-free decomposition (:mod:`app.squarefree`) separates roots by
   multiplicity — repeated roots are reported with their true multiplicity.
2. For every square-free factor a classical Sturm chain is built and the
   half-open cell ``(a, b]`` contains ``V(a) - V(b)`` distinct roots, where
   zeros encountered in a sign row are skipped (Sturm's convention).
3. Bisection on rational dyadic midpoints isolates cells: a cell with one
   root narrower than the requested width is returned; a root hit *exactly*
   at an endpoint is returned as an exact rational singleton.
4. A disjointness post-pass refines overlapping cells (roots from different
   multiplicity factors may start on different grids) until every returned
   interval is pairwise disjoint.

Budget: degree, coefficient bit size, chain length and the total number of
bisections are explicit limits. Exhausting the bisection budget raises
:class:`BudgetExceeded` carrying the partial, still-proven state.
"""
from __future__ import annotations

from collections import deque
from dataclasses import dataclass, field
from fractions import Fraction
from typing import Callable

from . import polynomial as P
from .errors import BudgetExceeded, FailureCode
from .polynomial import Poly
from .settings import KernelSettings
from .squarefree import SquareFreeDecomposition, square_free_decomposition


# ---------------------------------------------------------------------------
# Results
# ---------------------------------------------------------------------------

@dataclass(frozen=True)
class RootCell:
    """One isolated distinct real root.

    The root is either the exact rational singleton ``(lo == hi, exact=True)``
    or it lies in the *open* interval ``(lo, hi)`` — openness is proven: the
    isolating cell had Sturm count 1 with non-zero endpoint values.
    """

    lo: Fraction
    hi: Fraction
    multiplicity: int
    exact: bool
    variations_left: int
    variations_right: int
    chain_length: int

    @property
    def width(self) -> Fraction:
        return self.hi - self.lo


@dataclass
class FactorReport:
    multiplicity: int
    degree: int
    chain_length: int
    cauchy_bound: int
    isolated_roots: int


@dataclass
class IsolationResult:
    decomposition: SquareFreeDecomposition
    degree: int
    search_lo: Fraction
    search_hi: Fraction
    target_width: Fraction
    cells: list[RootCell]
    factor_reports: list[FactorReport]
    bisections_used: int
    bisection_limit: int

    @property
    def distinct_real_roots(self) -> int:
        return len(self.cells)

    @property
    def real_roots_with_multiplicity(self) -> int:
        return sum(c.multiplicity for c in self.cells)


# ---------------------------------------------------------------------------
# Small kernel-internal helpers
# ---------------------------------------------------------------------------

def sign_variations(signs: list[int]) -> int:
    """Count sign changes, skipping zero entries (Sturm's convention).

    Implemented directly on the ±1/0 sequence: two consecutive non-zero
    entries contribute a change exactly when they differ.
    """
    previous = 0
    count = 0
    for s in signs:
        if s == 0:
            continue
        if previous != 0 and s != previous:
            count += 1
        previous = s
    return count


def cauchy_integer_bound(p: Poly) -> int:
    """Return an integer B such that every root satisfies |root| < B.

    Cauchy's bound with an exact margin: with ``M = max_{i<n} |a_i/a_n|``,
    every root obeys ``|root| <= M + 1``, so ``B = floor(M) + 2`` is a strict
    integer bound — the initial bracketing endpoints are never roots.
    The floor is taken on the exact :class:`Fraction`, never via floats.
    """
    n = P.degree(p)
    top = abs(P.lc(p))
    m = max(abs(p[i]) / top for i in range(n))
    return m.numerator // m.denominator + 2


def _cells_intersect(a: RootCell, b: RootCell) -> bool:
    """Whether two located root sets have any common point.

    Exact cells are closed singletons ``{q}``; approximate cells are open
    ``(lo, hi)``. Cells merely sharing an endpoint are disjoint.
    """
    if a.exact and b.exact:
        return a.lo == b.lo  # cannot happen for coprime factors, kept for safety
    if a.exact:
        return b.lo < a.lo < b.hi
    if b.exact:
        return a.lo < b.lo < a.hi
    return max(a.lo, b.lo) < min(a.hi, b.hi)


# ---------------------------------------------------------------------------
# Per-run budget counter
# ---------------------------------------------------------------------------

@dataclass
class _Budget:
    limit: int
    used: int = 0

    def charge(self, state: Callable[[], dict]) -> None:
        self.used += 1
        if self.used > self.limit:
            raise BudgetExceeded(
                f"bisection budget exhausted after {self.used} halvings "
                f"(limit {self.limit})",
                code=FailureCode.BISECTION_BUDGET_EXCEEDED,
                state=state(),
            )


# ---------------------------------------------------------------------------
# Factor isolation
# ---------------------------------------------------------------------------

@dataclass
class _FactorIsolator:
    factor: Poly
    multiplicity: int
    settings: KernelSettings
    target_width: Fraction
    budget: _Budget
    cells: list[RootCell] = field(default_factory=list)
    bisections: int = 0

    def __post_init__(self) -> None:
        self.chain = P.sturm_chain(self.factor)
        if len(self.chain) > self.settings.max_sturm_length:
            raise BudgetExceeded(
                "Sturm chain exceeds configured length",
                code=FailureCode.STURM_LENGTH_EXCEEDED,
                state={"chain_length": len(self.chain),
                       "limit": self.settings.max_sturm_length},
            )
        self._sign_cache: dict[Fraction, list[int]] = {}
        # Exact rational roots already emitted as singletons. When a bisection
        # split lands exactly on a root, that point is still counted by the
        # left child's (a, mid] convention; the set prevents a duplicate.
        self._emitted: set[Fraction] = set()

    # -- exact signs, cached per rational point ---------------------------

    def _signs(self, x: Fraction) -> list[int]:
        cached = self._sign_cache.get(x)
        if cached is None:
            cached = [P.sign_exact(q, x) for q in self.chain]
            self._sign_cache[x] = cached
        return cached

    def _variations(self, x: Fraction) -> int:
        return sign_variations(self._signs(x))

    def _p_sign(self, x: Fraction) -> int:
        return self._signs(x)[0]

    def _cell(self, lo: Fraction, hi: Fraction, *, exact: bool) -> RootCell:
        return RootCell(
            lo=lo, hi=hi, multiplicity=self.multiplicity, exact=exact,
            variations_left=self._variations(lo),
            variations_right=self._variations(hi),
            chain_length=len(self.chain),
        )

    def _charge(self, width: Fraction, **extra: object) -> None:
        self.budget.charge(lambda: {
            "bisections_used": self.budget.used,
            "bisection_limit": self.budget.limit,
            "cell_width": str(width),
            "multiplicity": self.multiplicity,
            **extra,
        })

    def _emit(self, point: Fraction) -> RootCell:
        return RootCell(
            lo=point, hi=point, multiplicity=self.multiplicity, exact=True,
            variations_left=self._variations(point),
            variations_right=self._variations(point),
            chain_length=len(self.chain),
        )

    # -- main queue --------------------------------------------------------

    def isolate(self, lo: Fraction, hi: Fraction) -> None:
        # Convention: with zero rows skipped, V(a)-V(b) counts distinct roots
        # in the half-open cell (a, b] — a root at b counts, a root at a does
        # not. A root exactly at the initial left endpoint is therefore
        # reported separately before the cell is processed.
        if self._p_sign(lo) == 0:
            self.cells.append(self._emit(lo))
            self._emitted.add(lo)

        queue: deque[tuple[Fraction, Fraction]] = deque([(lo, hi)])
        while queue:
            a, b = queue.popleft()
            # Effective count of roots in (a, b] not already emitted as exact
            # singletons. Only the right endpoint can be double counted (a is
            # excluded by the half-open convention).
            count = self._variations(a) - self._variations(b)
            if self._p_sign(b) == 0 and b in self._emitted:
                count -= 1
            if count == 0:
                continue

            if count == 1 and self._p_sign(b) == 0 and b not in self._emitted:
                # The unique unemitted root is the closed right endpoint.
                self.cells.append(self._emit(b))
                self._emitted.add(b)
                continue

            width = b - a
            if count == 1 and width <= self.target_width:
                # The single unemitted root lies strictly inside (a, b): it
                # is not at b (handled above / emitted-adjusted) and a is
                # excluded by convention. Both endpoints non-root for *this*
                # root, so the cell is a genuine open enclosure.
                self.cells.append(self._cell(a, b, exact=False))
                continue

            mid = (a + b) / 2
            self._charge(width, roots_in_cell=count)
            self.bisections += 1
            if self._p_sign(mid) == 0 and mid not in self._emitted:
                # Root at the split belongs to the left child (a, mid]; emit
                # it as a singleton and let the emitted-adjustment prune it
                # from that child's count.
                self.cells.append(self._emit(mid))
                self._emitted.add(mid)
            # Both children are pushed; empty ones prune at zero cost.
            queue.append((a, mid))
            queue.append((mid, b))

    # -- post-pass support -------------------------------------------------

    def refine_cell(self, cell: RootCell) -> RootCell:
        """Halve an isolated open cell once, recomputing all evidence."""
        mid = (cell.lo + cell.hi) / 2
        self._charge(cell.width, refinement=True)
        self.bisections += 1
        if self._p_sign(mid) == 0:
            return self._cell(mid, mid, exact=True)
        if self._variations(cell.lo) - self._variations(mid) == 1:
            return self._cell(cell.lo, mid, exact=False)
        return self._cell(mid, cell.hi, exact=False)


# ---------------------------------------------------------------------------
# Orchestration
# ---------------------------------------------------------------------------

def isolate_real_roots(
    poly: Poly,
    settings: KernelSettings,
    *,
    target_width: Fraction,
    search_interval: tuple[Fraction, Fraction] | None = None,
) -> IsolationResult:
    """Isolate every distinct real root of ``poly`` inside ``[lo, hi]``.

    The *user-facing* search interval is the closed interval ``[lo, hi]``:
    a root sitting exactly at either endpoint is reported as an exact
    singleton. Internally, bisection cells use the half-open convention
    ``(a, b]``; the closed-left endpoint is handled once up front by the
    per-factor isolator, and the closed-right endpoint falls out of the
    half-open count. ``None`` means the whole real line, bracketed by a
    strict exact Cauchy bound whose endpoints are provably non-roots.
    """
    if P.is_zero(poly):
        raise ValueError("zero polynomial is handled by the service layer")
    if target_width <= 0:
        raise ValueError("target_width must be positive")

    decomposition = square_free_decomposition(poly)
    budget = _Budget(limit=settings.max_bisections)

    all_cells: list[RootCell] = []
    reports: list[FactorReport] = []
    isolators: dict[int, _FactorIsolator] = {}

    global_lo = global_hi = None
    for sf in decomposition.factors:
        bound = cauchy_integer_bound(sf.factor)
        lo, hi = search_interval if search_interval else (
            Fraction(-bound), Fraction(bound)
        )
        if global_lo is None or lo < global_lo:
            global_lo = lo
        if global_hi is None or hi > global_hi:
            global_hi = hi
        isolator = _FactorIsolator(
            sf.factor, sf.multiplicity, settings, target_width, budget
        )
        isolators[sf.multiplicity] = isolator
        isolator.isolate(lo, hi)
        all_cells.extend(isolator.cells)
        reports.append(FactorReport(
            multiplicity=sf.multiplicity,
            degree=P.degree(sf.factor),
            chain_length=len(isolator.chain),
            cauchy_bound=bound,
            isolated_roots=len(isolator.cells),
        ))

    _make_cells_disjoint(all_cells, isolators, budget)
    all_cells.sort(key=lambda c: (c.lo, c.hi))

    return IsolationResult(
        decomposition=decomposition,
        degree=P.degree(poly),
        search_lo=global_lo,
        search_hi=global_hi,
        target_width=target_width,
        cells=all_cells,
        factor_reports=reports,
        bisections_used=budget.used,
        bisection_limit=budget.limit,
    )


def _make_cells_disjoint(
    cells: list[RootCell],
    isolators: dict[int, _FactorIsolator],
    budget: _Budget,
) -> None:
    """Refine cells until pairwise disjoint.

    All cells isolate roots of square-free, pairwise coprime factors, so two
    intersecting cells contain two distinct points; repeatedly halving the
    wider cell eventually separates them. Exact singletons are never split.
    """
    while True:
        conflict = _first_conflict(cells)
        if conflict is None:
            return
        i, j = conflict
        wider, other = (i, j) if cells[i].width >= cells[j].width else (j, i)
        if cells[wider].exact:
            wider, other = other, wider
        if cells[wider].exact:
            # Defensive: two exact singletons cannot genuinely intersect.
            return
        isolator = isolators[cells[wider].multiplicity]
        cells[wider] = isolator.refine_cell(cells[wider])


def _first_conflict(cells: list[RootCell]) -> tuple[int, int] | None:
    for i in range(len(cells)):
        for j in range(i + 1, len(cells)):
            if _cells_intersect(cells[i], cells[j]):
                return i, j
    return None
