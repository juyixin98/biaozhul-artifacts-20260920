"""Numerical validation with answers generated independently of the core.

The reference trajectory here is written from scratch (its own forward pass,
softmax, gradient and flat-vector Adam). It never imports :mod:`adam_shards`
core optimizer/graph/sharding code, so the expected answer is not produced by
the implementation under test. Gradients from the core graph are additionally
cross-checked against central finite differences.
"""
from __future__ import annotations

from dataclasses import dataclass

import numpy as np


@dataclass(frozen=True)
class ParamCheck:
    name: str
    max_abs_diff: float
    rms_diff: float
    passed: bool


@dataclass(frozen=True)
class VerificationReport:
    request_id: str
    stage: str
    passed: bool
    tol: float
    checks: tuple[ParamCheck, ...]
    failures: tuple[str, ...]
    uncertainties: tuple[str, ...]

    def to_dict(self) -> dict:
        return {
            "request_id": self.request_id,
            "stage": self.stage,
            "passed": self.passed,
            "tol": self.tol,
            "max_abs_diff": max((c.max_abs_diff for c in self.checks), default=0.0),
            "checks": [
                {"name": c.name, "max_abs_diff": c.max_abs_diff,
                 "rms_diff": c.rms_diff, "passed": c.passed}
                for c in self.checks
            ],
            "failures": list(self.failures),
            "uncertainties": list(self.uncertainties),
        }


# --------------------------------------------------------------------------- #
# Independent reference model (plain functions, no core imports)
# --------------------------------------------------------------------------- #
def _ref_forward(params: dict[str, np.ndarray], x: np.ndarray,
                 n_layers: int) -> np.ndarray:
    h = x
    for i in range(n_layers):
        z = h @ params[f"layers.{i}.weight"].T + params[f"layers.{i}.bias"]
        h = np.maximum(z, 0.0) if i < n_layers - 1 else z
    return z


def _ref_softmax(logits: np.ndarray) -> np.ndarray:
    z = logits - np.max(logits, axis=1, keepdims=True)
    e = np.exp(z)
    return e / np.sum(e, axis=1, keepdims=True)


def _ref_loss(params: dict[str, np.ndarray], x: np.ndarray, y: np.ndarray,
              n_layers: int) -> float:
    p = _ref_softmax(_ref_forward(params, x, n_layers))
    return float(-np.mean(np.log(p[np.arange(len(y)), y] + 1e-300)))


def _ref_grads(params: dict[str, np.ndarray], x: np.ndarray, y: np.ndarray,
               n_layers: int) -> dict[str, np.ndarray]:
    """Independent backward pass (autograd-style local derivation)."""
    acts, pres = [x], []
    h = x
    for i in range(n_layers):
        z = h @ params[f"layers.{i}.weight"].T + params[f"layers.{i}.bias"]
        pres.append(z)
        h = np.maximum(z, 0.0) if i < n_layers - 1 else z
        if i < n_layers - 1:
            acts.append(h)
    b = len(y)
    p = _ref_softmax(pres[-1])
    p[np.arange(b), y] -= 1.0
    upstream = p / b
    grads: dict[str, np.ndarray] = {}
    for i in range(n_layers - 1, -1, -1):
        grads[f"layers.{i}.weight"] = upstream.T @ acts[i]
        grads[f"layers.{i}.bias"] = np.sum(upstream, axis=0)
        if i:
            upstream = (upstream @ params[f"layers.{i}.weight"]) * (pres[i - 1] > 0)
    return grads


def reference_adam_run(
    init_params: dict[str, np.ndarray],
    batches: list[tuple[np.ndarray, np.ndarray]],
    n_layers: int,
    lr: float,
    beta1: float,
    beta2: float,
    eps: float,
    init_moments: dict[str, tuple[np.ndarray, np.ndarray, int]] | None = None,
) -> dict:
    """Fully independent expected state after the given batches.

    Operates on flat 1-D concatenations per parameter triple with bias
    correction applied elementwise — a deliberately different code shape from
    the production optimizer.
    """
    params = {k: v.astype(np.float64).copy() for k, v in init_params.items()}
    m: dict[str, np.ndarray] = {}
    v: dict[str, np.ndarray] = {}
    steps: dict[str, int] = {}
    for name, arr in params.items():
        if init_moments and name in init_moments:
            m0, v0, t0 = init_moments[name]
            m[name], v[name], steps[name] = m0.copy(), v0.copy(), int(t0)
        else:
            m[name] = np.zeros_like(arr)
            v[name] = np.zeros_like(arr)
            steps[name] = 0

    for x, y in batches:
        g = _ref_grads(params, x, y, n_layers)
        for name in params:
            flat_p = params[name].reshape(-1)
            flat_g = g[name].reshape(-1)
            mm = m[name].reshape(-1)
            vv = v[name].reshape(-1)
            for j in range(flat_p.size):  # explicit element loop, independent path
                mm[j] = beta1 * mm[j] + (1.0 - beta1) * flat_g[j]
                vv[j] = beta2 * vv[j] + (1.0 - beta2) * flat_g[j] * flat_g[j]
            t = steps[name] + 1
            bc1, bc2 = 1.0 - beta1**t, 1.0 - beta2**t
            hat_m = mm / bc1
            hat_v = vv / bc2
            flat_p -= lr * hat_m / (np.sqrt(hat_v) + eps)
            params[name] = flat_p.reshape(params[name].shape)
            steps[name] = t
    moments = {n: (m[n], v[n], steps[n]) for n in params}
    return {"params": params, "moments": moments}


