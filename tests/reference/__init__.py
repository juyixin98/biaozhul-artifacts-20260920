"""Independent reference oracles for cross-checking the 2SLS kernel.

The answers here are deliberately computed through routes that share NO code
with ``twosls.kernel``:

1. ``iv_2sls_dense``  textbook projection formula with explicit n x n P_Q and
   LAPACK ``inv/solve`` (the kernel instead uses eigendecomposed symmetric
   pseudo-inverses and never forms P_Q).
2. ``iv_gmm_optimize`` efficient GMM objective minimized numerically by
   SciPy (finite-difference BFGS) -- an optimizer, not a closed-form solve.
3. ``iv_ils``         indirect least squares for the just-identified case
   via reduced-form parameters.
4. ``linearmodels_oracle`` the external mature package, when installed
   (tests skip gracefully without it).
5. ``bootstrap_reference_se`` independently seeded paired bootstrap.

If the kernel agrees with all of these, agreement is not circular: each
route can fail independently of the others.
"""
from __future__ import annotations

from dataclasses import dataclass

import numpy as np
import numpy.linalg as la
from scipy.optimize import minimize


@dataclass(frozen=True)
class IVReference:
    delta: np.ndarray
    se: np.ndarray
    se_robust: np.ndarray
    sigma2: float


def _blocks(sample, endog: list[str], exog: list[str], instruments: list[str], dep: str):
    y = np.asarray(sample[dep], dtype=float)
    Y = np.column_stack([np.asarray(sample[n], dtype=float) for n in endog])
    X = np.column_stack([np.asarray(sample[n], dtype=float) for n in exog])
    Z = np.column_stack([np.asarray(sample[n], dtype=float) for n in instruments])
    return y, Y, X, Z


def iv_2sls_dense(sample, spec) -> IVReference:
    """Route 1: explicit projection matrices + LAPACK inverses."""
    y, Y, X, Z = _blocks(sample, spec.endogenous, spec.included_exogenous,
                         spec.excluded_instruments, spec.dependent)
    n = y.shape[0]
    Q = np.column_stack([X, Z])
    W = np.column_stack([Y, X])
    P = Q @ la.solve(Q.T @ Q, Q.T)
    A = W.T @ P @ W
    delta = la.solve(A, W.T @ P @ y)
    e = y - W @ delta
    r = W.shape[1]
    sigma2 = float(e @ e / (n - r))
    A_inv = la.inv(A)
    se = np.sqrt(np.diag(sigma2 * A_inv))
    meat = (P @ W).T @ np.diag(e ** 2) @ (P @ W)
    vcov_rob = (n / (n - r)) * A_inv @ meat @ A_inv
    return IVReference(delta=delta, se=se, se_robust=np.sqrt(np.diag(vcov_rob)), sigma2=sigma2)


def iv_gmm_optimize(sample, spec) -> np.ndarray:
    """Route 2: GMM with weight (Q'Q/n)^-1, minimized numerically.

    The first-order condition reproduces 2SLS exactly; reaching the same
    point through a finite-difference optimizer guards against algebra and
    sign errors in the closed-form kernel.
    """
    y, Y, X, Z = _blocks(sample, spec.endogenous, spec.included_exogenous,
                         spec.excluded_instruments, spec.dependent)
    n = y.shape[0]
    Q = np.column_stack([X, Z])
    W = np.column_stack([Y, X])
    weight = la.inv(Q.T @ Q / n)

    ols_start = la.lstsq(W, y, rcond=None)[0]
    WtQ = W.T @ Q / n

    def objective(delta: np.ndarray) -> float:
        e = y - W @ delta
        g = Q.T @ e / n
        return float(g @ weight @ g)

    def jacobian(delta: np.ndarray) -> np.ndarray:
        e = y - W @ delta
        g = Q.T @ e / n
        return -2.0 * (WtQ @ weight @ g)

    res = minimize(
        objective, ols_start, jac=jacobian, method="BFGS",
        options={"gtol": 1e-9, "maxiter": 2000},
    )
    # BFGS can report "precision loss" while still sitting at the stationary
    # point; accept based on the gradient norm itself rather than the flag.
    grad_at = jacobian(res.x)
    scale = np.linalg.norm(weight) * (np.linalg.norm(Q.T @ y / n)) + 1.0
    if np.linalg.norm(grad_at) / scale > 1e-7:
        raise RuntimeError(f"GMM oracle stationary point not found: {res.message}")
    return res.x


