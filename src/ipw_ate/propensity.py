"""Propensity model and *declared* cross-fitting.

The model is a plain L2-regularized logistic regression fit with SciPy (no
ML framework), so every step is deterministic and independently checkable.

Cross-fitting is explicit: with ``K = n_splits`` folds, unit ``i`` receives a
score from a model trained on the other ``K-1`` folds. Fold assignment is a
seeded permutation split, recorded on the result so a run can be re-audited.

Boundary handling
~~~~~~~~~~~~~~~~~
A held-out score that is exactly 0/1 (within ``config.max_score``) or
non-finite raises :class:`PropensityScoreError` -- never an epsilon
substitution. A training fold containing a single treatment arm cannot fit an
intercept model and raises :class:`ModelSeparationError`.
"""

from __future__ import annotations

import numpy as np
from scipy.optimize import linprog, minimize

from .contract import IPWConfig
from .errors import ModelSeparationError, PropensityScoreError

# Fixed regularization strength (L2 penalty on standardized coefficients,
# intercept unpenalized). Declared so reruns are identical.
L2_LAMBDA: float = 1.0


def _standardize_fit(X: np.ndarray) -> tuple[np.ndarray, np.ndarray]:
    mean = X.mean(axis=0)
    sd = X.std(axis=0, ddof=0)
    sd = np.where(sd < 1e-12, 1.0, sd)  # constant column -> no scaling
    return mean, sd


def _standardize_apply(X: np.ndarray, mean: np.ndarray, sd: np.ndarray) -> np.ndarray:
    return (X - mean) / sd


def _sigmoid(z: np.ndarray) -> np.ndarray:
    # Stable elementwise logistic.
    out = np.empty_like(z, dtype=float)
    pos = z >= 0
    out[pos] = 1.0 / (1.0 + np.exp(-z[pos]))
    ez = np.exp(z[~pos])
    out[~pos] = ez / (1.0 + ez)
    return out


def fit_logistic(X: np.ndarray, t: np.ndarray) -> np.ndarray:
    """Fit L2-regularized logistic regression; return [intercept, beta...].

    Covariates are standardized externally (or already standardized). The
    intercept is unpenalized; coefficient penalty is ``L2_LAMBDA/2 ||beta||^2``.
    """
    n, p = X.shape
    Xd = np.column_stack([np.ones(n), X])

    def nll(theta: np.ndarray) -> float:
        z = Xd @ theta
        # log(1+exp(z)) stable
        reg = 0.5 * L2_LAMBDA * np.sum(theta[1:] ** 2)
        return float(np.sum(np.logaddexp(0.0, z) - t * z) + reg)

    def grad(theta: np.ndarray) -> np.ndarray:
        p = _sigmoid(Xd @ theta)
        g = Xd.T @ (p - t)
        g[1:] += L2_LAMBDA * theta[1:]
        return g

    opt = minimize(
        nll,
        np.zeros(p + 1),
        jac=grad,
        method="L-BFGS-B",
        options={"maxiter": 1000, "ftol": 1e-12, "gtol": 1e-8},
    )
    if not opt.success:
        # A failed fit is a declared failure, not a silent fallback estimate.
        raise PropensityScoreError(
            f"logistic fit did not converge: {opt.message!r}"
        )
    return opt.x


def predict_proba(theta: np.ndarray, X: np.ndarray) -> np.ndarray:
    Xd = np.column_stack([np.ones(X.shape[0]), X])
    return _sigmoid(Xd @ theta)


def is_linearly_separable(X: np.ndarray, t: np.ndarray) -> bool:
    """True if the two classes are strictly linearly separable.

    Solves a feasibility LP for a margin-1 separator (w, b):
        treated:  w.z + b >= 1
        control:  w.z + b <= -1
    Separability means the logistic MLE diverges (scores -> 0/1), so IPW
    weights are undefined. Detecting it structurally is independent of the
    logistic solver and its finite regularization, giving a definitive
    "separation" verdict rather than an arbitrary near-boundary score.
    """
    n, p = X.shape
    s = np.where(t == 1, 1.0, -1.0)
    # Variables [w (p), b]; inequalities encoded as -s*(w.z+b) <= -1.
    A_ub = np.zeros((n, p + 1))
    A_ub[:, :p] = -s[:, None] * X
    A_ub[:, p] = -s
    res = linprog(
        c=np.zeros(p + 1),
        A_ub=A_ub,
        b_ub=-np.ones(n),
        bounds=[(None, None)] * (p + 1),
        method="highs",
    )
    return bool(res.success)


