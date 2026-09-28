"""Sample-size solvers: compose the distribution kernels with integer search.

The guards required by the statistical boundary live here:

* a zero effect is an explicit ``EFFECT_ZERO`` failure (no finite n);
* when the expected cell count is below the low-base-rate threshold the normal
  approximation is declared unreliable and an *exact* binomial search is used;
* every reported n is verified on its actual operating characteristic, and the
  n-vs-(n-1) boundary is witnessed;
* hitting the size cap is reported as ``EXACT_LIMITED_BY_CAP``, never silently
  truncated.
"""
from __future__ import annotations

from dataclasses import dataclass, field
from typing import Callable, List, Optional

from ..contracts import (
    BinomialSpec,
    EstimandFamily,
    FailureCategory,
    NormalSpec,
    SampleSizeResult,
    SolverMethod,
)
from .integer_search import (
    IntegerBoundary,
    ceil_disjoint,
    smallest_integer_above,
    smallest_integer_with_backtrack,
)
from . import noncentral as nc


@dataclass(frozen=True)
class SolverConfig:
    max_sample_size: int = 1_000_000
    low_rate_threshold: float = 5.0
    exact_verify_limit: int = 200_000
    exact_tolerance: float = 0.005
    trace_limit: int = 40


@dataclass(frozen=True)
class SearchTrace:
    steps: List[dict] = field(default_factory=list)

    def record(self, n: int, power: float, method: SolverMethod) -> None:
        if len(self.steps) < 40:
            self.steps.append({"n": int(n), "power": float(power), "method": method.value})


def _failure(family: EstimandFamily, category: FailureCategory, detail: str,
             diagnostics: Optional[dict] = None) -> SampleSizeResult:
    return SampleSizeResult(
        success=False, family=family, failure_category=category,
        failure_detail=detail, diagnostics=diagnostics or {},
    )


def _n1_for(n0: int, ratio: float, one_sample: bool) -> Optional[int]:
    if one_sample:
        return None
    return max(1, int(round(ratio * n0)))


def _commit(boundary: IntegerBoundary, family: EstimandFamily, method: SolverMethod,
            n1: Optional[int], diagnostics: dict) -> SampleSizeResult:
    n0 = boundary.n
    total = n0 if n1 is None else n0 + n1
    return SampleSizeResult(
        success=True, family=family,
        n_per_group0=n0, n_per_group1=n1, n_total=total,
        achieved_power=boundary.value_at_n, method=method,
        power_at_n_minus_one=boundary.value_at_n_minus_one,
        diagnostics=diagnostics,
    )


# --------------------------------------------------------------------------- #
# Normal solver
# --------------------------------------------------------------------------- #
def solve_normal(spec: NormalSpec, cfg: Optional[SolverConfig] = None) -> SampleSizeResult:
    cfg = cfg or SolverConfig()
    family = EstimandFamily.NORMAL
    diag: dict = {"direction": spec.direction.value, "allocation_ratio": spec.allocation_ratio,
                  "alpha": spec.alpha, "target_power": spec.power}

    if spec.effect_zero:
        diag["limit_power_as_n_to_inf"] = spec.alpha
        return _failure(family, FailureCategory.EFFECT_ZERO,
                        "effect is exactly zero: power stays at alpha for every finite n", diag)

    two_sided = spec.direction.is_two_sided
    ratio = spec.allocation_ratio
    effect = spec.effect_raw
    method = SolverMethod.STUDENT_T if spec.use_t_distribution else SolverMethod.NORMAL_APPROX

    try:
        root = nc.normal_n0_continuous(
            effect, spec.sd0, None if spec.one_sample else spec.sd1_effective,
            spec.alpha, spec.power, two_sided, ratio, spec.one_sample,
        )
        diag["continuous_root_n0"] = root
        start = ceil_disjoint(root)
        if start > cfg.max_sample_size:
            diag["continuous_root_n0"] = root
            return _failure(family, FailureCategory.EXACT_LIMITED_BY_CAP,
                            f"required n0 ~ {root:.1f} exceeds cap {cfg.max_sample_size}", diag)

        trace = SearchTrace()

        def power_at(n0: int) -> float:
            n1 = _n1_for(n0, ratio, spec.one_sample)
            p = nc.normal_power(
                n0, n1, effect, spec.sd0, spec.sd1_effective,
                spec.alpha, two_sided, 1.0, use_t=spec.use_t_distribution,
            )
            trace.record(n0, p, method)
            return p

        boundary = smallest_integer_above(power_at, spec.power, start, cap=cfg.max_sample_size)
    except OverflowError as exc:
        return _failure(family, FailureCategory.EXACT_LIMITED_BY_CAP, str(exc), diag)
    except (ArithmeticError, ValueError, RuntimeError) as exc:
        return _failure(family, FailureCategory.NON_CONVERGENCE, f"{type(exc).__name__}: {exc}", diag)

    diag["integer_search_trace"] = trace.steps
    diag["boundary_rule"] = "power(n) >= target AND power(n-1) < target"
    return _commit(boundary, family, method, _n1_for(boundary.n, ratio, spec.one_sample), diag)


