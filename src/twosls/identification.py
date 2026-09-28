"""Identification and instrument-relevance diagnostics.

Three distinct questions are kept separate on purpose:

1. Order condition     L >= k                         (counting; necessary)
2. Rank condition      rank(M_X Z projected onto M_X Y) = k
3. Relevance/strength  first-stage F, Sanderson-Windmeijer conditional F,
                       partial R2, Cragg-Donald g_min  (can be weak even when
                       the model is formally identified)

The exclusion restriction is NOT tested here and cannot follow from any of
these numbers; it is an economic assertion recorded in the outcome.
"""
from __future__ import annotations

import numpy as np
from scipy import stats

from .config import Settings
from .contract import EstimationData, FirstStageResult, IdentificationResult
from .linalg import annihilator, matrix_rank_detail, ols_fit, pinv_sym, projection


def _partial_out(a: np.ndarray, b: np.ndarray) -> np.ndarray:
    """M_a b without forming the full annihilator when b is tall."""
    pa_b = a @ (pinv_sym(a.T @ a) @ (a.T @ b))
    return b - pa_b


def instrument_correlation(Z: np.ndarray) -> np.ndarray:
    sd = Z.std(axis=0, ddof=1)
    if np.any(sd == 0):
        return np.full((Z.shape[1], Z.shape[1]), np.nan)
    Zn = (Z - Z.mean(axis=0)) / sd
    return Zn.T @ Zn / (Z.shape[0] - 1)


def first_stage_diagnostics(data: EstimationData) -> list[FirstStageResult]:
    n, m, L = data.nobs, data.n_exog, data.n_instruments
    k = data.n_endog
    X, Z, Y = data.X, data.Z, data.Y
    Q = np.column_stack([X, Z])

    results: list[FirstStageResult] = []
    for j in range(k):
        yj = Y[:, j]

        # restricted: Y_j on X ; unrestricted: Y_j on [X, Z]
        _, resid_r, _ = ols_fit(yj, X)
        _, resid_u, _ = ols_fit(yj, Q)
        ssr_r = float(resid_r @ resid_r)
        ssr_u = float(resid_u @ resid_u)

        df_u = n - m - L
        partial_r2 = 1.0 - ssr_u / ssr_r if ssr_r > 0 else float("nan")
        f_stat = ((ssr_r - ssr_u) / L) / (ssr_u / df_u) if ssr_u > 0 else float("inf")
        f_p = float(stats.f.sf(f_stat, L, df_u)) if np.isfinite(f_stat) else 0.0

        denom_adj = n - m - L - 1
        partial_r2_adj = (
            1.0 - (1.0 - partial_r2) * (n - m - 1) / denom_adj if denom_adj > 0 else float("nan")
        )

        # Sanderson-Windmeijer conditional F: does Z explain Y_j *beyond the
        # other endogenous regressors*? Identical to the plain F when k == 1.
        sw_f = _sanderson_windmeijer_f(data, j)
        g_j = f_stat * L / df_u if np.isfinite(f_stat) else float("inf")

        fs_coef_vec, _, _ = ols_fit(yj, Q)
        names = data.exog_names + data.instrument_names
        coef_map = {name: float(val) for name, val in zip(names, fs_coef_vec)}

        results.append(
            FirstStageResult(
                endogenous_name=data.endog_names[j],
                partial_r2=float(partial_r2),
                partial_r2_adjusted=float(partial_r2_adj),
                f_statistic=float(f_stat),
                f_p_value=f_p,
                effective_f_statistic=float(sw_f),
                eigen_min_statistic=float(g_j),
                coefficients=coef_map,
            )
        )
    return results


def _sanderson_windmeijer_f(data: EstimationData, j: int) -> float:
    """Sanderson-Windmeijer conditional F for endogenous regressor j.

    Regress the partialled-out Y_j on the OTHER endogenous regressors and on
    the (partialled-out) instruments, then test joint significance of the
    instruments. Conditioning on the actual other endogs is what makes the
    statistic sensitive to a first-stage coefficient matrix whose columns
    are nearly collinear -- the failure mode where marginal first-stage Fs
    are all large but the structural coefficients are not jointly identified.
    Identical to the ordinary first-stage F when k == 1.
    """
    n, m, L = data.nobs, data.n_exog, data.n_instruments
    others = [c for c in range(data.n_endog) if c != j]
    Yt = _partial_out(data.X, data.Y)
    Zt = _partial_out(data.X, data.Z)

    if others:
        Yother = Yt[:, others]
        _, resid_restricted, _ = ols_fit(Yt[:, j], Yother)
        design_u = np.column_stack([Yother, Zt])
    else:
        resid_restricted = Yt[:, j]
        design_u = Zt

    _, resid_u, _ = ols_fit(Yt[:, j], design_u)
    ssr_r = float(resid_restricted @ resid_restricted)
    ssr_u = float(resid_u @ resid_u)
    df_u = n - m - (data.n_endog - 1) - L
    if ssr_u <= 0 or df_u <= 0:
        return float("inf")
    return float(((ssr_r - ssr_u) / L) / (ssr_u / df_u))