def make_folds(n: int, n_splits: int, seed: int) -> np.ndarray:
    """Return fold id per unit via a seeded permutation (contiguous blocks)."""
    rng = np.random.default_rng(seed)
    perm = rng.permutation(n)
    fold_ids = np.empty(n, dtype=int)
    # np.array_split tolerates n not divisible by n_splits.
    for fold_id, idx in enumerate(np.array_split(perm, n_splits)):
        fold_ids[idx] = fold_id
    return fold_ids


def cross_fit_propensity(
    treatment: np.ndarray,
    covariates: np.ndarray,
    config: IPWConfig,
    fold_ids: np.ndarray | None = None,
) -> tuple[np.ndarray, np.ndarray, tuple[tuple[int, ...], ...]]:
    """Out-of-fold propensities.

    Returns ``(scores, fold_ids, fold_membership)``. Each score is produced by
    a model trained without that unit. ``fold_ids`` may be supplied
    deterministically (used by tests to force a specific fold structure);
    otherwise a seeded permutation split is used.
    """
    n = treatment.shape[0]
    if fold_ids is None:
        fold_ids = make_folds(n, config.n_splits, config.random_seed)
    else:
        fold_ids = np.asarray(fold_ids, dtype=int)
        if fold_ids.shape != (n,):
            raise ValueError("fold_ids must be shape (n,)")
        if set(np.unique(fold_ids).tolist()) != set(range(config.n_splits)):
            raise ValueError("fold_ids must contain every fold 0..n_splits-1")
    scores = np.full(n, np.nan, dtype=float)
    folds: list[tuple[int, ...]] = []

    for held in range(config.n_splits):
        train_idx = np.where(fold_ids != held)[0]
        hold_idx = np.where(fold_ids == held)[0]
        folds.append(tuple(int(i) for i in hold_idx))

        t_train = treatment[train_idx]
        # A training fold with a single arm cannot identify the intercept.
        if t_train.min() == t_train.max():
            raise ModelSeparationError(
                f"training fold {held} contains a single treatment arm "
                f"(n={len(train_idx)}); propensity model is separated"
            )

        mean, sd = _standardize_fit(covariates[train_idx])
        Xtr = _standardize_apply(covariates[train_idx], mean, sd)
        Xho = _standardize_apply(covariates[hold_idx], mean, sd)

        # Structural separation check (feasibility LP), independent of the
        # logistic solver: a separable design makes the MLE diverge to 0/1.
        if is_linearly_separable(Xtr, t_train):
            raise ModelSeparationError(
                f"training fold {held} is linearly (quasi-)separated; "
                "propensity MLE does not exist and IPW weights are undefined"
            )

        theta = fit_logistic(Xtr, t_train)
        scores[hold_idx] = predict_proba(theta, Xho)

    _assert_finite_interior(scores, config)
    return scores, fold_ids, tuple(folds)


def _assert_finite_interior(scores: np.ndarray, config: IPWConfig) -> None:
    if not np.all(np.isfinite(scores)):
        bad = int(np.sum(~np.isfinite(scores)))
        raise PropensityScoreError(f"{bad} non-finite propensity scores")
    at_zero = np.sum(scores <= config.max_score)
    at_one = np.sum(scores >= 1.0 - config.max_score)
    if at_zero or at_one:
        raise PropensityScoreError(
            f"{int(at_zero)} score(s) at 0 and {int(at_one)} at 1 "
            "(separation/degenerate model); weights are undefined. "
            "No denominator replacement is performed."
        )
