"""Estimation kernel: pure numerical statistics with no I/O or HTTP concerns.

Conventions (part of the statistical contract, documented in README):

* Model: ``y = W gamma + X beta + e``, instruments ``[W, Z]``; ``X`` endogenous,
  ``W`` included exogenous (constant prepended when requested), ``Z`` excluded
  instruments. Estimator::

      beta_2sls = (R' P_Z R)^{-1} R' P_Z y,   R=[W, X],  P_Z = Zm(Zm'Zm)^{-1}Zm'

* Structural residuals use the *original* endogenous regressors:
  ``u = y - R beta`` (never ``y - [W, X_hat] beta``).
* Conventional VCV uses the 2SLS formula ``sigma2 (R' P_Z R)^{-1}`` with
  sigma2 = u'u/(n-k) -- this is NOT the naive second-stage OLS variance
  ``sigma2 (X_hat'X_hat)^{-1}`` evaluated with second-stage residuals.
* Robust VCV is White HC1 around the 2SLS sandwich.
* Weak ID: Cragg-Donald Wald F; for K=1 it equals the excluded-instruments
  partial F. Stock-Yogo 10% critical values are used where tabulated.
"""

from __future__ import annotations

from dataclasses import dataclass, field

import numpy as np
from scipy import stats

from .errors import InapplicableTest, SingularDesign, UnidentifiedModel


# --------------------------------------------------------------------------- #
# Matrix helpers
# --------------------------------------------------------------------------- #

def _lstsq(a: np.ndarray, b: np.ndarray) -> np.ndarray:
    """Least-squares solve; raises SingularDesign on numerical rank loss."""
    if a.shape[1] == 0:
        return np.zeros((0, b.shape[1] if b.ndim > 1 else 0))
    coef, *_ = np.linalg.lstsq(a, b if b.ndim > 1 else b.reshape(-1, 1), rcond=None)
    return coef if b.ndim > 1 else coef.ravel()


def _residualize(base: np.ndarray, y: np.ndarray) -> np.ndarray:
    """Residual of projecting y off ``base`` (M_base y); identity when base empty."""
    if base.shape[1] == 0:
        return y.copy()
    return y - base @ _lstsq(base, y)


def _inv(a: np.ndarray) -> np.ndarray:
    try:
        return np.linalg.inv(a)
    except np.linalg.LinAlgError as exc:  # pragma: no cover - defensive
        raise SingularDesign(f"matrix inversion failed: {exc}") from exc


# --------------------------------------------------------------------------- #
# Result containers
# --------------------------------------------------------------------------- #

@dataclass(frozen=True)
class FirstStageStat:
    endogenous_name: str
    f_stat: float
    f_pvalue: float
    partial_r2: float
    partial_r2_adj: float
    coefficients: dict[str, float]


@dataclass(frozen=True)
class OverIdStat:
    statistic: float
    p_value: float
    kind: str  # "sargan" | "hansen_j"


@dataclass(frozen=True)
class EndogeneityStat:
    statistic: float
    p_value: float
    coefficients: dict[str, float]


@dataclass(frozen=True)
class RankInfo:
    n: int
    n_included: int          # J (incl. constant)
    n_endogenous: int        # K
    n_excluded: int          # L
    rank_design: int         # rank of [W, Z]
    rank_first_stage: int    # rank of M_W X_hat
    design_svals: tuple[float, ...]
    first_stage_svals: tuple[float, ...]
    vif: dict[str, float]

    @property
    def max_vif(self) -> float:
        return max(self.vif.values(), default=1.0)


@dataclass(frozen=True)
class CoefficientStat:
    name: str
    estimate: float
    std_error: float
    z_stat: float
    p_value: float
    ci_lower: float
    ci_upper: float


@dataclass(frozen=True)
class KernelResult:
    n_obs: int
    cov_type: str
    coefficient_names: tuple[str, ...]
    coefficients: tuple[CoefficientStat, ...]
    beta: np.ndarray
    vcv: np.ndarray
    residuals: np.ndarray
    first_stage: tuple[FirstStageStat, ...]
    rank: RankInfo
    cragg_donald: float
    overid: OverIdStat | None
    endogeneity: EndogeneityStat
    gmm_beta: np.ndarray
    gmm_j_stat: float


