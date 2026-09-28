"""Evidence: independent Monte-Carlo simulated power.

The kernels compute power from distributions.  This module measures power by
generating synthetic data under the alternative and applying the actual test -
an independent route that shares no power math with the kernels (only the
contract and the test's stated critical rule).

Simulations are seeded and chunked so logs show progress; results carry the
Monte-Carlo standard error and a 95% interval.  Errors abort the run with
:class:`SimulationError` rather than being reported as evidence.
"""
from __future__ import annotations

import math
from dataclasses import dataclass

import numpy as np
from scipy.stats import hypergeom, norm, t

from .contracts import (
    Alternative,
    Allocation,
    BinomialSpec,
    NormalSpec,
)
from .diagnostics import RunLogger, numerical_versions
from .errors import SimulationError
from .kernels import binomial as bk

_CHUNK = 2_000


@dataclass(frozen=True)
class SimulationEvidence:
    estimated_power: float
    trials: int
    rejections: int
    mc_standard_error: float
    ci95_low: float
    ci95_high: float
    seed: int
    target_power: float | None
    meets_target_within_mcse: bool | None
    versions: dict[str, str]

    def to_dict(self) -> dict:
        return {
            "estimated_power": self.estimated_power,
            "trials": self.trials,
            "rejections": self.rejections,
            "mc_standard_error": self.mc_standard_error,
            "ci95": [self.ci95_low, self.ci95_high],
            "seed": self.seed,
            "target_power": self.target_power,
            "meets_target_within_mcse": self.meets_target_within_mcse,
            "versions": self.versions,
        }


def _critical_z(alpha: float, alternative: Alternative) -> float:
    a_star = alpha / 2.0 if alternative is Alternative.TWO_SIDED else alpha
    return float(norm.ppf(1.0 - a_star))


def _reject_flags(
    stats: np.ndarray, crit: float, alternative: Alternative
) -> np.ndarray:
    if alternative is Alternative.TWO_SIDED:
        return np.abs(stats) >= crit
    if alternative is Alternative.GREATER:
        return stats >= crit
    return stats <= -crit


def simulate_normal_power(
    spec: NormalSpec,
    allocation: Allocation,
    trials: int,
    seed: int,
    logger: RunLogger,
) -> SimulationEvidence:
    """Generate normal samples under the alternative and run the z/t test."""
    if trials <= 0:
        raise SimulationError("trials must be positive", details={"trials": trials})
    rng = np.random.default_rng(seed)
    d = spec.standardized_effect
    # Work on the unit-variance scale: standardized effect d = effect/sigma.
    shift = abs(d)
    if spec.alternative is Alternative.LESS:
        shift = -shift
    zcrit = _critical_z(spec.alpha, spec.alternative)
    n0, n1 = allocation.n0, allocation.n1
    rejections = 0
    done = 0
    logger.step("simulation_start", endpoint="normal", trials=trials, seed=seed,
                allocation=allocation.as_dict(), chunk=_CHUNK)
    while done < trials:
        b = min(_CHUNK, trials - done)
        if spec.two_sample:
            # Control arm at the null mean; treatment arm shifted.
            x0 = rng.normal(loc=0.0, scale=1.0, size=(b, n0))
            x1 = rng.normal(loc=shift, scale=1.0, size=(b, n1))
            if spec.known_sigma:
                se = math.sqrt(1.0 / n0 + 1.0 / n1)
                stats = (x1.mean(axis=1) - x0.mean(axis=1)) / se
                crit = zcrit
            else:
                s0 = x0.std(axis=1, ddof=1)
                s1 = x1.std(axis=1, ddof=1)
                df = n0 + n1 - 2
                sp2 = ((n0 - 1) * s0**2 + (n1 - 1) * s1**2) / df
                stats = (x1.mean(axis=1) - x0.mean(axis=1)) / np.sqrt(
                    sp2 * (1.0 / n0 + 1.0 / n1)
                )
                crit = float(t.ppf(
                    1.0 - (spec.alpha / 2.0 if spec.alternative is Alternative.TWO_SIDED else spec.alpha),
                    df,
                ))
        else:
            # One-sample: observations drawn under the alternative mean.
            x = rng.normal(loc=shift, scale=1.0, size=(b, n0))
            if spec.known_sigma:
                stats = x.mean(axis=1) * math.sqrt(n0)
                crit = zcrit
            else:
                df = n0 - 1
                stats = x.mean(axis=1) / (x.std(axis=1, ddof=1) / math.sqrt(n0))
                crit = float(t.ppf(
                    1.0 - (spec.alpha / 2.0 if spec.alternative is Alternative.TWO_SIDED else spec.alpha),
                    df,
                ))
        flags = _reject_flags(stats, crit, spec.alternative)
        rejections += int(flags.sum())
        done += b
        logger.info("simulation_progress", completed=done, trials=trials,
                    cumulative_rejections=rejections)
    return _evidence(rejections, trials, seed, spec.target_power, logger)