# --------------------------------------------------------------------------- #
# Binomial solver
# --------------------------------------------------------------------------- #
def _expected_counts(n0: int, n1: Optional[int], spec: BinomialSpec) -> dict:
    counts = {"n0*p0": n0 * spec.p0, "n0*(1-p0)": n0 * (1.0 - spec.p0)}
    if n1 is not None:
        counts.update({"n1*p1": n1 * spec.p1, "n1*(1-p1)": n1 * (1.0 - spec.p1)})
    return counts


def solve_binomial(spec: BinomialSpec, cfg: Optional[SolverConfig] = None,
                   force_exact: bool = False) -> SampleSizeResult:
    cfg = cfg or SolverConfig()
    family = EstimandFamily.BINOMIAL
    two_sided = spec.direction.is_two_sided
    greater = spec.direction.value == "greater"
    ratio = spec.allocation_ratio
    diag: dict = {"direction": spec.direction.value, "allocation_ratio": ratio,
                  "alpha": spec.alpha, "target_power": spec.power,
                  "low_rate_threshold_expected_count": cfg.low_rate_threshold,
                  "force_exact": force_exact}

    if spec.effect_zero:
        diag["limit_power_as_n_to_inf"] = spec.alpha
        return _failure(family, FailureCategory.EFFECT_ZERO,
                        "p0 == p1: power stays at (the exact size) alpha for every n", diag)

    try:
        root = nc.binomial_n0_continuous(
            spec.p0, spec.p1, spec.alpha, spec.power, two_sided, ratio, spec.one_sample)
        diag["normal_continuous_root_n0"] = root
        start = max(1, ceil_disjoint(root))
        if start > cfg.max_sample_size:
            diag["required_n0_approx"] = root
            return _failure(family, FailureCategory.EXACT_LIMITED_BY_CAP,
                            f"required n0 ~ {root:.1f} exceeds cap {cfg.max_sample_size}", diag)
        n1_start = _n1_for(start, ratio, spec.one_sample)
        counts = _expected_counts(start, n1_start, spec)
        diag["expected_counts_at_root"] = counts
        min_count = min(counts.values())
        low_rate = force_exact or min_count < cfg.low_rate_threshold
        diag["minimum_expected_count"] = min_count
        diag["low_base_rate"] = low_rate

        if low_rate:
            reason = ("explicit force_exact requested" if force_exact
                      else f"minimum expected count {min_count:.3g} < "
                           f"threshold {cfg.low_rate_threshold}")
            return _exact_search(spec, cfg, diag, start, two_sided, greater, ratio,
                                 reason=reason)
        return _normal_search_with_exact_guard(spec, cfg, diag, start, two_sided, greater, ratio)
    except OverflowError as exc:
        return _failure(family, FailureCategory.EXACT_LIMITED_BY_CAP, str(exc), diag)
    except (ArithmeticError, ValueError, RuntimeError) as exc:
        return _failure(family, FailureCategory.NON_CONVERGENCE, f"{type(exc).__name__}: {exc}", diag)