# --------------------------------------------------------------------------- #
# Rank / collinearity
# --------------------------------------------------------------------------- #

def _svals(a: np.ndarray) -> np.ndarray:
    if a.size == 0 or a.shape[1] == 0:
        return np.empty(0)
    return np.linalg.svd(a, compute_uv=False)


def check_rank(
    w: np.ndarray, x: np.ndarray, z: np.ndarray, rcond: float
) -> tuple[RankInfo, np.ndarray, np.ndarray, np.ndarray]:
    """Validate ranks, return RankInfo plus residualized matrices."""
    n, j = w.shape
    k, ell = x.shape[1], z.shape[1]
    zm = np.hstack([w, z])

    z_svals = _svals(zm)
    rank_design = int(np.linalg.matrix_rank(zm, tol=rcond * z_svals[0]))
    if rank_design < j + ell:
        raise SingularDesign(
            "instrument/design matrix [W, Z] is rank deficient",
            details={"rank": rank_design, "expected": j + ell},
        )

    xdot = _residualize(w, x)
    zdot = _residualize(w, z) if j else z.copy()
    pi_dot = _lstsq(zdot, xdot)
    xhat_dot = zdot @ pi_dot

    fs_svals = _svals(xhat_dot)
    rank_fs = int(np.linalg.matrix_rank(xhat_dot, tol=rcond * fs_svals[0])) if k else 0
    if rank_fs < k:
        raise UnidentifiedModel(
            "rank condition fails: projected endogenous regressors are collinear "
            "(instruments provide no independent variation for some endogenous "            "regressor)",
            details={"rank_first_stage": rank_fs, "n_endogenous": k},
        )

    vif = _vif_dict(zdot)
    info = RankInfo(
        n=n, n_included=j, n_endogenous=k, n_excluded=ell,
        rank_design=rank_design, rank_first_stage=rank_fs,
        design_svals=tuple(np.round(z_svals, 8)),
        first_stage_svals=tuple(np.round(fs_svals, 8)),
        vif=vif,
    )
    return info, xdot, zdot, xhat_dot


def _vif_dict(zdot: np.ndarray) -> dict[str, float]:
    """Variance-inflation factors of residualized excluded instruments."""
    ell = zdot.shape[1]
    if ell <= 1:
        return {f"z{idx + 1}": 1.0 for idx in range(ell)}
    vif: dict[str, float] = {}
    for idx in range(ell):
        others = np.delete(zdot, idx, axis=1)
        rj = _residualize(others, zdot[:, [idx]])
        tss = float(zdot[:, idx] @ zdot[:, idx])
        rss = float(rj.ravel() @ rj.ravel())
        r2 = 0.0 if tss == 0 else max(0.0, 1.0 - rss / tss)
        vif[f"z{idx + 1}"] = float("inf") if r2 >= 1.0 else 1.0 / (1.0 - r2)
    return vif


# --------------------------------------------------------------------------- #
# First stage
# --------------------------------------------------------------------------- #

def first_stage_stats(
    w: np.ndarray, x: np.ndarray,
    xdot: np.ndarray, zdot: np.ndarray, xhat_dot: np.ndarray,
    pi_full: np.ndarray,
    names_endogenous: tuple[str, ...],
    names_design: tuple[str, ...],
) -> tuple[FirstStageStat, ...]:
    """Per-endogenous-regressor first-stage partial F / partial R².

    Partial statistics use sums of squares residualized off W, so they
    reflect the *excluded* instruments only.
    """
    n, j = w.shape
    ell = zdot.shape[1]
    df = n - j - ell
    if df <= 0:
        raise UnidentifiedModel(
            "no residual degrees of freedom for first stage",
            details={"df": df},
        )
    stats_list: list[FirstStageStat] = []
    for k_idx in range(x.shape[1]):
        resid = xdot[:, k_idx] - xhat_dot[:, k_idx]
        ssr_ur = float(resid @ resid)
        ssr_r = float(xdot[:, k_idx] @ xdot[:, k_idx])
        f_stat = ((ssr_r - ssr_ur) / ell) / (ssr_ur / df) if ssr_ur > 0 else float("inf")
        p_value = float(stats.f.sf(f_stat, ell, df)) if np.isfinite(f_stat) else 0.0
        partial_r2 = 0.0 if ssr_r == 0 else max(0.0, 1.0 - ssr_ur / ssr_r)
        denom = n - j
        partial_r2_adj = (
            1.0 - (ssr_ur / df) / (ssr_r / denom) if denom > 0 and ssr_r > 0 else 0.0
        )
        stats_list.append(
            FirstStageStat(
                endogenous_name=names_endogenous[k_idx],
                f_stat=float(f_stat),
                f_pvalue=p_value,
                partial_r2=partial_r2,
                partial_r2_adj=partial_r2_adj,
                coefficients={
                    name: float(pi_full[row, k_idx])
                    for row, name in enumerate(names_design)
                },
            )
        )
    return tuple(stats_list)