def simulate_binomial_power_asymptotic(
    spec: BinomialSpec,
    allocation: Allocation,
    trials: int,
    seed: int,
    logger: RunLogger,
) -> SimulationEvidence:
    """Generate binomial counts and apply the normal score statistic."""
    if trials <= 0:
        raise SimulationError("trials must be positive", details={"trials": trials})
    rng = np.random.default_rng(seed)
    p0, p1 = spec.p0, spec.alternative_p1
    n0, n1 = allocation.n0, allocation.n1
    crit = _critical_z(spec.alpha, spec.alternative)
    rejections = 0
    done = 0
    logger.step("simulation_start", endpoint="binomial_asymptotic", trials=trials, seed=seed,
                allocation=allocation.as_dict(), chunk=_CHUNK)
    while done < trials:
        b = min(_CHUNK, trials - done)
        x0 = rng.binomial(n0, p0, size=b)
        if not spec.two_sample:
            phat = x0 / n0
            stats = (phat - p0) / math.sqrt(p0 * (1.0 - p0) / n0)
        else:
            x1 = rng.binomial(n1, p1, size=b)
            p_bar = (x0 + x1) / (n0 + n1)
            se = np.sqrt(p_bar * (1.0 - p_bar) * (1.0 / n0 + 1.0 / n1))
            stats = (x1 / n1 - x0 / n0) / se
        rejections += int(_reject_flags(stats, crit, spec.alternative).sum())
        done += b
        logger.info("simulation_progress", completed=done, trials=trials,
                    cumulative_rejections=rejections)
    return _evidence(rejections, trials, seed, spec.target_power, logger)


def simulate_binomial_power_exact(
    spec: BinomialSpec,
    allocation: Allocation,
    trials: int,
    seed: int,
    logger: RunLogger,
) -> SimulationEvidence:
    """Generate binomial counts and apply the exact test directly.

    One sample: compare the count with the exact binomial critical region.
    Two samples: evaluate Fisher's conditional tail for each simulated table.
    """
    if trials <= 0:
        raise SimulationError("trials must be positive", details={"trials": trials})
    rng = np.random.default_rng(seed)
    p0, p1 = spec.p0, spec.alternative_p1
    n0, n1 = allocation.n0, allocation.n1
    two_sided = spec.alternative is Alternative.TWO_SIDED
    tail = spec.alpha / 2.0 if two_sided else spec.alpha
    rejections = 0
    done = 0
    logger.step("simulation_start", endpoint="binomial_exact", trials=trials, seed=seed,
                allocation=allocation.as_dict(), chunk=_CHUNK)

    if not spec.two_sample:
        c_low, c_high, _ = bk._exact_one_sample_critical(n0, p0, spec.alpha, spec.alternative)
        while done < trials:
            b = min(_CHUNK, trials - done)
            x = rng.binomial(n0, p1, size=b)
            flags = np.zeros(b, dtype=bool)
            if c_low >= 0:
                flags |= x <= c_low
            if c_high <= n0:
                flags |= x >= c_high
            rejections += int(flags.sum())
            done += b
            logger.info("simulation_progress", completed=done, trials=trials,
                        cumulative_rejections=rejections)
    else:
        while done < trials:
            b = min(_CHUNK, trials - done)
            x0 = rng.binomial(n0, p0, size=b)
            x1 = rng.binomial(n1, p1, size=b)
            s = x0 + x1
            total = n0 + n1
            sf = np.asarray(hypergeom.sf(x1 - 1, total, n1, s), dtype=float)
            cdf = np.asarray(hypergeom.cdf(x1, total, n1, s), dtype=float)
            if two_sided:
                flags = (sf <= tail) | (cdf <= tail)
            elif spec.alternative is Alternative.GREATER:
                flags = sf <= tail
            else:
                flags = cdf <= tail
            rejections += int(flags.sum())
            done += b
            logger.info("simulation_progress", completed=done, trials=trials,
                        cumulative_rejections=rejections)
    return _evidence(rejections, trials, seed, spec.target_power, logger)


def _evidence(
    rejections: int,
    trials: int,
    seed: int,
    target_power: float | None,
    logger: RunLogger,
) -> SimulationEvidence:
    p_hat = rejections / trials
    if not (0.0 <= p_hat <= 1.0):  # pragma: no cover - defensive
        raise SimulationError("simulated power outside [0,1]", details={"power": p_hat})
    mcse = math.sqrt(p_hat * (1.0 - p_hat) / trials)
    z = 1.959963984540054
    evidence = SimulationEvidence(
        estimated_power=p_hat,
        trials=trials,
        rejections=rejections,
        mc_standard_error=mcse,
        ci95_low=max(0.0, p_hat - z * mcse),
        ci95_high=min(1.0, p_hat + z * mcse),
        seed=seed,
        target_power=target_power,
        meets_target_within_mcse=None if target_power is None else p_hat + z * mcse >= target_power,
        versions=numerical_versions(),
    )
    logger.info(
        "simulation_complete",
        estimated_power=evidence.estimated_power,
        mcse=evidence.mc_standard_error,
        ci95=[evidence.ci95_low, evidence.ci95_high],
        target=target_power,
    )
    return evidence