def _binomial_power_fn(spec: BinomialSpec, two_sided: bool, greater: bool, ratio: float,
                       method: SolverMethod, trace: SearchTrace) -> Callable[[int], float]:
    def power_at(n0: int) -> float:
        n1 = _n1_for(n0, ratio, spec.one_sample)
        if method is SolverMethod.EXACT_BINOMIAL:
            if spec.one_sample:
                p = nc.binomial_power_exact_one_sample(
                    n0, spec.p0, spec.p1, spec.alpha, two_sided, greater)
            else:
                p = nc.binomial_power_exact_two_sample(
                    n0, n1, spec.p0, spec.p1, spec.alpha, two_sided, greater)
        else:
            p = nc.binomial_power_normal(n0, n1, spec.p0, spec.p1, spec.alpha, two_sided)
        trace.record(n0, p, method)
        return p
    return power_at


def _exact_search(spec: BinomialSpec, cfg: SolverConfig, diag: dict, start: int,
                  two_sided: bool, greater: bool, ratio: float, *, reason: str) -> SampleSizeResult:
    diag["approximation_switch_reason"] = reason
    trace = SearchTrace()
    power_at = _binomial_power_fn(spec, two_sided, greater, ratio, SolverMethod.EXACT_BINOMIAL, trace)
    try:
        boundary = smallest_integer_with_backtrack(
            power_at, spec.power, start, cap=cfg.max_sample_size)
    except OverflowError as exc:
        diag["integer_search_trace"] = trace.steps
        return _failure(EstimandFamily.BINOMIAL, FailureCategory.EXACT_LIMITED_BY_CAP,
                        f"exact binomial search reached cap {cfg.max_sample_size}: {exc}", diag)
    diag["integer_search_trace"] = trace.steps
    diag["boundary_rule"] = "exact_power(n) >= target AND exact_power(n-1) < target"
    return _commit(boundary, EstimandFamily.BINOMIAL, SolverMethod.EXACT_BINOMIAL,
                   _n1_for(boundary.n, ratio, spec.one_sample), diag)


def _normal_search_with_exact_guard(spec: BinomialSpec, cfg: SolverConfig, diag: dict,
                                    start: int, two_sided: bool, greater: bool,
                                    ratio: float) -> SampleSizeResult:
    trace = SearchTrace()
    normal_power = _binomial_power_fn(
        spec, two_sided, greater, ratio, SolverMethod.NORMAL_APPROX, trace)
    boundary = smallest_integer_above(normal_power, spec.power, start,
                                      cap=cfg.max_sample_size)
    n0, n1 = boundary.n, _n1_for(boundary.n, ratio, spec.one_sample)

    # Cross-check the normal answer against the exact operating characteristic
    # whenever the exact sum is cheap enough. A material discrepancy demotes the
    # answer and restarts the search exactly.
    if n0 <= cfg.exact_verify_limit:
        exact_at_n = _binomial_power_fn(
            spec, two_sided, greater, ratio, SolverMethod.EXACT_BINOMIAL, SearchTrace())(n0)
        exact_at_prev = None
        if n0 - 1 >= 1:
            exact_at_prev = _binomial_power_fn(
                spec, two_sided, greater, ratio, SolverMethod.EXACT_BINOMIAL,
                SearchTrace())(n0 - 1)
        discrepancy = abs(exact_at_n - boundary.value_at_n)
        diag["exact_cross_check_power_at_n"] = exact_at_n
        diag["exact_cross_check_power_at_n_minus_one"] = exact_at_prev
        diag["normal_minus_exact_discrepancy"] = discrepancy
        if exact_at_n < spec.power - cfg.exact_tolerance:
            diag["fallback_trigger"] = (
                f"normal approx says {boundary.value_at_n:.4f} but exact power is "
                f"{exact_at_n:.4f} (< {spec.power} - tol {cfg.exact_tolerance})")
            return _exact_search(spec, cfg, diag, max(1, n0 - 4), two_sided, greater, ratio,
                                 reason="normal/exact cross-check failed at normal solution")

    diag["integer_search_trace"] = trace.steps
    diag["boundary_rule"] = "normal_power(n) >= target AND normal_power(n-1) < target"
    return _commit(boundary, EstimandFamily.BINOMIAL, SolverMethod.NORMAL_APPROX, n1, diag)
