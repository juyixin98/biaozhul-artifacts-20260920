"""Planning orchestration.

Responsibilities separated from the kernels:

* choose the computation method (AUTO switches binomial plans from the normal
  approximation to the exact test when expected counts are too low);
* run the **integer search** over the *total* sample size ``N``, with the arm
  split chosen to track the allocation ratio as closely as possible;
* verify the acceptance predicate directly: the returned ``N`` achieves the
  target power while ``N - 1`` does not;
* classify failures (vanishing effect, approximation invalid, exact cap
  exceeded, no feasible sample) instead of returning a degenerate success.
"""
from __future__ import annotations

import math
from dataclasses import dataclass
from typing import Callable

from .config import Settings, get_settings
from .contracts import (
    Allocation,
    Alternative,
    BinomialSpec,
    MethodPreference,
    NormalSpec,
    PlanResult,
)
from .diagnostics import RunLogger, numerical_versions
from .errors import (
    ApproximationInvalidError,
    EffectTooSmallError,
    ExactCapExceededError,
    NoFeasibleSampleError,
)
from .kernels import binomial as bk
from .kernels import normal as nk

# Safety valve for asymptotic searches (per-arm control size); an effect this
# close to zero is reported as indistinguishable, never as a plan.
_ASYMPTOTIC_SAFETY_CAP_N0 = 10_000_000

PowerFn = Callable[[Allocation], float]


# ---------------------------------------------------------------------------
# Allocation derived from the control-arm size
# ---------------------------------------------------------------------------


def derive_allocation(n0: int, allocation_ratio: float, two_sample: bool) -> Allocation:
    """Derive the integer allocation from the control size ``n0``.

    The contract treats ``n0`` as the integer planning variable and derives
    ``n1 = round(r * n0)`` (at least 1 for two-sample plans).  Because both
    arm sizes are non-decreasing functions of ``n0``, power is monotone in
    ``n0`` - this is what makes the integer boundary search well defined and
    independently re-checkable (n0 passes iff n0-1 fails).  Searching over a
    fixed *total* with tie-breaking, by contrast, is not monotone at odd
    totals and can hide a feasible total inside a bisection.
    """
    if n0 <= 0:
        return Allocation(n0=0, n1=0)
    if not two_sample:
        return Allocation(n0=int(n0), n1=0)
    n1 = max(1, int(math.floor(allocation_ratio * n0 + 0.5)))
    return Allocation(n0=int(n0), n1=int(n1))


def split_total(total: int, allocation_ratio: float, two_sample: bool) -> Allocation:
    """Back-compat helper: nearest-rational allocation for a fixed total.

    Used only for display/probing; the authoritative planner path derives the
    allocation from ``n0`` via :func:`derive_allocation`.
    """
    if total <= 0:
        return Allocation(n0=0, n1=0)
    if not two_sample:
        return Allocation(n0=total, n1=0)
    n0 = max(1, int(math.floor(total / (1.0 + allocation_ratio) + 0.5)))
    return derive_allocation(min(n0, total - 1), allocation_ratio, True)


def n0_cap_for_total(total_cap: int, allocation_ratio: float, two_sample: bool) -> int:
    """Largest n0 whose derived allocation respects a total-sample cap."""
    if not two_sample:
        return max(1, total_cap)
    guess = max(1, int(math.floor(total_cap / (1.0 + allocation_ratio))))
    n0 = guess
    # Correct rounding at the boundary with a couple of local steps.
    while derive_allocation(n0 + 1, allocation_ratio, True).total <= total_cap:
        n0 += 1
    while n0 > 1 and derive_allocation(n0, allocation_ratio, True).total > total_cap:
        n0 -= 1
    return n0


# ---------------------------------------------------------------------------
# Integer search
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class SearchOutcome:
    allocation: Allocation
    achieved_power: float
    power_minus_one: float
    allocation_minus_one: Allocation
    evaluations: int
    trail: tuple[tuple[int, float, str], ...]