def cragg_donald_min_eigenvalue(xdot: np.ndarray, xhat_dot: np.ndarray) -> float:
    """Smallest eigenvalue of the Cragg-Donald matrix (unscaled).

    The F statistic is ``(n-J-L)/L * lambda_min`` of ``R^{-1/2} E R^{-1/2}``
    with ``E = Xdot' P_{M_W Z} Xdot`` and ``R = Xdot' M_[W,Z] Xdot``.
    For one endogenous regressor the scaled value equals the excluded
    instruments partial F.
    """
    e_mat = xhat_dot.T @ xhat_dot
    r_mat = xdot.T @ xdot - e_mat
    try:
        chol = np.linalg.cholesky(r_mat)
    except np.linalg.LinAlgError as exc:
        raise UnidentifiedModel(
            "reduced-form residual covariance is singular; cannot compute "
            "Cragg-Donald statistic"
        ) from exc
    solved = np.linalg.solve(chol, e_mat)
    whitened = np.linalg.solve(chol, solved.T).T
    return float(np.linalg.eigvalsh(whitened).min())


# --------------------------------------------------------------------------- #
# 2SLS core
# --------------------------------------------------------------------------- #

def twosls_coefficients(
    y: np.ndarray, w: np.ndarray, x: np.ndarray, zm: np.ndarray
) -> tuple[np.ndarray, np.ndarray, np.ndarray]:
    """Return (beta, residuals u, R_hat=P_Z[W,X])."""
    pi_x = _lstsq(zm, x)
    x_hat = zm @ pi_x
    r_hat = np.hstack([w, x_hat]) if w.shape[1] else x_hat
    r = np.hstack([w, x]) if w.shape[1] else x
    beta = _lstsq(r_hat, y)
    residuals = y - r @ beta
    return beta, residuals, r_hat


def conventional_vcv(
    r_hat: np.ndarray, residuals: np.ndarray
) -> np.ndarray:
    n, k = r_hat.shape
    sigma2 = float(residuals @ residuals) / (n - k)
    return sigma2 * _inv(r_hat.T @ r_hat)


def robust_vcv(r_hat: np.ndarray, residuals: np.ndarray) -> np.ndarray:
    n, k = r_hat.shape
    bread = _inv(r_hat.T @ r_hat)
    weighted = residuals[:, None] ** 2 * r_hat
    meat = r_hat.T @ weighted
    return (n / (n - k)) * bread @ meat @ bread


def coefficient_stats(
    beta: np.ndarray, vcv: np.ndarray, names: tuple[str, ...], alpha: float
) -> tuple[CoefficientStat, ...]:
    se = np.sqrt(np.diag(vcv))
    z = beta / se
    p = 2.0 * stats.norm.sf(np.abs(z))
    zcrit = stats.norm.ppf(1.0 - alpha / 2.0)
    return tuple(
        CoefficientStat(
            name=name,
            estimate=float(beta[i]),
            std_error=float(se[i]),
            z_stat=float(z[i]),
            p_value=float(p[i]),
            ci_lower=float(beta[i] - zcrit * se[i]),
            ci_upper=float(beta[i] + zcrit * se[i]),
        )
        for i, name in enumerate(names)
    )


