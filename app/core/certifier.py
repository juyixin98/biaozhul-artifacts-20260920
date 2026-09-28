"""Interval-Newton + bisection root certification.

Theorems used (f continuously differentiable on X, all arithmetic enclosed)

* Range exclusion:        if 0 ∉ f(X) then X contains no root.
* Newton exclusion:       N(X) = m(X) - f(m(X))/f'(X); if X ∩ N(X) = ∅
                          then X contains no root.
* Newton uniqueness:      if X ∩ N(X) ⊂ int(X) then X contains exactly one
                          root, which lies in X ∩ N(X).
* Monotone IVT:           if f'(X) is strictly one-signed and the rigorous
                          endpoint enclosures straddle zero (endpoint zero
                          allowed), X contains exactly one root. This is what
                          certifies a root exactly on a search boundary.

Nothing short of these conditions is reported as "unique": containing zero is
never, by itself, taken as evidence of a root. When the derivative enclosure
crosses zero the step cannot contract and the interval is bisected. Even-
multiplicity / tangency roots therefore exhaust the budget and are returned as
``undecided`` with an explicit reason, never mis-certified.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any

from .config import CertConfig
from .errors import ResourceExhausted
from .evaluator import Evaluator
from .interval_utils import IntervalOps
from .tracer import Tracer

# Outcome labels for a single active sub-interval.
OUTCOME_UNIQUE = "unique_root"
OUTCOME_NONE = "no_root"
OUTCOME_UNDECIDED = "undecided"

# Undecided reasons.
REASON_BUDGET_DEPTH = "max_depth_reached"
REASON_BUDGET_EVALS = "max_evaluations_reached"
REASON_TANGENT = "tangent_or_even_multiplicity"
REASON_NEWTON_STALL = "newton_did_not_contract"
REASON_IDENTICALLY_ZERO = "function_identically_zero_not_isolable"

# Cap consecutive Newton shrinks that never satisfy a theorem, to guarantee
# forward progress even on pathological near-tangent problems.
MAX_SHRINKS_PER_LEAF = 8


@dataclass
class _Item:
    lo: Any  # mpmath mpf
    hi: Any
    depth: int


@dataclass
class CertifiedRoot:
    lo: Any
    hi: Any
    theorem: str
    newton_iterations: int
    residual_range: tuple[Any, Any]
    derivative_range: tuple[Any, Any]
    midpoint: Any
    evidence: dict[str, Any] = field(default_factory=dict)


@dataclass
class UndecidedInterval:
    lo: Any
    hi: Any
    depth: int
    reason: str
    value_range: tuple[Any, Any]
    derivative_range: tuple[Any, Any] | None


@dataclass
class CertificationResult:
    roots: list[CertifiedRoot] = field(default_factory=list)
    undecided: list[UndecidedInterval] = field(default_factory=list)
    excluded_leaves: int = 0
    bisections: int = 0
    newton_steps: int = 0
    value_evaluations: int = 0
    derivative_evaluations: int = 0
    budget_hit: str | None = None
    search_interval: tuple[Any, Any] | None = None

    @property
    def root_free(self) -> bool:
        return not self.roots and not self.undecided


class Certifier:
    def __init__(
        self,
        f_ast,
        fp_ast,
        ev: Evaluator,
        config: CertConfig,
        tracer: Tracer,
    ) -> None:
        self.f = f_ast
        self.fp = fp_ast
        self.ev = ev
        self.cfg = config
        self.tracer = tracer
        self.io = IntervalOps(ev)
        self.target = ev.mp.mpf(config.target_width)
        self.evals = 0
        self.result = CertificationResult()

    # -- entry point --------------------------------------------------------
    def run(self, lo, hi) -> CertificationResult:
        self.result.search_interval = (lo, hi)
        # Degenerate case: f' enclosure is exactly {0} over the whole search
        # interval, i.e. f is rigorously constant. If that constant is zero
        # the root set is the whole interval (not an isolable root); report a
        # single undecided region instead of bisecting forever.
        initial = self.io.make(lo, hi)
        whole_derivative = self._eval_derivative(initial)
        if self.io.is_exact_zero(whole_derivative):
            whole_value = self._eval_value(initial)
            if self.io.contains_zero(whole_value):
                self._record_undecided(
                    initial, 0, REASON_IDENTICALLY_ZERO,
                    whole_value, whole_derivative,
                )
                return self.result

        stack: list[_Item] = [_Item(lo, hi, 0)]
        while stack:
            item = stack.pop()
            try:
                self._process(item, stack)
            except ResourceExhausted:
                # Budget died mid-interval: record every still-active piece as
                # undecided so partial results stay honest and replayable.
                self.result.budget_hit = REASON_BUDGET_EVALS
                self._record_undecide_stack(item, stack)
                break
        return self.result

    def _record_undecide_stack(
        self, current: _Item, stack: list[_Item]
    ) -> None:
        pending = [current] + stack
        for piece in pending:
            interval = self.io.make(piece.lo, piece.hi)
            try:
                value_range = self._eval_value(interval)
            except ResourceExhausted:
                value_range = self.io.make(piece.lo, piece.hi)
            self._record_undecided(
                interval, piece.depth, REASON_BUDGET_EVALS,
                value_range, None,
            )

    # -- per-interval decision ---------------------------------------------
    def _process(self, item: _Item, stack: list[_Item]) -> str:
        x = self.io.make(item.lo, item.hi)
        shrinks = 0
        current = x
        while True:
            value_range = self._eval_value(current)
            # Rule A: range exclusion (no root).
            if not self.io.contains_zero(value_range):
                self.result.excluded_leaves += 1
                self.tracer.event(
                    "certify", "range_excludes_zero",
                    interval=self._bounds(current),
                    value_range=self._bounds(value_range),
                    depth=item.depth,
                )
                return OUTCOME_NONE

            derivative_range = self._eval_derivative(current)
            newton = self._newton_step(current, value_range, derivative_range)

            # Rule C: Newton exclusion.
            if newton is _NEWTON_EMPTY:
                self.result.excluded_leaves += 1
                self.tracer.event(
                    "certify", "newton_image_disjoint",
                    interval=self._bounds(current),
                    depth=item.depth,
                )
                return OUTCOME_NONE

            if newton is _NEWTON_UNBOUNDED:
                # Derivative is exactly {0}: no Newton information; fall
                # through to monotone/bisection logic with no contraction.
                image, intersection, newton_evidence = None, current, None
            else:
                image, intersection, newton_evidence = newton

            # Rule B: Newton uniqueness (image intersection strictly interior).
            if intersection is not None and self.io.strictly_inside(
                intersection, current
            ):
                refined = self._refine(intersection)
                newton_evidence["kind"] = "interval_newton_uniqueness"
                newton_evidence["containment"] = (
                    "intersection_strictly_inside_search_interval"
                )
                self._record_root(
                    refined, "interval_newton_uniqueness",
                    iterations=self.result.newton_steps,
                    evidence=newton_evidence,
                )
                return OUTCOME_UNIQUE

            # Rule D: monotone IVT (covers interior stalls and boundary roots).
            monotone, monotone_evidence = self._monotone_root(
                current, derivative_range
            )
            if monotone == OUTCOME_UNIQUE:
                refined = self._refine_monotone(current, derivative_range)
                self._record_root(
                    refined, "monotone_intermediate_value",
                    iterations=self.result.newton_steps,
                    evidence=monotone_evidence,
                )
                return OUTCOME_UNIQUE
            if monotone == OUTCOME_NONE:
                self.result.excluded_leaves += 1
                self.tracer.event(
                    "certify", "monotone_endpoint_exclusion",
                    interval=self._bounds(current),
                    evidence=monotone_evidence,
                    depth=item.depth,
                )
                return OUTCOME_NONE

            # No theorem applies: shrink via Newton when it makes progress,
            # otherwise (or after a few shrinks) conservatively bisect.
            width = self.io.width(current)
            if (
                image is not None
                and intersection is not None
                and not self.io.is_infinite(image)
                and self.io.width(intersection) < width * 0.99
                and shrinks < MAX_SHRINKS_PER_LEAF
                and width > self.target
            ):
                self.tracer.event(
                    "shrink", "newton_contracts",
                    interval=self._bounds(current),
                    image_intersection=self._bounds(intersection),
                    depth=item.depth,
                )
                current = intersection
                lo, hi = self.io.endpoints(current)
                item = _Item(lo, hi, item.depth)
                shrinks += 1
                continue

            return self._divide_or_undecide(item, current, value_range,
                                            derivative_range, stack)

    # -- Newton operator ----------------------------------------------------
    def _newton_step(self, x, value_range, derivative_range):
        """Compute one interval-Newton image with its theorem evidence.

        Returns ``_NEWTON_EMPTY`` when the image is disjoint from X (rigorous
        no-root), ``_NEWTON_UNBOUNDED`` when the derivative is exactly {0}, or
        a triple ``(image, intersection, evidence)``.
        """
        self.result.newton_steps += 1
        # A derivative enclosure exactly equal to {0} gives no contraction
        # (e.g. a constant function); mark unbounded rather than dividing by
        # zero, and let bisection/undecided reporting handle it conservatively.
        if self.io.is_exact_zero(derivative_range):
            return _NEWTON_UNBOUNDED
        mid_iv = self.io.midpoint(x)
        mid = self.io.endpoints(mid_iv)[0]
        f_mid = self.ev.interval(self.f, mid_iv)
        self.evals += 1
        self.result.value_evaluations += 1
        # Extended interval division: infinite endpoints when f'(X) straddles
        # zero, which simply fails to contract (conservative).
        image = mid_iv - f_mid / derivative_range
        intersection = self.io.intersect(x, image)
        if intersection is None:
            return _NEWTON_EMPTY
        evidence = {
            "kind": "interval_newton",
            "midpoint": str(mid),
            "f_midpoint_enclosure": self._bounds(f_mid),
            "derivative_range": self._bounds(derivative_range),
            "newton_image": self._bounds(image),
            "search_interval": self._bounds(x),
            "image_intersection": self._bounds(intersection),
        }
        return image, intersection, evidence

    # -- monotone IVT -------------------------------------------------------
    def _refine_monotone(self, x, derivative_range):
        """Tighten a monotone interval's unique root by directional bisection.

        Strict monotonicity makes the sign of the rigorous midpoint enclosure
        f(m) decisive: f(m) > 0 puts the root in the left half (increasing),
        etc. When f(m) itself straddles zero the bracket is already at the
        rounding scale, so refinement stops.
        """
        d_lo, d_hi = self.io.endpoints(derivative_range)
        increasing = d_lo > 0
        current = x
        for _ in range(self.cfg.max_newton_iters):
            if self.io.width(current) <= self.target:
                break
            lo, hi = self.io.endpoints(current)
            mid = (lo + hi) / 2
            f_mid = self._eval_value(self.io.make(mid, mid))
            fm_lo, fm_hi = self.io.endpoints(f_mid)
            if fm_lo > 0:
                current = (
                    self.io.make(lo, mid) if increasing
                    else self.io.make(mid, hi)
                )
            elif fm_hi < 0:
                current = (
                    self.io.make(mid, hi) if increasing
                    else self.io.make(lo, mid)
                )
            else:
                # 0 in f(m): root is within rounding distance of mid. Clamp a
                # symmetrised bracket to the current one so boundary roots
                # never produce enclosures outside the search interval.
                half = self.io.width(current) / 2
                candidate = self.io.make(mid - half, mid + half)
                clamped = self.io.intersect(candidate, current)
                if clamped is None:  # pragma: no cover - defensive
                    break
                current = clamped
                break
        self.tracer.event(
            "refine", "monotone_directional_bisection",
            enclosure=self._bounds(current),
            width=str(self.io.width(current)),
        )
        return current

    def _monotone_root(self, x, derivative_range):
        """Return ``(verdict, evidence)``.

        ``verdict`` is one of OUTCOME_UNIQUE / OUTCOME_NONE / None. Evidence
        records the rigorous derivative sign and the endpoint enclosures that
        establish the monotone intermediate-value claim.
        """
        lo, hi = self.io.endpoints(x)
        d_lo, d_hi = self.io.endpoints(derivative_range)
        if d_lo > 0:
            increasing = True
        elif d_hi < 0:
            increasing = False
        else:
            return None, None
        f_lo = self._eval_value(self.io.make(lo, lo))
        f_hi = self._eval_value(self.io.make(hi, hi))
        flo_lo, flo_hi = self.io.endpoints(f_lo)
        fhi_lo, fhi_hi = self.io.endpoints(f_hi)
        if increasing:
            # Increasing: f(lo) <= f(hi).
            # root  <=> f(lo) <= 0 <= f(hi), proven via the INNER bounds
            # upper(f(lo)) <= 0 and lower(f(hi)) >= 0.
            straddles = flo_hi <= 0 and fhi_lo >= 0
            # strictly positive throughout <=> lower(f(lo)) > 0
            strictly_above = flo_lo > 0
            # strictly negative throughout <=> upper(f(hi)) < 0
            strictly_below = fhi_hi < 0
        else:
            # Decreasing: f(lo) >= f(hi).
            # root <=> f(lo) >= 0 >= f(hi), proven via lower(f(lo)) >= 0
            # and upper(f(hi)) <= 0.
            straddles = flo_lo >= 0 and fhi_hi <= 0
            # strictly positive throughout <=> lower(f(hi)) > 0
            strictly_above = fhi_lo > 0
            # strictly negative throughout <=> upper(f(lo)) < 0
            strictly_below = flo_hi < 0
        evidence = {
            "kind": "monotone_intermediate_value",
            "monotonicity": "increasing" if increasing else "decreasing",
            "derivative_lower_bound": str(d_lo),
            "derivative_upper_bound": str(d_hi),
            "f_at_left_enclosure": [str(flo_lo), str(flo_hi)],
            "f_at_right_enclosure": [str(fhi_lo), str(fhi_hi)],
            "interval": [str(lo), str(hi)],
        }
        if straddles:
            return OUTCOME_UNIQUE, evidence
        if strictly_above or strictly_below:
            evidence["kind"] = "monotone_endpoint_exclusion"
            return OUTCOME_NONE, evidence
        return None, None

    # -- refinement of a certified enclosure --------------------------------
    def _refine(self, x):
        current = x
        iterations = 0
        for _ in range(self.cfg.max_newton_iters):
            iterations += 1
            if self.io.width(current) <= self.target:
                break
            value_range = self._eval_value(current)
            derivative_range = self._eval_derivative(current)
            mid = self.io.midpoint(current)
            f_mid = self.ev.interval(self.f, mid)
            self.evals += 1
            self.result.value_evaluations += 1
            d_lo, d_hi = self.io.endpoints(derivative_range)
            if d_lo == 0 or d_hi == 0 or self.io.contains_zero(derivative_range):
                break  # uniqueness already proven; stop tightening safely.
            image = mid - f_mid / derivative_range
            nxt = self.io.intersect(current, image)
            if nxt is None:
                break
            if self.io.width(nxt) >= self.io.width(current):
                break
            current = nxt
        self.tracer.event(
            "refine", "target_width_or_stall",
            iterations=iterations,
            enclosure=self._bounds(current),
            width=str(self.io.width(current)),
        )
        return current

    # -- bisection / undecided ---------------------------------------------
    def _divide_or_undecide(
        self, item, x, value_range, derivative_range, stack: list[_Item]
    ) -> str:
        width = self.io.width(x)
        # Below target resolution with no theorem: tangent/even multiplicity.
        if width <= self.target:
            self._record_undecided(
                x, item.depth, REASON_TANGENT, value_range, derivative_range
            )
            return OUTCOME_UNDECIDED

        if item.depth >= self.cfg.max_depth:
            self._record_undecided(
                x, item.depth, REASON_BUDGET_DEPTH, value_range, derivative_range
            )
            self.result.budget_hit = REASON_BUDGET_DEPTH
            return OUTCOME_UNDECIDED

        if self.evals >= self.cfg.max_evals:
            self._record_undecided(
                x, item.depth, REASON_BUDGET_EVALS, value_range, derivative_range
            )
            self.result.budget_hit = REASON_BUDGET_EVALS
            return OUTCOME_UNDECIDED

        left, right = self.io.split(x)
        l_lo, l_hi = self.io.endpoints(left)
        r_lo, r_hi = self.io.endpoints(right)
        self.result.bisections += 1
        self.tracer.event(
            "bisect", "derivative_crosses_zero_or_stall",
            interval=self._bounds(x),
            left=[str(l_lo), str(l_hi)],
            right=[str(r_lo), str(r_hi)],
            depth=item.depth,
            derivative_range=self._bounds(derivative_range),
        )
        # Push right first so left is processed first (LIFO).
        stack.append(_Item(r_lo, r_hi, item.depth + 1))
        stack.append(_Item(l_lo, l_hi, item.depth + 1))
        return OUTCOME_UNDECIDED

    # -- recording ----------------------------------------------------------
    def _record_root(
        self,
        enclosure,
        theorem: str,
        iterations: int,
        evidence: dict[str, Any] | None = None,
    ) -> None:
        lo, hi = self.io.endpoints(enclosure)
        residual = self._eval_value(enclosure)
        derivative = self._eval_derivative(enclosure)
        midpoint = (lo + hi) / 2
        root = CertifiedRoot(
            lo=lo,
            hi=hi,
            theorem=theorem,
            newton_iterations=iterations,
            residual_range=self.io.endpoints(residual),
            derivative_range=self.io.endpoints(derivative),
            midpoint=midpoint,
            evidence=evidence or {},
        )
        # A root lying exactly on a shared bisection boundary is certified
        # once per adjacent monotone piece: merge overlapping enclosures so
        # the same root is never reported twice.
        self._merge_root(root)
        self.tracer.event(
            "certify", theorem,
            enclosure=[str(lo), str(hi)],
            width=str(hi - lo),
            midpoint=str(midpoint),
            residual=self._bounds(residual),
            evidence=evidence or {},
            iterations=iterations,
        )

    def _merge_root(self, root: CertifiedRoot) -> None:
        """Merge an enclosure with an overlapping existing one, or de-duplicate.

        Adjacent monotone pieces can each certify a root on their shared
        bisection boundary, reporting it twice. This never silently merges:
        the hull is re-proved by the monotone IVT (derivative strictly signed
        over the *whole* hull); when that proof is unavailable the looser
        enclosure is dropped as a duplicate, so no root is double-counted and
        no uniqueness claim is extended without evidence.
        """
        for i, existing in enumerate(self.result.roots):
            if root.lo > existing.hi or root.hi < existing.lo:
                continue
            hull = self.io.make(
                min(root.lo, existing.lo), max(root.hi, existing.hi)
            )
            derivative = self._eval_derivative(hull)
            verdict, hull_evidence = self._monotone_root(hull, derivative)
            if verdict == OUTCOME_UNIQUE:
                refined = self._refine_monotone(hull, derivative)
                lo, hi = self.io.endpoints(refined)
                residual = self._eval_value(refined)
                self.result.roots[i] = CertifiedRoot(
                    lo=lo,
                    hi=hi,
                    theorem="monotone_intermediate_value_merged_boundary",
                    newton_iterations=existing.newton_iterations
                    + root.newton_iterations,
                    residual_range=self.io.endpoints(residual),
                    derivative_range=self.io.endpoints(
                        self._eval_derivative(refined)
                    ),
                    midpoint=(lo + hi) / 2,
                    evidence=hull_evidence or {},
                )
                self.tracer.event(
                    "certify", "monotone_intermediate_value_merged_boundary",
                    enclosure=[str(lo), str(hi)],
                    merged_with=[str(existing.lo), str(existing.hi)],
                )
                return
            # Proof unavailable on the hull: keep the tighter enclosure and
            # discard the duplicate, recording the reason explicitly.
            tighter, looser = sorted(
                (existing, root), key=lambda r: r.hi - r.lo
            )
            self.tracer.event(
                "certify", "duplicate_shared_boundary_dropped",
                kept=[str(tighter.lo), str(tighter.hi)],
                dropped=[str(looser.lo), str(looser.hi)],
            )
            return
        self.result.roots.append(root)

    def _record_undecided(
        self, x, depth, reason, value_range, derivative_range
    ) -> None:
        lo, hi = self.io.endpoints(x)
        self.result.undecided.append(
            UndecidedInterval(
                lo=lo,
                hi=hi,
                depth=depth,
                reason=reason,
                value_range=self.io.endpoints(value_range),
                derivative_range=(
                    self.io.endpoints(derivative_range)
                    if derivative_range is not None
                    else None
                ),
            )
        )
        self.tracer.event(
            "undecided", reason,
            interval=[str(lo), str(hi)],
            depth=depth,
            value_range=self._bounds(value_range),
            derivative_range=(
                self._bounds(derivative_range)
                if derivative_range is not None
                else None
            ),
        )

    # -- evaluation accounting ---------------------------------------------
    def _eval_value(self, x):
        self._check_budget()
        self.evals += 1
        self.result.value_evaluations += 1
        return self.ev.interval(self.f, x)

    def _eval_derivative(self, x):
        self._check_budget()
        self.evals += 1
        self.result.derivative_evaluations += 1
        return self.ev.interval(self.fp, x)

    def _check_budget(self) -> None:
        if self.evals >= self.cfg.max_evals:
            self.tracer.event("budget", "max_evaluations_reached", evals=self.evals)
            raise ResourceExhausted(
                f"evaluation budget of {self.cfg.max_evals} exhausted",
                {"evals": self.evals, "max_evals": self.cfg.max_evals},
            )

    # -- serialisation helpers ---------------------------------------------
    def _bounds(self, interval) -> list[str]:
        lo, hi = self.io.endpoints(interval)
        return [str(lo), str(hi)]


# Sentinels for Newton step outcomes.
_NEWTON_EMPTY = object()
_NEWTON_UNBOUNDED = object()
