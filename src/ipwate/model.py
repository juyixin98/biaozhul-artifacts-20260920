"""Propensity score model: ridge logistic regression via auditable IRLS.

Implemented directly with NumPy (no opaque estimator) so the fitting process
is fully reviewable:

* intercept is unpenalized; ridge penalty applies to standardized covariates;
* covariates are standardized using TRAIN statistics only;
* convergence is declared on the max absolute score gradient;
* predicted scores are the exact sigmoid values — we never clip a predicted
  score to make a denominator finite. Near-zero/one scores are surfaced to the
  positivity check, which rejects by contract.
"""

from __future__ import annotations

from dataclasses import dataclass

import numpy as np

from .errors import ModelConvergenceError, ValidationError

_SIGMOID_CLIP_ARG = 60.0  # only bounds the EXPONENT argument against under/overflow;
# the returned probability is still expit(x) and can be effectively 0 or 1.


def _sigmoid(z: np.ndarray) -> np.ndarray:
    z = np.clip(z, -_SIGMOID_CLIP_ARG, _SIGMOID_CLIP_ARG)
    return 1.0 / (1.0 + np.exp(-z))


@dataclass(frozen=True)
class Standardization:
    mean: np.ndarray
    scale: np.ndarray

    def transform(self, x: np.ndarray) -> np.ndarray:
        return (x - self.mean) / self.scale


def fit_standardization(x_train: np.ndarray) -> Standardization:
    mean = x_train.mean(axis=0)
    scale = x_train.std(axis=0, ddof=0)
    scale = np.where(scale < 1e-12, 1.0, scale)
    return Standardization(mean=mean, scale=scale)


@dataclass(frozen=True)
class LogisticRidge:
    beta: np.ndarray  # [intercept, standardized slopes]
    standardization: Standardization
    converged: bool
    n_iter: int

    def predict_proba(self, x: np.ndarray) -> np.ndarray:
        xs = self.standardization.transform(x)
        design = np.column_stack([np.ones(len(xs)), xs])
        return _sigmoid(design @ self.beta)


def fit_logistic_ridge(
    x_train: np.ndarray,
    a_train: np.ndarray,
    *,
    ridge_lambda: float,
    max_iter: int,
    tol: float,
) -> LogisticRidge:
    """Fit ridge logistic regression by iteratively reweighted least squares."""
    if ridge_lambda < 0:
        raise ValidationError("ridge_lambda must be non-negative")
    if not np.any(a_train == 1) or not np.any(a_train == 0):
        raise ValidationError(
            "Training fold must contain both treatment classes",
            details={
                "n_treated": int(a_train.sum()),
                "n_untreated": int(len(a_train) - a_train.sum()),
            },
        )

    standardization = fit_standardization(x_train)
    xs = standardization.transform(x_train)
    design = np.column_stack([np.ones(len(xs)), xs])
    n, d = design.shape
    beta = np.zeros(d)
    # Penalty matrix: no penalty on the intercept.
    penalty = np.eye(d) * ridge_lambda
    penalty[0, 0] = 0.0

    converged = False
    n_iter = 0
    for n_iter in range(1, max_iter + 1):
        eta = design @ beta
        p = _sigmoid(eta)
        residual = p - a_train
        grad = design.T @ residual / n + penalty @ beta
        w = np.maximum(p * (1.0 - p), 1e-14)
        hessian = (design.T * w) @ design / n + penalty
        try:
            delta = np.linalg.solve(hessian, grad)
        except np.linalg.LinAlgError as exc:
            raise ModelConvergenceError(
                "Singular Hessian in logistic IRLS",
                details={"fold_size": n, "n_covariates": d - 1},
            ) from exc
        beta = beta - delta
        if np.max(np.abs(grad)) < tol and np.max(np.abs(delta)) < tol:
            converged = True
            break

    if not converged:
        # Ridge makes the objective strictly convex for lambda>0; failure here
        # signals near-separation or a mis-specified iteration budget.
        raise ModelConvergenceError(
            "Logistic ridge did not converge within the declared iteration budget",
            details={"max_iter": max_iter, "final_grad_max": float(np.max(np.abs(grad)))},
        )

    return LogisticRidge(
        beta=beta, standardization=standardization, converged=converged, n_iter=n_iter
    )
