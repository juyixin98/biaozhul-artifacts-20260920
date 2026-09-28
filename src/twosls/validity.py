"""Validity diagnostics: over-identification and endogeneity tests.

Over-identification (only possible when L > k)
----------------------------------------------
* homoskedastic: Sargan's score  n * R2_u  from regressing the 2SLS residual
  on Q = [X, Z],  chi2(L - k).
* robust: Wooldridge's heteroskedasticity-robust score, using the Schur
  complement block of (Q' diag(e^2) Q)^{-1} on Z,  chi2(L - k).

A failure means *at least one* instrument is inconsistent with the assumed
orthogonality under the maintained model -- it cannot identify which, and a
non-rejection is not proof that the exclusion restriction holds (low power is
always an alternative explanation).

Endogeneity: augmented-regression (Durbin-Wu-Hausman) test
----------------------------------------------------------
Regress y on W = [Y, X] together with the first-stage residuals V_hat:
    y = Y beta + X gamma + V_hat' rho + u
H0: rho = 0 (Y exogenous). The Wald statistic uses the requested covariance
(robust when the caller asked for robust SEs). The OLS/IV coefficient
contrast is reported alongside for transparency.
"""
from __future__ import annotations

import numpy as np
from scipy import stats

from .contract import EndogeneityResult, OveridResult
from .kernel import KernelArtifacts
from .linalg import ols_fit, pinv_sym


def sargan_test(art: KernelArtifacts, data, robust: bool) -> OveridResult:
    L, k = data.n_instruments, data.n_endog
    if L <= k:
        return OveridResult(
            test_name="wooldridge_score" if robust else "sargan",
            testable=False,
            statistic=None,
            p_value=None,
            degrees_of_freedom=None,
            verdict="untestable",
            detail=(
                f"just- or under-identified (L={L}, k={k}): over-identifying "
                "restrictions cannot be tested; exclusion remains an assumption"
            ),
        )

    df = L - k
    e = art.residuals
    X, Z = data.X, data.Z
    n = data.nobs

    if not robust:
        # n * uncentered R2 of e on [X, Z]. X'e = 0 by the IV first-order
        # conditions, so the explanatory power comes from Z alone.
        q = art.Q
        ssr = float(e @ e)
        explained = e @ q @ pinv_sym(q.T @ q) @ q.T @ e
        stat = float(n * explained / ssr) if ssr > 0 else float("nan")
    else:
        # Robust score: e'Z (Z' M^D_X Z)^{-1} Z'e, D = diag(e^2).
        D = e * e
        ZD = Z.T * D  # (L,n) with row scaling
        ZDZ = ZD @ Z
        ZDX = ZD @ X
        XDX = X.T @ (X * D[:, None])
        schur = ZDZ - ZDX @ pinv_sym(XDX) @ ZDX.T
        stat = float(e @ Z @ pinv_sym(schur) @ Z.T @ e)

    p = float(stats.chi2.sf(stat, df)) if np.isfinite(stat) else float("nan")
    verdict = "reject" if np.isfinite(p) and p < 0.05 else "not_reject"
    detail = (
        f"chi2({df})={'rejects' if verdict == 'reject' else 'does not reject'} "
        "orthogonality of over-identifying instruments at 5%; "
        + ("exogeneity of ALL instruments is still not proven (test has power only against some violations)"
           if verdict == "not_reject" else
           "at least one instrument is inconsistent with exogeneity under the maintained model")
    )
    return OveridResult(
        test_name="wooldridge_score" if robust else "sargan",
        testable=True,
        statistic=stat,
        p_value=p,
        degrees_of_freedom=df,
        verdict=verdict,
        detail=detail,
    )


def endogeneity_test(art: KernelArtifacts, data, robust: bool) -> EndogeneityResult:
    """Durbin-Wu-Hausman via the augmented regression, plus OLS/IV contrast."""
    n, k = data.nobs, data.n_endog
    W = art.W
    V_hat = art.first_stage_resid  # (n,k)

    # Plain OLS for the contrast
    ols_coefs, _, _ = ols_fit(data.y, W)
    iv_coefs = art.delta
    ols_contrast = ols_coefs[:k].tolist()
    ivs_contrast = iv_coefs[:k].tolist()

    # Augmented regression: y on [W, V_hat]. Note [Y, V_hat] spans the same
    # space as [Y_hat, V_hat], on which OLS reproduces the 2SLS coefficients.
    A = np.column_stack([W, V_hat])
    coefs_aug, resid_aug, _ = ols_fit(data.y, A)
    r = W.shape[1]
    rho = coefs_aug[r:]  # k coefficients on first-stage residuals

    dof = n - A.shape[1]
    sigma2_aug = float(resid_aug @ resid_aug / dof)

    if not robust:
        AtA_inv = pinv_sym(A.T @ A)
        var_rho = sigma2_aug * AtA_inv[r:, r:]
    else:
        AtA_inv = pinv_sym(A.T @ A)
        meat = A.T @ (A * (resid_aug ** 2)[:, None])
        cov_full = (n / max(n - A.shape[1], 1)) * (AtA_inv @ meat @ AtA_inv)
        var_rho = cov_full[r:, r:]

    wald = float(rho @ pinv_sym(var_rho) @ rho)
    # Use the number of clearly positive eigenvalues as the effective df
    eig = np.linalg.eigvalsh(0.5 * (var_rho + var_rho.T))
    df_eff = int(np.sum(eig > np.finfo(float).eps * max(1.0, abs(eig[-1]))))
    df_eff = max(df_eff, 1)
    p = float(stats.chi2.sf(wald, df_eff))

    verdict = "inconclusive"
    if np.isfinite(p):
        verdict = "endogenous" if p < 0.05 else "not_endogenous"

    return EndogeneityResult(
        statistic=wald,
        p_value=p,
        degrees_of_freedom=df_eff,
        verdict=verdict,
        ols_contrast=ols_contrast,
        ivs_contrast=ivs_contrast,
    )