def iv_ils(sample, spec) -> np.ndarray:
    """Route 3: indirect least squares, valid when L == k (just identified).

    Partialling X:  yt = Yt beta + et, instrument Zt; with L = k,
        beta = (Zt'Yt)^{-1} Zt'yt
    then gamma from the residual structural regression on X.
    """
    y, Y, X, Z = _blocks(sample, spec.endogenous, spec.included_exogenous,
                         spec.excluded_instruments, spec.dependent)
    k, L = Y.shape[1], Z.shape[1]
    if L != k:
        raise ValueError("ILS closed form here assumes exactly-identified L == k")

    def partial(b: np.ndarray) -> np.ndarray:
        return b - X @ la.solve(X.T @ X, X.T @ b)

    yt, Yt, Zt = partial(y), partial(Y), partial(Z)
    beta = la.solve(Zt.T @ Yt, Zt.T @ yt)
    gamma = la.solve(X.T @ X, X.T @ (y - Y @ beta))
    return np.concatenate([beta, gamma])


def sargan_reference(sample, spec, delta: np.ndarray) -> tuple[float, int]:
    """n * R2-style Sargan score computed independently of validity.py."""
    y, Y, X, Z = _blocks(sample, spec.endogenous, spec.included_exogenous,
                         spec.excluded_instruments, spec.dependent)
    n = y.shape[0]
    W = np.column_stack([Y, X])
    Q = np.column_stack([X, Z])
    e = y - W @ delta
    explained = e @ Q @ la.solve(Q.T @ Q, Q.T @ e)
    stat = n * explained / (e @ e)
    return float(stat), Z.shape[1] - Y.shape[1]


def bootstrap_reference_se(sample, spec, reps: int = 500, seed: int = 0xBADC0DE) -> np.ndarray:
    """Independent paired-bootstrap SE (own RNG, own dense fit)."""
    y, Y, X, Z = _blocks(sample, spec.endogenous, spec.included_exogenous,
                         spec.excluded_instruments, spec.dependent)
    n = y.shape[0]
    Q = np.column_stack([X, Z])
    W = np.column_stack([Y, X])
    rng = np.random.default_rng(seed)
    draws = []
    for _ in range(reps):
        idx = rng.integers(0, n, size=n)
        try:
            Qb, Wb, yb = Q[idx], W[idx], y[idx]
            P = Qb @ la.solve(Qb.T @ Qb, Qb.T)
            d = la.solve(Wb.T @ P @ Wb, Wb.T @ P @ yb)
            if np.all(np.isfinite(d)):
                draws.append(d)
        except la.LinAlgError:
            continue
    if len(draws) < reps // 2:
        raise RuntimeError(f"reference bootstrap only kept {len(draws)}/{reps}")
    return np.std(np.array(draws), axis=0, ddof=1)


def linearmodels_oracle(sample, spec, covariance: str = "unadjusted"):
    """Route 4: mature external package. Raises ImportError if not installed."""
    from linearmodels.iv import IV2SLS  # type: ignore

    y, Y, X, Z = _blocks(sample, spec.endogenous, spec.included_exogenous,
                         spec.excluded_instruments, spec.dependent)
    cov_type = "unadjusted" if covariance == "homoskedastic" else "robust"
    res = IV2SLS(y, X, Y, Z).fit(cov_type=cov_type)
    return res
