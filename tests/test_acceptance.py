"""Acceptance tests mirroring the task's verification checklist.

Run directly: ``python tests/test_acceptance.py`` (prints a PASS/FAIL report)
or under pytest. Covers:
1. Huber vs OLS on synthetic data with outliers — robustness comparison.
2. Collinear columns — rank deficiency reported explicitly, not silently fit;
   ridge penalty on slopes as an explicit recovery path.
3. Exactly zero residuals — no division-by-zero / NaN.
4. Monotone objective descent and explicit convergence status.
5. Reproducibility of results (library and JSON layer).
"""

import json
import os
import sys

import numpy as np

# Allow running directly as `python tests/test_acceptance.py`.
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from robustreg import (  # noqa: E402
    RankDeficientError,
    handle_request,
    huber_irls,
    ols_fit,
)

CHECKS = []


def check(name):
    def deco(fn):
        CHECKS.append((name, fn))
        return fn
    return deco


def _synthetic_with_outliers(seed=20260923):
    rng = np.random.default_rng(seed)
    n = 120
    X = rng.normal(size=(n, 4))
    true = np.array([2.5, -1.0, 0.5, 3.0])
    intercept_true = -0.75
    y = intercept_true + X @ true + rng.normal(scale=0.4, size=n)
    # ~14% gross asymmetric outliers.
    n_out = n // 7
    idx = rng.choice(n, size=n_out, replace=False)
    y[idx] += rng.choice([-1.0, 1.0], size=n_out) * rng.uniform(10, 20,
                                                                size=n_out)
    return X, y, true, intercept_true, idx


@check("1. Huber beats OLS vs the clean truth under outliers")
def _t1():
    X, y, true, b0, _ = _synthetic_with_outliers()
    h = huber_irls(X, y)
    o = ols_fit(X, y)
    err_h = np.abs(np.r_[h.coef, h.intercept] - np.r_[true, b0])
    err_o = np.abs(np.r_[o.coef, o.intercept] - np.r_[true, b0])
    assert h.converged, "Huber did not converge"
    assert np.max(err_h) < np.max(err_o), (
        f"max error Huber {np.max(err_h):.3f} not below OLS {np.max(err_o):.3f}"
    )
    assert np.mean(err_h) < np.mean(err_o)
    assert h.objective <= o.huber_objective, (
        "Huber objective not <= OLS-at-Huber objective"
    )
    return (f"max|err| Huber={np.max(err_h):.4f} vs OLS={np.max(err_o):.4f}; "
            f"Huber obj={h.objective:.4f} <= OLS-at-Huber obj="
            f"{o.huber_objective:.4f}")


@check("2a. Exact collinear columns raise an explicit rank error")
def _t2a():
    X, y, *_ = _synthetic_with_outliers()
    Xc = np.column_stack([X[:, 0], X[:, 1], X[:, 0] + X[:, 1]])
    try:
        huber_irls(Xc, y)
    except RankDeficientError as e:
        assert e.nullspace_dim == 1
        return f"rank={e.rank}/{e.p}, nullspace_dim={e.nullspace_dim}"
    raise AssertionError("rank deficiency was not detected")


@check("2b. JSON envelope reports rank_deficient with details")
def _t2b():
    X, y, *_ = _synthetic_with_outliers()
    Xc = np.column_stack([X[:, 0], X[:, 0]])
    resp = handle_request({"X": Xc.tolist(), "y": y.tolist()})
    assert resp["ok"] is False
    err = resp["error"]
    assert err["code"] == "rank_deficient"
    assert err["nullspace_dim"] == 1
    assert "hint" in err
    json.dumps(resp, allow_nan=False)  # error envelope is clean JSON
    return f"error code={err['code']}, rank={err['rank']}/{err['p']}"


@check("2c. Ridge penalty on slopes resolves collinearity")
def _t2c():
    rng = np.random.default_rng(77)
    x = rng.normal(size=100)
    Xc = np.column_stack([x, x])  # duplicate slope columns
    y = 2.0 * x + rng.normal(scale=0.1, size=100)  # no intercept in truth
    h = huber_irls(Xc, y, fit_intercept=True, lam=1e-3)
    assert h.rank == 3, f"rank {h.rank} != 3"
    assert not h.rank_deficient
    # Symmetric duplicate columns split the load, intercept stays ~0.
    assert abs(h.intercept) < 0.05
    assert abs((h.coef[0] + h.coef[1]) - 2.0) < 0.1
    return f"rank full ({h.rank}); split coefs sum={h.coef[0]+h.coef[1]:.4f}"


@check("3. Exactly zero residuals: no NaN, weights=1, objective=0")
def _t3():
    # Full-rank design (with intercept) whose response lies exactly in its
    # column space: residuals are at machine precision.
    X = np.array([
        [0.0, 0.0],
        [1.0, 0.0],
        [0.0, 1.0],
        [1.0, 1.0],
        [2.0, 1.0],
    ])
    y = 1.0 + 2.0 * X[:, 0] - 3.0 * X[:, 1]
    h = huber_irls(X, y)
    assert np.all(np.isfinite(h.weights))
    assert np.all(np.isfinite(h.residual))
    np.testing.assert_allclose(h.residual, 0.0, atol=1e-10)
    np.testing.assert_allclose(h.weights, 1.0)
    assert h.objective < 1e-20
    assert h.converged
    return (f"max|residual|={np.max(np.abs(h.residual)):.2e}, "
            f"objective={h.objective:.2e}, iters={h.iterations}")


@check("4. Objective descends monotonically and status is explicit")
def _t4():
    X, y, *_ = _synthetic_with_outliers()
    h = huber_irls(X, y, tol=1e-12, max_iter=200)
    hist = np.asarray(h.objective_history)
    diffs = np.diff(hist)
    assert np.all(diffs <= 1e-10 * max(1.0, abs(hist[0]))), (
        f"non-monotone objective: max rise {diffs.max()}"
    )
    assert hist[-1] <= hist[0]
    assert h.converged
    return (f"iterations={h.iterations}, {hist[0]:.4f} -> {hist[-1]:.4f}, "
            f"monotone={h.objective_decreased}")


@check("5. Reproducibility: identical request gives identical answer")
def _t5():
    X, y, *_ = _synthetic_with_outliers()
    r1 = huber_irls(X, y, delta=1.345, tol=1e-10, max_iter=100)
    r2 = huber_irls(X, y, delta=1.345, tol=1e-10, max_iter=100)
    assert np.array_equal(r1.coef, r2.coef)
    assert r1.objective == r2.objective
    assert r1.iterations == r2.iterations
    payload = {"X": X.tolist(), "y": y.tolist(), "tol": 1e-10,
               "max_iter": 100}
    assert handle_request(payload) == handle_request(payload)
    return "library and JSON outputs identical across runs"


def _run_all():
    passed = 0
    lines = []
    for name, fn in CHECKS:
        try:
            detail = fn()
            passed += 1
            lines.append(f"PASS  {name}" + (f"  [{detail}]" if detail else ""))
        except Exception as exc:  # noqa: BLE001 - report any failure
            lines.append(f"FAIL  {name}  -> {type(exc).__name__}: {exc}")
    lines.append("")
    lines.append(f"{passed}/{len(CHECKS)} acceptance checks passed")
    return passed == len(CHECKS), "\n".join(lines)


# Under pytest each check is exposed as a plain test function.
def test_acceptance_checklist():
    for name, fn in CHECKS:
        fn()  # raises on failure; pytest reports which check


if __name__ == "__main__":
    ok, report = _run_all()
    print(report)
    sys.exit(0 if ok else 1)