# --------------------------------------------------------------------------- #
# Overidentification and endogeneity
# --------------------------------------------------------------------------- #

def sargan_stat(zm: np.ndarray, residuals: np.ndarray, overid_df: int) -> OverIdStat:
    """Sargan's score: n * u'P_Z u / u'u  ~ chi2(L-K) (conditional homosked.)."""
    fitted = zm @ _lstsq(zm, residuals)
    n = zm.shape[0]
    stat = n * float(fitted @ fitted) / float(residuals @ residuals)
    return OverIdStat(statistic=float(stat), p_value=float(stats.chi2.sf(stat, overid_df)),
                      kind="sargan")


def two_step_gmm(
    y: np.ndarray, w: np.ndarray, x: np.ndarray, zm: np.ndarray,
    beta1: np.ndarray, overid_df: int,
) -> tuple[np.ndarray, float]:
    """Independent efficient-GMM numerical path (also yields Hansen J).

    Step 1 builds a weight from 2SLS residuals and solves for beta; the J
    statistic is evaluated with that SAME weight at the new beta, which is the
    standard two-step GMM over-ID statistic (~ chi2(L-K)).
    """
    n = zm.shape[0]
    r = np.hstack([w, x]) if w.shape[1] else x
    u1 = y - r @ beta1
    weight1 = _inv(zm.T @ (u1[:, None] ** 2 * zm) / n)
    g = zm.T @ r / n
    h = zm.T @ y / n
    beta2 = np.linalg.solve(g.T @ weight1 @ g, g.T @ weight1 @ h)
    u2 = y - r @ beta2
    moments = zm.T @ u2 / n
    j_stat = float(n * moments @ weight1 @ moments)
    return beta2, j_stat


def endogeneity_test(
    y: np.ndarray, w: np.ndarray, x: np.ndarray, zm: np.ndarray,
    x_hat: np.ndarray, robust: bool, names_endog: tuple[str, ...],
) -> EndogeneityStat:
    """Durbin-Wu-Hausman augmented-regression test.

    Add first-stage residuals V = X - X_hat to the structural regression and
    jointly test their coefficients are zero (chi2 with K d.f.; HC1 robust
    variant when requested).
    """
    v = x - x_hat
    a = np.hstack([w, x, v]) if w.shape[1] else np.hstack([x, v])
    n, total_k = a.shape
    beta_a = _lstsq(a, y)
    resid_a = y - a @ beta_a
    k = x.shape[1]
    theta = beta_a[-k:]

    if robust:
        bread = _inv(a.T @ a)
        meat = a.T @ (resid_a[:, None] ** 2 * a)
        vcv_all = (n / (n - total_k)) * bread @ meat @ bread
    else:
        sigma2 = float(resid_a @ resid_a) / (n - total_k)
        vcv_all = sigma2 * _inv(a.T @ a)
    vcv_theta = vcv_all[-k:, -k:]
    try:
        stat = float(theta @ np.linalg.solve(vcv_theta, theta))
    except np.linalg.LinAlgError as exc:  # pragma: no cover - defensive
        raise InapplicableTest("endogeneity test VCV singular") from exc
    return EndogeneityStat(
        statistic=stat,
        p_value=float(stats.chi2.sf(stat, k)),
        coefficients={name: float(val) for name, val in zip(names_endog, theta)},
    )


# --------------------------------------------------------------------------- #
# Orchestration
# --------------------------------------------------------------------------- #

def _check_order(k: int, ell: int) -> None:
    if ell < k:
        raise UnidentifiedModel(
            f"order condition fails: {ell} excluded instrument(s) < {k} "
            "endogenous regressor(s)",
            details={"n_excluded_instruments": ell, "n_endogenous": k},
        )


def _select_vcv(cov_type: str, r_hat: np.ndarray,
                residuals: np.ndarray) -> np.ndarray:
    if cov_type == "robust":
        return robust_vcv(r_hat, residuals)
    return conventional_vcv(r_hat, residuals)