def integer_search(
    power_fn: PowerFn,
    start_n0: int,
    allocation_ratio: float,
    two_sample: bool,
    n0_cap: int,
    target_power: float,
    logger: RunLogger,
) -> SearchOutcome:
    """Smallest control size n0 with power >= target; n0-1 must fail.

    Phase 1 (expand): walk the analytic start upward (doubling) until power is
    adequate or the cap binds.  Phase 2 (pinpoint): bisection on the monotone
    power curve fixes the exact integer boundary.
    """
    minimum_n0 = 1
    n0 = max(minimum_n0, min(int(start_n0), n0_cap))
    trail: list[tuple[int, float, str]] = []
    evaluations = 0

    def evaluate(control_size: int, phase: str) -> float:
        nonlocal evaluations
        alloc = derive_allocation(control_size, allocation_ratio, two_sample)
        pwr = float(power_fn(alloc))
        evaluations += 1
        trail.append((control_size, pwr, phase))
        logger.step(
            "power_evaluation",
            phase=phase,
            n0=alloc.n0,
            n1=alloc.n1,
            total=alloc.total,
            power=pwr,
            target=target_power,
            passes=pwr >= target_power,
        )
        return pwr

    power = evaluate(n0, "expand:start")
    if power < target_power:
        low = n0
        step = max(1, n0 // 4)
        while power < target_power and n0 < n0_cap:
            n0 = min(n0_cap, n0 + step)
            power = evaluate(n0, "expand")
            if power >= target_power:
                break
            step *= 2
        if power < target_power:
            raise ExactCapExceededError(
                "integer search reached the sample-size cap without achieving target power",
                details={
                    "cap_n0": n0_cap,
                    "allocation_at_cap": derive_allocation(n0_cap, allocation_ratio, two_sample).as_dict(),
                    "power_at_cap": power,
                    "target_power": target_power,
                    "evaluations": evaluations,
                },
            )
        high = n0
    else:
        low = minimum_n0
        high = n0

    # Bisection: find smallest passing n0 in (low, high].
    while high - low > 1:
        mid = (low + high) // 2
        if mid == low:
            break
        mid_power = evaluate(mid, "bisect")
        if mid_power >= target_power:
            high = mid
            power = mid_power
        else:
            low = mid

    final_alloc = derive_allocation(high, allocation_ratio, two_sample)
    final_power = float(power_fn(final_alloc))
    evaluations += 1
    if high - 1 >= minimum_n0:
        prev_alloc = derive_allocation(high - 1, allocation_ratio, two_sample)
        prev_power = float(power_fn(prev_alloc))
    else:
        prev_alloc = Allocation(n0=0, n1=0)
        prev_power = 0.0
    evaluations += 1
    logger.step(
        "boundary_verification",
        n0=final_alloc.n0,
        n1=final_alloc.n1,
        total=final_alloc.total,
        power=final_power,
        n0_minus_one=high - 1,
        allocation_minus_one=prev_alloc.as_dict(),
        power_minus_one=prev_power,
        target=target_power,
    )
    if final_power < target_power:
        raise NoFeasibleSampleError(
            "final boundary re-check fell below target (non-monotone kernel?)",
            details={"n0": high, "power": final_power, "target": target_power},
        )
    if high - 1 >= minimum_n0 and prev_power >= target_power:
        raise NoFeasibleSampleError(
            "integer minimality violated: n0-1 already achieves target power",
            details={"n0": high, "power": final_power, "power_minus_one": prev_power},
        )
    return SearchOutcome(
        allocation=final_alloc,
        achieved_power=final_power,
        power_minus_one=prev_power,
        allocation_minus_one=prev_alloc,
        evaluations=evaluations,
        trail=tuple(trail),
    )


# ---------------------------------------------------------------------------
# Normal plans
# ---------------------------------------------------------------------------


def plan_normal(
    spec: NormalSpec,
    run_id: str,
    fingerprint: str,
    logger: RunLogger,
    settings: Settings | None = None,
) -> PlanResult:
    settings = settings or get_settings()
    d_abs = abs(spec.standardized_effect)
    if d_abs == 0.0:  # contract already rejects exact zero; defensive only.
        raise EffectTooSmallError("standardized effect is zero", details={"spec": spec.to_dict()})

    n0_cont = nk.continuous_n0(
        d_abs=d_abs,
        alpha=spec.alpha,
        target_power=spec.target_power,
        alternative=spec.alternative,
        two_sample=spec.two_sample,
        allocation_ratio=spec.allocation_ratio,
    )
    logger.step("continuous_estimate", model="normal_z", n0_continuous=n0_cont, d=d_abs)
    if n0_cont > _ASYMPTOTIC_SAFETY_CAP_N0:
        raise EffectTooSmallError(
            "effect is indistinguishable from zero at the requested alpha/power "
            "(continuous estimate exceeds safety cap)",
            details={"n0_continuous": n0_cont, "cap_n0": _ASYMPTOTIC_SAFETY_CAP_N0, "d": d_abs},
        )
    start_n0 = int(math.ceil(n0_cont))

    model = nk.NormalModel(
        d_abs=d_abs,
        alpha=spec.alpha,
        alternative=spec.alternative,
        two_sample=spec.two_sample,
        known_sigma=spec.known_sigma,
    )
    method = "normal_z_known_sigma" if spec.known_sigma else "student_t_noncentral"
    logger.info("method_selected", method=method, two_sample=spec.two_sample)
    outcome = integer_search(
        power_fn=model.power,
        start_n0=start_n0,
        allocation_ratio=spec.allocation_ratio,
        two_sample=spec.two_sample,
        n0_cap=n0_cap_for_total(
            _cap_total(settings, spec.two_sample, exact=False),
            spec.allocation_ratio,
            spec.two_sample,
        ),
        target_power=spec.target_power,
        logger=logger,
    )

    df = (
        outcome.allocation.total - 2
        if spec.two_sample
        else outcome.allocation.n0 - 1
    )
    from scipy.stats import norm as _norm
    from scipy.stats import t as _t

    a_star = spec.alpha / 2.0 if spec.alternative is Alternative.TWO_SIDED else spec.alpha
    if spec.known_sigma:
        crit = float(_norm.ppf(1.0 - a_star))
        lam = (
            d_abs * math.sqrt(outcome.allocation.n0 * outcome.allocation.n1 / outcome.allocation.total)
            if spec.two_sample
            else d_abs * math.sqrt(outcome.allocation.n0)
        )
    else:
        crit = float(_t.ppf(1.0 - a_star, df))
        lam = (
            d_abs * math.sqrt(outcome.allocation.n0 * outcome.allocation.n1 / outcome.allocation.total)
            if spec.two_sample
            else d_abs * math.sqrt(outcome.allocation.n0)
        )
    critical = (crit,) if spec.alternative is not Alternative.TWO_SIDED else (-crit, crit)

    return _build_result(
        endpoint="normal",
        spec=spec,
        run_id=run_id,
        fingerprint=fingerprint,
        logger=logger,
        method=method,
        outcome=outcome,
        noncentrality=lam,
        critical_values=critical,
        warnings=(),
    )


def _cap_total(settings: Settings, two_sample: bool, exact: bool) -> int:
    if not exact:
        return _ASYMPTOTIC_SAFETY_CAP_N0 * (2 if two_sample else 1)
    return settings.exact_two_sample_total_cap if two_sample else settings.exact_one_sample_cap


# ---------------------------------------------------------------------------
# Binomial plans
# ---------------------------------------------------------------------------


def plan_binomial(
    spec: BinomialSpec,
    run_id: str,
    fingerprint: str,
    logger: RunLogger,
    settings: Settings | None = None,
) -> PlanResult:
    settings = settings or get_settings()
    p0, p1 = spec.p0, spec.alternative_p1
    n0_cont = bk.continuous_n0_two_sample(
        p0, p1, spec.alpha, spec.target_power, spec.alternative, spec.allocation_ratio
    ) if spec.two_sample else bk.continuous_n0_one_sample(
        p0, p1, spec.alpha, spec.target_power, spec.alternative
    )
    start_n0 = int(math.ceil(n0_cont))
    probe_alloc = derive_allocation(max(1, start_n0), spec.allocation_ratio, spec.two_sample)
    min_expected = bk.min_expected_count(p0, p1, probe_alloc, spec.two_sample)
    logger.step(
        "continuous_estimate",
        model="binomial_normal_approx",
        n0_continuous=n0_cont,
        start_n0=start_n0,
        min_expected_count=min_expected,
        p0=p0,
        p1=p1,
    )

    use_exact, switch_warning = _select_method(spec, min_expected, logger)

    if not use_exact and n0_cont > _ASYMPTOTIC_SAFETY_CAP_N0:
        raise EffectTooSmallError(
            "effect is indistinguishable from zero at the requested alpha/power "
            "(continuous estimate exceeds safety cap)",
            details={"n0_continuous": n0_cont, "cap_n0": _ASYMPTOTIC_SAFETY_CAP_N0, "p0": p0, "p1": p1},
        )

    warnings: list[str] = []
    if switch_warning:
        warnings.append(switch_warning)

    def run_search(exact: bool) -> tuple[SearchOutcome, PowerFn, str]:
        power_fn, method, total_cap = _build_binomial_kernel(
            spec, p0, p1, exact, settings, logger
        )
        outcome = integer_search(
            power_fn=power_fn,
            start_n0=start_n0,
            allocation_ratio=spec.allocation_ratio,
            two_sample=spec.two_sample,
            n0_cap=n0_cap_for_total(total_cap, spec.allocation_ratio, spec.two_sample),
            target_power=spec.target_power,
            logger=logger,
        )
        return outcome, power_fn, method

    outcome, _power_fn, method = run_search(use_exact)

    # Re-validate expected counts at the final integer allocation.  In AUTO
    # mode a late failure means the approximation only became invalid at the
    # boundary - switch to exact and re-search once.  A FORCED asymptotic plan
    # is failed explicitly with the approximation_invalid category instead of
    # returning a risky answer as success.
    if not use_exact:
        final_expected = bk.min_expected_count(p0, p1, outcome.allocation, spec.two_sample)
        if final_expected < settings.approx_min_expected:
            if spec.method_preference is MethodPreference.ASYMPTOTIC:
                raise ApproximationInvalidError(
                    "normal approximation invalid at the final integer allocation "
                    "(expected cell count below threshold) and asymptotic was FORCED; "
                    "use method_preference=auto or exact",
                    details={
                        "min_expected_count": final_expected,
                        "threshold": settings.approx_min_expected,
                        "allocation": outcome.allocation.as_dict(),
                    },
                )
            warnings.append(
                f"normal approximation invalid at the final allocation "
                f"(min expected count {final_expected:.3f} < {settings.approx_min_expected}); "
                "re-searched with the exact test"
            )
            logger.warning("method_switch_late", to_method="exact",
                           min_expected_count=final_expected,
                           threshold=settings.approx_min_expected)
            use_exact = True
            outcome, _power_fn, method = run_search(True)

    noncentrality, critical = _binomial_summary(spec, p0, p1, outcome, use_exact)
    return _build_result(
        endpoint="binomial",
        spec=spec,
        run_id=run_id,
        fingerprint=fingerprint,
        logger=logger,
        method=method,
        outcome=outcome,
        noncentrality=noncentrality,
        critical_values=critical,
        warnings=tuple(warnings),
    )


def _select_method(spec: BinomialSpec, min_expected: float, logger: RunLogger) -> tuple[bool, str | None]:
    """Decide exact vs asymptotic. AUTO switches at the expected-count limit."""
    threshold = get_settings().approx_min_expected
    if spec.method_preference is MethodPreference.EXACT:
        logger.info("method_selected", method="exact", reason="user_forced")
        return True, None
    if spec.method_preference is MethodPreference.ASYMPTOTIC:
        logger.info("method_selected", method="asymptotic", reason="user_forced",
                    min_expected_count=min_expected, threshold=threshold)
        return False, None
    if min_expected < threshold:
        warning = (
            f"normal approximation invalid: min expected count {min_expected:.3f} "
            f"< threshold {threshold}; switched to exact test (low base rate)"
        )
        logger.warning("method_switch", from_method="asymptotic", to_method="exact",
                       min_expected_count=min_expected, threshold=threshold)
        return True, warning
    logger.info("method_selected", method="asymptotic", reason="expected_counts_adequate",
                min_expected_count=min_expected, threshold=threshold)
    return False, None


def _build_binomial_kernel(
    spec: BinomialSpec, p0: float, p1: float, use_exact: bool, settings: Settings, logger: RunLogger
) -> tuple[PowerFn, str, int]:
    if not use_exact:
        if spec.two_sample:
            kernel = bk.BinomialAsymptoticTwoSample(p0, p1, spec.alpha, spec.alternative)
            return kernel.power, "binomial_normal_approx_two_sample", _cap_total(settings, True, False)
        kernel = bk.BinomialAsymptoticOneSample(p0, p1, spec.alpha, spec.alternative)
        return kernel.power, "binomial_normal_approx_one_sample", _cap_total(settings, False, False)
    if spec.two_sample:
        kernel = bk.BinomialExactTwoSample(p0, p1, spec.alpha, spec.alternative)
        return kernel.power, "fisher_exact_two_sample", settings.exact_two_sample_total_cap
    kernel = bk.BinomialExactOneSample(p0, p1, spec.alpha, spec.alternative)
    return kernel.power, "binomial_exact_one_sample", settings.exact_one_sample_cap


def _binomial_summary(
    spec: BinomialSpec, p0: float, p1: float, outcome: SearchOutcome, use_exact: bool
) -> tuple[float | None, tuple[float, ...]]:
    alloc = outcome.allocation
    a_star = spec.alpha / 2.0 if spec.alternative is Alternative.TWO_SIDED else spec.alpha
    from scipy.stats import norm as _norm

    zcrit = float(_norm.ppf(1.0 - a_star))
    if not use_exact:
        if spec.two_sample:
            p_bar = (alloc.n0 * p0 + alloc.n1 * p1) / alloc.total
            se = math.sqrt(p_bar * (1 - p_bar) * (1.0 / alloc.n0 + 1.0 / alloc.n1))
        else:
            se = math.sqrt(p0 * (1.0 - p0) / alloc.n0)
        lam = (p1 - p0) / se
        crit = (zcrit,) if spec.alternative is not Alternative.TWO_SIDED else (-zcrit, zcrit)
        return lam, crit
    if not spec.two_sample:
        c_low, c_high, size = bk.BinomialExactOneSample(p0, p1, spec.alpha, spec.alternative).critical_region(alloc)
        return None, tuple(float(v) for v in (c_low, c_high, size))
    return None, ()


# ---------------------------------------------------------------------------
# Result assembly
# ---------------------------------------------------------------------------


def _build_result(
    *,
    endpoint: str,
    spec,
    run_id: str,
    fingerprint: str,
    logger: RunLogger,
    method: str,
    outcome: SearchOutcome,
    noncentrality: float | None,
    critical_values: tuple[float, ...],
    warnings: tuple[str, ...],
) -> PlanResult:
    if outcome.power_minus_one >= spec.target_power and outcome.allocation.total > 1:
        # The search guarantees this cannot happen; treat it as a hard fault
        # rather than reporting a non-minimal answer as success.
        raise NoFeasibleSampleError(
            "integer minimality violated: N-1 already achieves target power",
            details={
                "total": outcome.allocation.total,
                "power": outcome.achieved_power,
                "power_minus_one": outcome.power_minus_one,
                "target": spec.target_power,
            },
        )
    result = PlanResult(
        endpoint=endpoint,
        run_id=run_id,
        fingerprint=fingerprint,
        allocation=outcome.allocation,
        achieved_power=outcome.achieved_power,
        target_power=spec.target_power,
        alpha=spec.alpha,
        alternative=spec.alternative,
        method=method,
        noncentrality=noncentrality,
        critical_values=critical_values,
        power_at_total_minus_one=outcome.power_minus_one,
        allocation_minus_one=outcome.allocation_minus_one,
        warnings=warnings,
        steps=(
            {"search_evaluations": outcome.evaluations},
            {"trail_tail": list(outcome.trail[-6:])},
        ),
        spec=spec.to_dict(),
        versions=numerical_versions(),
    )
    logger.info(
        "plan_completed",
        method=method,
        allocation=result.allocation.as_dict(),
        achieved_power=result.achieved_power,
        power_minus_one=result.power_at_total_minus_one,
        minimal=result.is_committed(),
        warnings=list(warnings),
    )
    return result