def cragg_donald(data: EstimationData) -> float:
    """Cragg-Donald g_min (the eigenvalue; multiply by (n-m-L)/L for CD stat).

    g_min = min eig of (Yt'P_Zt Yt)(Yt'M_Q Y)^{-1}, Yt=M_X Y, Zt=M_X Z.
    """
    X, Y, Z = data.X, data.Y, data.Z
    Q = np.column_stack([X, Z])
    Yt = _partial_out(X, Y)
    Zt = _partial_out(X, Z)
    MqY = Yt - Q @ (pinv_sym(Q.T @ Q) @ (Q.T @ Yt))  # Y residualized on [X,Z]

    A = Yt.T @ (projection(Zt) @ Yt)
    B = MqY.T @ MqY
    # Symmetric form: B^{-1/2} A B^{-1/2}
    wb, vb = np.linalg.eigh(B)
    if wb[0] <= np.finfo(float).eps * max(1.0, abs(wb[-1])):
        return 0.0
    B_inv_sqrt = (vb * (1.0 / np.sqrt(wb))) @ vb.T
    sym = B_inv_sqrt @ A @ B_inv_sqrt
    eigvals = np.linalg.eigvalsh(0.5 * (sym + sym.T))
    return float(max(eigvals[0], 0.0))


def check_identification(
    data: EstimationData, first_stages: list[FirstStageResult], settings: Settings
) -> IdentificationResult:
    k, m, L = data.n_endog, data.n_exog, data.n_instruments
    n = data.nobs
    reasons: list[str] = []

    # --- order ------------------------------------------------------------
    order_ok = L >= k
    order_detail = f"L={L} excluded instruments vs k={k} endogenous regressors; " + (
        "order condition L>=k satisfied" if order_ok else "order condition L>=k FAILS"
    )

    # --- rank of instrument block and of cross-product --------------------
    Zt = _partial_out(data.X, data.Z)
    rank_z, sv_z = matrix_rank_detail(Zt, settings.rank_tol)
    cross_full = False
    rank_cross = -1
    sv_cross = np.array([0.0])
    if rank_z >= k:
        Yt = _partial_out(data.X, data.Y)
        cross = Zt.T @ Yt
        sv_cross = np.linalg.svd(cross, compute_uv=False)
        # Absolute scale: a cross-product whose singular values are all tiny
        # relative to the NORMS OF THE CONSTITUENT MATRICES is rank-deficient.
        # A purely self-relative tolerance cannot detect this (it normalizes
        # the tiny scale away).
        cross_scale = float(sv_z[0]) * float(np.linalg.svd(Yt, compute_uv=False)[0])
        cross_cutoff = settings.rank_tol * cross_scale
        rank_cross = int(np.sum(sv_cross > cross_cutoff))
        cross_full = rank_cross == k

    rank_ok = order_ok and rank_z == L and cross_full
    if not order_ok:
        reasons.append(
            "order condition fails: fewer excluded instruments than endogenous regressors"
        )
    if rank_z < L:
        reasons.append(
            f"instrument block rank {rank_z} < {L} after partialling controls "
            "(exact or near-exact collinearity among instruments)"
        )
    if rank_z >= k and not cross_full:
        smallest = float(sv_cross[min(k - 1, len(sv_cross) - 1)]) if len(sv_cross) else 0.0
        reasons.append(
            f"rank condition fails: rank(M_X Z' M_X Y)={rank_cross} < k={k}; "
            f"instruments are uncorrelated with the endogenous variation "
            f"(smallest sv={smallest:.3e}, cutoff={cross_cutoff:.3e})"
        )

    # --- instrument correlations / collinear pairs ------------------------
    corr = instrument_correlation(data.Z)
    corr_list = corr.tolist()
    if L > 1:
        off_diag = corr.copy()
        np.fill_diagonal(off_diag, np.nan)
        corr_min = float(np.nanmin(off_diag))
    else:
        corr_min = 1.0
    collinear_pairs: list[list[str]] = []
    names = data.instrument_names
    for a in range(L):
        for b in range(a + 1, L):
            if np.isfinite(corr[a, b]) and abs(corr[a, b]) >= settings.collinear_corr:
                collinear_pairs.append([names[a], names[b]])
    if collinear_pairs:
        reasons.append(f"near-collinear instrument pairs: {collinear_pairs}")

    g_min = cragg_donald(data)
    cd_stat = g_min * (n - m - L) / L

    # --- strength ----------------------------------------------------------
    status = "identified"
    if not rank_ok:
        status = "unidentified"
    else:
        weak_bits = []
        min_f = min((fs.effective_f_statistic for fs in first_stages), default=float("inf"))
        if min_f < settings.weak_f_threshold:
            weak_bits.append(
                f"min conditional first-stage F={min_f:.3f} < {settings.weak_f_threshold:g}"
            )
        if cd_stat < settings.weak_f_threshold:
            weak_bits.append(
                f"Cragg-Donald statistic={cd_stat:.3f} < {settings.weak_f_threshold:g}"
            )
        min_r2 = min((fs.partial_r2 for fs in first_stages), default=1.0)
        if min_r2 < settings.partial_r2_low:
            weak_bits.append(f"min partial R2={min_r2:.4f} < {settings.partial_r2_low:g}")
        if weak_bits:
            status = "weak"
            reasons.append("weak instruments: " + "; ".join(weak_bits))

    return IdentificationResult(
        order_condition=order_ok,
        order_detail=order_detail,
        rank_condition=rank_ok,
        rank_value=max(rank_cross, 0),
        rank_required=k,
        instrument_correlation_matrix=corr_list,
        instrument_correlation_min=corr_min,
        collinear_instrument_pairs=collinear_pairs,
        cragg_donald_statistic=float(cd_stat),
        status=status,
        reasons=reasons,
    )