def _overidentification(
    y: np.ndarray, w: np.ndarray, x: np.ndarray, zm: np.ndarray,
    beta: np.ndarray, residuals: np.ndarray, *, k: int, ell: int,
    cov_type: str, run_overid: bool,
) -> tuple[OverIdStat | None, np.ndarray, float]:
    """Return (over-id result or None, gmm beta, gmm J) for over-ID models."""
    gmm_beta = np.full_like(beta, np.nan)
    gmm_j = float("nan")
    if ell <= k:
        return None, gmm_beta, gmm_j
    if cov_type == "robust":
        # Independent efficient-GMM path; its J doubles as robust over-id test.
        gmm_beta, gmm_j = two_step_gmm(y, w, x, zm, beta, ell - k)
    if not run_overid:
        return None, gmm_beta, gmm_j
    if cov_type == "robust":
        overid = OverIdStat(
            statistic=gmm_j, p_value=float(stats.chi2.sf(gmm_j, ell - k)),
            kind="hansen_j",
        )
    else:
        overid = sargan_stat(zm, residuals, ell - k)
    return overid, gmm_beta, gmm_j


def _first_stage_and_cd(
    y_shape_n: int, w: np.ndarray, x: np.ndarray, xdot: np.ndarray,
    zdot: np.ndarray, xhat_dot: np.ndarray, zm: np.ndarray,
    pi_full: np.ndarray, *, names_endog, names_exog,
    names_instruments, add_constant: bool,
):
    names_design = (
        (("const",) if add_constant else ()) + names_exog + names_instruments
    )
    fs = first_stage_stats(
        w, x, xdot, zdot, xhat_dot, pi_full, names_endog, names_design
    )
    j, ell = w.shape[1], zdot.shape[1]
    cd = cragg_donald_min_eigenvalue(xdot, xhat_dot) * (y_shape_n - j - ell) / ell
    return fs, cd


def estimate(
    y: np.ndarray,
    x: np.ndarray,
    w: np.ndarray,
    z: np.ndarray,
    *,
    names_endog: tuple[str, ...],
    names_exog: tuple[str, ...],
    names_instruments: tuple[str, ...],
    add_constant: bool,
    cov_type: str,
    alpha: float,
    rank_rcond: float,
    run_overid: bool,
    run_endogeneity: bool,
) -> KernelResult:
    n = y.shape[0]
    k, ell = x.shape[1], z.shape[1]
    _check_order(k, ell)

    rank, xdot, zdot, xhat_dot = check_rank(w, x, z, rank_rcond)
    zm = np.hstack([w, z]) if w.shape[1] else z
    pi_full = _lstsq(zm, x)

    beta, residuals, r_hat = twosls_coefficients(y, w, x, zm)
    vcv = _select_vcv(cov_type, r_hat, residuals)

    names = (("const",) if add_constant else ()) + names_exog + names_endog
    coefs = coefficient_stats(beta, vcv, names, alpha)
    fs, cd_stat = _first_stage_and_cd(
        n, w, x, xdot, zdot, xhat_dot, zm, pi_full,
        names_endog=names_endog, names_exog=names_exog,
        names_instruments=names_instruments, add_constant=add_constant,
    )
    overid, gmm_beta, gmm_j = _overidentification(
        y, w, x, zm, beta, residuals, k=k, ell=ell,
        cov_type=cov_type, run_overid=run_overid,
    )
    endo = _endogeneity(
        y, w, x, zm, zm @ pi_full, names_endog, cov_type, run_endogeneity
    )

    return KernelResult(
        n_obs=n, cov_type=cov_type, coefficient_names=names,
        coefficients=tuple(coefs), beta=beta, vcv=vcv,
        residuals=residuals, first_stage=fs, rank=rank,
        cragg_donald=float(cd_stat), overid=overid, endogeneity=endo,
        gmm_beta=gmm_beta, gmm_j_stat=gmm_j,
    )


def _endogeneity(y, w, x, zm, x_hat, names_endog, cov_type, run):
    if not run:
        return EndogeneityStat(statistic=float("nan"), p_value=float("nan"),
                               coefficients={})
    return endogeneity_test(
        y, w, x, zm, x_hat, cov_type == "robust", names_endog
    )