# --------------------------------------------------------------------------- #
# Finite-difference gradient check (independent oracle for core gradients)
# --------------------------------------------------------------------------- #
def finite_difference_check(
    loss_fn, params: dict[str, np.ndarray], grads: dict[str, np.ndarray],
    *, eps: float = 1e-6, sample: int = 32, seed: int = 7,
    significance: float = 1e-7,
) -> tuple[float, list[str]]:
    """Central differences on ``sample`` random coordinates.

    Returns (max relative error, uncertainty notes). Relative error uses the
    symmetric denominator ``|numeric| + |analytic|`` (which is bounded in
    [0, 1]). Coordinates where both sides are below ``significance`` carry no
    meaningful relative error (the quotient is dominated by round-off), so
    they are skipped rather than counted as failures; a high skip fraction is
    reported as an uncertainty.
    """
    rng = np.random.default_rng(seed)
    worst, notes = 0.0, []
    total = sum(a.size for a in params.values())
    idxs = rng.choice(total, size=min(sample, total), replace=False)
    flat_index = _FlatIndex(params)
    significant = skipped = 0
    for idx in idxs:
        name, pos = flat_index.locate(int(idx))
        original = params[name][pos]
        params[name][pos] = original + eps
        plus = loss_fn(params)
        params[name][pos] = original - eps
        minus = loss_fn(params)
        params[name][pos] = original
        numeric = (plus - minus) / (2.0 * eps)
        analytic = float(grads[name][pos])
        scale = abs(numeric) + abs(analytic)
        if scale < significance:
            skipped += 1
            continue
        significant += 1
        worst = max(worst, abs(numeric - analytic) / (scale + 1e-300))
    if skipped:
        notes.append(
            f"{skipped}/{skipped + significant} finite-difference probes sat on "
            f"near-zero gradients and were skipped (relative error undefined)"
        )
    if significant and worst > 1e-4:
        notes.append(f"finite-difference relative error {worst:.2e} exceeds 1e-4")
    return worst, notes


class _FlatIndex:
    def __init__(self, params: dict[str, np.ndarray]) -> None:
        self.names = sorted(params)
        self.shapes = {n: params[n].shape for n in self.names}
        self.bounds = []
        cursor = 0
        for n in self.names:
            cursor += params[n].size
            self.bounds.append(cursor)

    def locate(self, idx: int) -> tuple[str, tuple]:
        prev = 0
        for name, bound in zip(self.names, self.bounds):
            if idx < bound:
                local = idx - prev
                return name, tuple(int(d) for d in np.unravel_index(local, self.shapes[name]))
            prev = bound
        raise IndexError(idx)


# --------------------------------------------------------------------------- #
# Structured comparison
# --------------------------------------------------------------------------- #
def compare_states(
    actual: dict[str, np.ndarray],
    expected: dict[str, np.ndarray],
    *,
    request_id: str,
    stage: str,
    tol: float,
    uncertainties: list[str] | None = None,
) -> VerificationReport:
    failures: list[str] = []
    if set(actual) != set(expected):
        missing = sorted(set(expected) - set(actual))
        extra = sorted(set(actual) - set(expected))
        if missing:
            failures.append(f"missing parameters: {missing}")
        if extra:
            failures.append(f"unexpected parameters: {extra}")
    checks: list[ParamCheck] = []
    for name in sorted(set(actual) & set(expected)):
        a, e = np.asarray(actual[name]), np.asarray(expected[name])
        if a.shape != e.shape:
            failures.append(f"{name}: shape {a.shape} vs expected {e.shape}")
            continue
        diff = np.abs(a - e)
        max_abs = float(diff.max(initial=0.0))
        checks.append(ParamCheck(
            name=name,
            max_abs_diff=max_abs,
            rms_diff=float(np.sqrt(np.mean(diff**2))),
            passed=max_abs <= tol,
        ))
        if max_abs > tol:
            failures.append(
                f"{name}: max abs diff {max_abs:.3e} exceeds tol {tol:.0e}"
            )
    passed = not failures
    return VerificationReport(
        request_id=request_id, stage=stage, passed=passed, tol=tol,
        checks=tuple(checks), failures=tuple(failures),
        uncertainties=tuple(uncertainties or []),
    )
