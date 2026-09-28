"""Numerical verification: gradient checks and sharded-vs-reference parity.

Two independent oracles are used:

* finite differences over the analytic backward pass;
* :class:`ReferenceAdam`, a separate single-process tensor-keyed Adam path.

Reports carry the request id, the exact stage/version being checked and keep
hard failures separate from uncertain conclusions (e.g. tolerances nearly
exceeded but not crossed).
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any

import numpy as np

from .graph import MLPModule, mse_loss_and_grad

STATUS_PASS = "pass"
STATUS_FAIL = "fail"
STATUS_UNCERTAIN = "uncertain"

# Tight tolerances are legitimate because both paths execute in float64 and
# should be bit-near-identical (only slicing/concatenation differs).
EQUAL_RTOL = 1e-10
EQUAL_ATOL = 1e-12
UNCERTAIN_MARGIN = 50.0  # within 50x of the tolerance band -> flagged uncertain


@dataclass(frozen=True)
class CheckEntry:
    name: str
    status: str
    detail: dict[str, Any] = field(default_factory=dict)


@dataclass(frozen=True)
class VerificationReport:
    request_id: str
    stage: str
    version: str
    location: str
    checks: tuple[CheckEntry, ...]

    @property
    def status(self) -> str:
        if any(c.status == STATUS_FAIL for c in self.checks):
            return STATUS_FAIL
        if any(c.status == STATUS_UNCERTAIN for c in self.checks):
            return STATUS_UNCERTAIN
        return STATUS_PASS

    @property
    def failures(self) -> tuple[CheckEntry, ...]:
        return tuple(c for c in self.checks if c.status == STATUS_FAIL)

    @property
    def uncertainties(self) -> tuple[CheckEntry, ...]:
        return tuple(c for c in self.checks if c.status == STATUS_UNCERTAIN)

    def to_dict(self) -> dict[str, Any]:
        return {
            "request_id": self.request_id,
            "stage": self.stage,
            "version": self.version,
            "location": self.location,
            "status": self.status,
            "checks": [
                {"name": c.name, "status": c.status, "detail": c.detail} for c in self.checks
            ],
            "failures": [
                {"name": c.name, "detail": c.detail, "reason": c.detail.get("reason", "")}
                for c in self.failures
            ],
            "uncertainties": [
                {"name": c.name, "detail": c.detail, "reason": c.detail.get("reason", "")}
                for c in self.uncertainties
            ],
        }


def max_abs_rel_diff(actual: np.ndarray, expected: np.ndarray) -> tuple[float, float]:
    actual = np.asarray(actual, dtype=np.float64)
    expected = np.asarray(expected, dtype=np.float64)
    if actual.shape != expected.shape:
        raise ValueError(f"shape mismatch in comparison: {actual.shape} vs {expected.shape}")
    abs_diff = np.abs(actual - expected)
    scale = np.maximum(np.abs(expected), 1.0)
    return float(abs_diff.max()), float((abs_diff / scale).max())


def _classify_tolerance(abs_err: float, rel_err: float, rtol: float, atol: float) -> tuple[str, str]:
    if abs_err > atol or rel_err > rtol:
        return STATUS_FAIL, (
            f"abs_err={abs_err:.3e} or rel_err={rel_err:.3e} exceeded "
            f"rtol={rtol:.0e}/atol={atol:.0e}"
        )
    if abs_err > atol / UNCERTAIN_MARGIN or rel_err > rtol / UNCERTAIN_MARGIN:
        return STATUS_UNCERTAIN, "error within 50x of tolerance band"
    return STATUS_PASS, ""


def assert_arrays_equal(actual: np.ndarray, expected: np.ndarray, label: str) -> dict[str, float]:
    abs_err, rel_err = max_abs_rel_diff(actual, expected)
    status, reason = _classify_tolerance(abs_err, rel_err, EQUAL_RTOL, EQUAL_ATOL)
    if status == STATUS_FAIL:
        raise AssertionError(f"{label}: {reason}")
    return {"abs_err": abs_err, "rel_err": rel_err, "status": status, "reason": reason}


def finite_difference_gradient_check(
    model: MLPModule,
    x: np.ndarray,
    y: np.ndarray,
    *,
    eps: float = 1e-6,
    max_probes: int = 24,
    seed: int = 0,
) -> CheckEntry:
    """Compare analytic backward gradients against central finite differences.

    A stratified subset of elements across every named parameter is probed to
    keep the check fast while covering each tensor's identity.
    """

    def loss_of(params: dict[str, np.ndarray]) -> float:
        saved = model.snapshot()
        model.install(params)
        try:
            pred = model.forward(x)
            loss, _ = mse_loss_and_grad(pred, y)
        finally:
            model.install(saved)
        return loss

    pred = model.forward(x)
    _, grad_out = mse_loss_and_grad(pred, y)
    analytic = model.backward(x, grad_out)

    rng = np.random.default_rng(seed)
    probes: list[tuple[str, tuple[int, ...], float, float]] = []
    for name in sorted(analytic):
        flat_idx = np.arange(analytic[name].size)
        chosen = rng.choice(flat_idx, size=min(max_probes // len(analytic) + 1, analytic[name].size), replace=False)
        for idx in chosen:
            probes.append((name, tuple(np.unravel_index(idx, analytic[name].shape)), 0.0, 0.0))

    worst_abs = 0.0
    worst_rel = 0.0
    for name, multi_idx, _, _ in probes:
        params_plus = model.snapshot()
        params_minus = model.snapshot()
        params_plus[name][multi_idx] += eps
        params_minus[name][multi_idx] -= eps
        numerical = (loss_of(params_plus) - loss_of(params_minus)) / (2.0 * eps)
        exact = float(analytic[name][multi_idx])
        denom = max(abs(exact), 1.0)
        worst_abs = max(worst_abs, abs(numerical - exact))
        worst_rel = max(worst_rel, abs(numerical - exact) / denom)

    # Finite differencing carries O(eps^2) truncation error, hence a looser band.
    status, reason = _classify_tolerance(worst_abs, worst_rel, rtol=1e-5, atol=1e-7)
    return CheckEntry(
        name="finite_difference_gradient",
        status=status,
        detail={
            "reason": reason,
            "probes": len(probes),
            "eps": eps,
            "max_abs_err": worst_abs,
            "max_rel_err": worst_rel,
        },
    )


def compare_param_dicts(
    actual: dict[str, np.ndarray],
    expected: dict[str, np.ndarray],
    label: str,
) -> CheckEntry:
    if set(actual) != set(expected):
        only_a = sorted(set(actual) - set(expected))
        only_e = sorted(set(expected) - set(actual))
        return CheckEntry(
            name=label,
            status=STATUS_FAIL,
            detail={"reason": "parameter name sets differ", "only_actual": only_a, "only_expected": only_e},
        )
    worst_abs = worst_rel = 0.0
    bad_shape = None
    for name in sorted(actual):
        if actual[name].shape != expected[name].shape:
            bad_shape = name
            break
        a, r = max_abs_rel_diff(actual[name], expected[name])
        worst_abs, worst_rel = max(worst_abs, a), max(worst_rel, r)
    if bad_shape:
        return CheckEntry(
            name=label,
            status=STATUS_FAIL,
            detail={"reason": f"shape mismatch on {bad_shape}", "parameter": bad_shape},
        )
    status, reason = _classify_tolerance(worst_abs, worst_rel, EQUAL_RTOL, EQUAL_ATOL)
    return CheckEntry(
        name=label,
        status=status,
        detail={"reason": reason, "max_abs_err": worst_abs, "max_rel_err": worst_rel},
    )
