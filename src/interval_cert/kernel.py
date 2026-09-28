"""Certification kernel: interval Newton + bisection over mpmath intervals.

Algorithm (per box X, rigorously, all arithmetic outward-rounded):

1. If 0 not in f(X)               -> X contains no root (eliminate).
2. If 0 in f'(X)                  -> uniqueness theorem not applicable;
                                     bisect conservatively.
3. Otherwise compute the interval Newton image
       N(X) = m - f(m) / f'(X),  m = mid(X)
   - N(X) disjoint from X         -> no root in X.
   - N(X) subset of X             -> unique root in X: existence from the
                                     interval Newton theorem, uniqueness
                                     from 0 not in f'(X) (strict
                                     monotonicity). The enclosure is then
                                     tightened by further Newton
                                     contractions. Non-strict containment
                                     is required so roots lying exactly on
                                     a bisection boundary can be certified.
   - otherwise                    -> intersect and continue, or bisect when
                                     contraction stalls.

Boxes that survive to the width/depth budget without a verdict are reported
as *undecided* — never silently dropped, never promoted to certified.
Certified enclosures that overlap (the same root certified from two
neighbouring boxes) are merged, with every witness retained.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Callable, Optional

from mpmath import mp, mpf

from .errors import (
    CertError,
    DomainEvaluationError,
    InputValidationError,
    StateConflictError,
)
from .evidence import (
    CertifiedRoot,
    RootWitness,
    TraceLog,
    UndecidedInterval,
    new_run_id,
)
from .expressions import (
    differentiate,
    evaluate_interval,
    make_callable,
    parse_expression,
)
from .intervals import (
    Interval,
    contains_zero,
    intersection,
    interval_to_dict,
    is_inside,
    make_interval,
    midpoint,
    point_interval,
    safe_divide,
    scalar,
    width,
)

STATUS_COMPLETED = "completed"
STATUS_RESOURCE_EXHAUSTED = "resource_exhausted"
STATUS_DOMAIN_ERROR = "domain_error"
STATUS_COMPUTE_FAILURE = "compute_failure"

# Newton contraction must shrink a box by at least this factor per step,
# otherwise we bisect instead (avoids crawling along shallow slopes).
_MIN_CONTRACTION = mpf("0.75")
_MAX_TIGHTEN_STEPS = 100

# Epsilon inflation (Rump's trick): when N(X) is not quite contained in X
# (typical for roots exactly on a bisection boundary), re-verify on a
# slightly inflated box. Certification stays rigorous because existence and
# uniqueness are re-established on the inflated box itself.
_INFLATION_REL = mpf("1e-3")
_MAX_INFLATION_ROUNDS = 3


@dataclass
class KernelConfig:
    tol: Any = mpf("1e-12")
    max_depth: int = 50
    max_intervals: int = 10000
    max_steps: int = 20000
    dps: int = 50
    include_trace: bool = True


@dataclass
class CertificationResult:
    run_id: str
    status: str
    certified: list[CertifiedRoot] = field(default_factory=list)
    undecided: list[UndecidedInterval] = field(default_factory=list)
    approximations: list[dict[str, Any]] = field(default_factory=list)
    stats: dict[str, Any] = field(default_factory=dict)
    trace: list[dict[str, Any]] = field(default_factory=list)
    error: Optional[dict[str, Any]] = None

    def to_dict(self) -> dict[str, Any]:
        return {
            "run_id": self.run_id,
            "status": self.status,
            "certified_roots": [c.to_dict() for c in self.certified],
            "undecided": [u.to_dict() for u in self.undecided],
            "approximations": self.approximations,
            "stats": self.stats,
            "trace": self.trace,
            "error": self.error,
        }


def _validate_request(lo, hi, config: KernelConfig) -> None:
    lo_m, hi_m, tol = mpf(lo), mpf(hi), mpf(config.tol)
    if not (lo_m < hi_m):
        raise InputValidationError(f"interval must satisfy lo < hi, got [{lo_m}, {hi_m}]")
    if tol <= 0:
        raise InputValidationError(f"tol must be positive, got {tol}")
    if config.max_depth < 1:
        raise InputValidationError("max_depth must be >= 1")
    if config.max_intervals < 1:
        raise InputValidationError("max_intervals must be >= 1")
    if config.dps < 15:
        raise InputValidationError("dps must be >= 15 for reliable interval arithmetic")
    # A tolerance far below the working precision can never be resolved:
    # individually valid parameters, mutually inconsistent -> state conflict.
    if tol < mpf(10) ** (-(config.dps - 10)):
        raise StateConflictError(
            f"tol={mp.nstr(tol, 10)} is below what dps={config.dps} can resolve; "
            f"raise dps or loosen tol"
        )


def certify_roots(
    expression_text: str,
    variable: str,
    lo,
    hi,
    config: Optional[KernelConfig] = None,
    sink: Optional[Callable[[dict], None]] = None,
) -> CertificationResult:
    """Certify unique roots of ``expression_text`` on [lo, hi].

    Never raises for *expected* failure categories: domain errors and
    resource exhaustion are reported through the result status. Input and
    state-conflict errors are raised (they are caller bugs, not outcomes).
    """
    config = config or KernelConfig()
    _validate_request(lo, hi, config)

    run_id = new_run_id()
    trace = TraceLog(run_id, sink=sink)
    result = CertificationResult(run_id=run_id, status=STATUS_COMPLETED)
    stats = {
        "steps": 0,
        "eliminated": 0,
        "bisections": 0,
        "contractions": 0,
        "max_depth_reached": 0,
    }

    f_ast = parse_expression(expression_text, variable)
    df_ast = differentiate(f_ast, variable)

    with mp.workdps(config.dps):
        try:
            _run_search(f_ast, df_ast, variable, lo, hi, config, trace, result, stats)
        except DomainEvaluationError as exc:
            result.status = STATUS_DOMAIN_ERROR
            result.error = exc.to_dict()
        except CertError as exc:  # pragma: no cover - defensive
            result.status = STATUS_COMPUTE_FAILURE
            result.error = exc.to_dict()

        result.stats = stats
        result.trace = trace.to_list() if config.include_trace else []
        _attach_approximations(f_ast, variable, result)
    return result


def _run_search(f_ast, df_ast, variable, lo, hi, config, trace, result, stats) -> None:
    tol = mpf(config.tol)
    worklist: list[tuple[Interval, int]] = [(make_interval(lo, hi), 0)]

    while worklist:
        if stats["steps"] >= config.max_steps or len(worklist) > config.max_intervals:
            result.status = STATUS_RESOURCE_EXHAUSTED
            for box, depth in worklist:
                result.undecided.append(UndecidedInterval(box, "resource_exhausted"))
                trace.record(stats["steps"], "undecided", box, "resource_exhausted",
                             {"depth": depth})
            return

        box, depth = worklist.pop()
        stats["steps"] += 1
        stats["max_depth_reached"] = max(stats["max_depth_reached"], depth)

        # --- 1. range test: 0 not in f(X) -> no root ---------------------
        f_box = evaluate_interval(f_ast, variable, box)
        if not contains_zero(f_box):
            stats["eliminated"] += 1
            trace.record(stats["steps"], "eliminate", box,
                         "f_interval_excludes_zero",
                         {"f_interval": interval_to_dict(f_box)})
            continue

        # --- 2. derivative test: 0 in f'(X) -> conservative bisection ----
        df_box = evaluate_interval(df_ast, variable, box)
        if contains_zero(df_box):
            if width(box) <= tol:
                result.undecided.append(
                    UndecidedInterval(box, "min_width_derivative_straddles_zero"))
                trace.record(stats["steps"], "undecided", box,
                             "min_width_derivative_straddles_zero",
                             {"df_interval": interval_to_dict(df_box)})
                continue
            if depth >= config.max_depth:
                result.undecided.append(UndecidedInterval(box, "max_depth_reached"))
                trace.record(stats["steps"], "undecided", box, "max_depth_reached",
                             {"depth": depth})
                continue
            _bisect(box, depth, worklist, trace, stats)
            continue

        # --- 3. interval Newton step (0 not in f'(X) here) ----------------
        mid = midpoint(box)
        f_mid = evaluate_interval(f_ast, variable, point_interval(mid))
        newton_image = point_interval(mid) - safe_divide(
            f_mid, df_box, location="kernel/newton_step")
        narrowed = intersection(newton_image, box)

        if narrowed is None:
            stats["eliminated"] += 1
            trace.record(stats["steps"], "eliminate", box,
                         "newton_image_disjoint",
                         {"newton_image": interval_to_dict(newton_image)})
            continue

        if is_inside(newton_image, box):
            certified = _certify_and_tighten(
                f_ast, df_ast, variable, box, newton_image, df_box, f_mid,
                tol, trace, stats)
            _merge_certified(result.certified, certified)
            continue

        inflated = _inflate_and_certify(
            f_ast, df_ast, variable, box, newton_image, tol, trace, stats)
        if inflated is not None:
            _merge_certified(result.certified, inflated)
            continue

        if width(box) <= tol:
            result.undecided.append(
                UndecidedInterval(box, "min_width_newton_image_not_contained"))
            trace.record(stats["steps"], "undecided", box,
                         "min_width_newton_image_not_contained",
                         {"newton_image": interval_to_dict(newton_image)})
            continue

        if width(narrowed) <= _MIN_CONTRACTION * width(box):
            stats["contractions"] += 1
            trace.record(stats["steps"], "contract", box, "newton_contraction",
                         {"narrowed": interval_to_dict(narrowed)})
            worklist.append((narrowed, depth))
            continue

        _bisect(box, depth, worklist, trace, stats)


def _bisect(box, depth, worklist, trace, stats) -> None:
    mid = midpoint(box)
    left = make_interval(box.a, mid)
    right = make_interval(mid, box.b)
    stats["bisections"] += 1
    trace.record(stats["steps"], "bisect", box, "split_at_midpoint",
                 {"left": interval_to_dict(left), "right": interval_to_dict(right)})
    worklist.append((left, depth + 1))
    worklist.append((right, depth + 1))


def _certify_and_tighten(
    f_ast, df_ast, variable, box, newton_image, df_box, f_mid, tol, trace, stats,
    reason: str = "newton_image_contained",
) -> CertifiedRoot:
    """Record the uniqueness witness, then tighten the enclosure.

    Theorem (interval Newton): N(X) ⊆ X implies a root exists in X, and
    0 ∉ f'(X) (strict monotonicity) makes it unique. Further Newton
    intersections can only shrink the enclosure, never lose the root.
    """
    enclosure = intersection(newton_image, box)
    assert enclosure is not None  # guaranteed by the caller's containment test
    witness = RootWitness(
        theorem="interval_newton_contraction",
        initial_interval=interval_to_dict(box),
        derivative_interval=interval_to_dict(df_box),
        f_at_midpoint=interval_to_dict(f_mid),
        newton_image=interval_to_dict(newton_image),
        containment_margin_lo=mp.nstr(scalar(newton_image.a) - scalar(box.a), 20),
        containment_margin_hi=mp.nstr(scalar(box.b) - scalar(newton_image.b), 20),
    )
    stats["contractions"] += 1
    trace.record(stats["steps"], "certify", box, reason, witness.to_dict())

    steps = 0
    while width(enclosure) > tol and steps < _MAX_TIGHTEN_STEPS:
        df_enc = evaluate_interval(df_ast, variable, enclosure)
        if contains_zero(df_enc):
            break
        mid = midpoint(enclosure)
        f_mid_enc = evaluate_interval(f_ast, variable, point_interval(mid))
        image = point_interval(mid) - safe_divide(
            f_mid_enc, df_enc, location="kernel/tighten")
        narrowed = intersection(image, enclosure)
        if narrowed is None or width(narrowed) >= width(enclosure):
            break
        enclosure = narrowed
        steps += 1

    return CertifiedRoot(interval=enclosure, witnesses=[witness],
                         tightening_steps=steps)


def _inflate_and_certify(
    f_ast, df_ast, variable, box, newton_image, tol, trace, stats
) -> CertifiedRoot | None:
    """Epsilon-inflation certification for boundary-hugging roots.

    When N(X) overshoots X by a hair (root on or near the box boundary),
    repeatedly inflate the hull of X and N(X) and re-establish the full
    theorem conditions (0 ∉ f'(X*) and N(X*) ⊆ X*) on the inflated box X*.
    Returns None when containment cannot be established within a few rounds;
    the caller then falls back to contraction/bisection. The certified
    enclosure may extend slightly beyond the original search interval —
    this is reported honestly in the enclosure itself.
    """
    lo = min(scalar(box.a), scalar(newton_image.a))
    hi = max(scalar(box.b), scalar(newton_image.b))
    for _ in range(_MAX_INFLATION_ROUNDS):
        span = hi - lo
        margin = span * _INFLATION_REL + mpf(10) ** (-(mp.dps - 5))
        inflated = make_interval(lo - margin, hi + margin)
        df_inf = evaluate_interval(df_ast, variable, inflated)
        if contains_zero(df_inf):
            return None
        mid = midpoint(inflated)
        f_mid = evaluate_interval(f_ast, variable, point_interval(mid))
        image = point_interval(mid) - safe_divide(
            f_mid, df_inf, location="kernel/inflated_newton")
        if is_inside(image, inflated):
            return _certify_and_tighten(
                f_ast, df_ast, variable, inflated, image, df_inf, f_mid,
                tol, trace, stats, reason="inflated_newton_image_contained")
        lo = min(lo, scalar(image.a))
        hi = max(hi, scalar(image.b))
    return None


def _merge_certified(certified: list[CertifiedRoot], new_root: CertifiedRoot) -> None:
    """Insert ``new_root``, merging enclosures that intersect.

    Two certified enclosures can only intersect when they certify the same
    root (each box's root is unique within its box), so merging is sound.
    """
    merged = new_root
    kept: list[CertifiedRoot] = []
    for existing in certified:
        if intersection(existing.interval, merged.interval) is not None:
            lo = min(scalar(existing.interval.a), scalar(merged.interval.a))
            hi = max(scalar(existing.interval.b), scalar(merged.interval.b))
            merged = CertifiedRoot(
                interval=make_interval(lo, hi),
                witnesses=existing.witnesses + merged.witnesses,
                tightening_steps=existing.tightening_steps + merged.tightening_steps,
            )
        else:
            kept.append(existing)
    kept.append(merged)
    certified[:] = kept


def _attach_approximations(f_ast, variable, result: CertificationResult) -> None:
    """Numerical (NOT certified) root approximations, kept strictly separate
    from the certified enclosures."""
    try:
        f_numeric = make_callable(f_ast, variable)
    except CertError:
        return
    for root in result.certified:
        approx = _findroot_safely(f_numeric, midpoint(root.interval))
        result.approximations.append({
            "kind": "refined_certified",
            "value": approx,
            "source_interval": interval_to_dict(root.interval),
            "method": "mpmath.findroot",
            "certified": False,
        })
    for box in result.undecided:
        approx = _findroot_safely(f_numeric, midpoint(box.interval))
        result.approximations.append({
            "kind": "undecided_candidate",
            "value": approx,
            "source_interval": interval_to_dict(box.interval),
            "method": "mpmath.findroot",
            "certified": False,
        })


def _findroot_safely(f_numeric, x0) -> Optional[str]:
    try:
        with mp.workdps(40):
            return mp.nstr(mp.findroot(f_numeric, mpf(x0)), 40)
    except (ValueError, ZeroDivisionError, ArithmeticError):
        return None
