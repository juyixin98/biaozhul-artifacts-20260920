"""Monte Carlo check of the first-order covariance propagation.

For each run we perturb every edge transform by a *bona fide* SE(3) sample
drawn from the stated 6D covariance, compose the perturbed chain/loop, and
compare the empirical covariance of the resulting log-space residuals with
the analytic first-order propagation.

This is a genuine numeric check, not a self-consistency identity: the
analytic side uses only the Jacobians; the empirical side uses exact group
operations (exp/compose/log).  Their gap is exactly the linearisation error,
which is what the README documents.
"""

from __future__ import annotations

import numpy as np

from .graph import (
    assemble_propagation,
    build_steps,
    parse_edges,
    reverse_edge_map,
    step_local_cov,
)
from .se3 import adjoint, compose, exp_se3, invert, log_se3
from .linalg_utils import nearest_psd


def _joint_cov(local_covs, policy, rho, cross_map) -> np.ndarray:
    n = len(local_covs)
    big = np.zeros((6 * n, 6 * n))
    for k, S in enumerate(local_covs):
        big[6 * k : 6 * k + 6, 6 * k : 6 * k + 6] = S
    if policy == "cross_blocks":
        for (i, j), C in cross_map.items():
            big[6 * i : 6 * i + 6, 6 * j : 6 * j + 6] = C
            big[6 * j : 6 * j + 6, 6 * i : 6 * i + 6] = C.T
    elif policy == "conservative_rho":
        # rho bounds unknown correlations; for the MC *ground truth* we
        # instantiate the extreme coherent coupling in rotation+translation
        # space: cross block = rho * L_i L_j^T (rank-1, joint PSD).
        factors = []
        for S in local_covs:
            Sp, _ = nearest_psd(S)
            w, V = np.linalg.eigh(Sp)
            factors.append(V * np.sqrt(np.clip(w, 0, None)))
        for i in range(n):
            for j in range(i + 1, n):
                r = float(rho[i, j]) if hasattr(rho, "__getitem__") else float(rho)
                if r <= 0:
                    continue
                Ci = r * factors[i] @ factors[j].T
                big[6 * i : 6 * i + 6, 6 * j : 6 * j + 6] = Ci
                big[6 * j : 6 * j + 6, 6 * i : 6 * i + 6] = Ci.T
    return big


def _sample_block(S, rng, n_samples):
    Sp, _ = nearest_psd(S)
    w, V = np.linalg.eigh(Sp)
    w = np.clip(w, 0, None)
    L = V * np.sqrt(w)
    z = rng.standard_normal((6, n_samples))
    return L @ z  # 6 x n_samples


def run_monte_carlo(
    edge_specs,
    frame_path,
    *,
    convention: str,
    correlation_policy: str,
    rho=None,
    cross_blocks=None,
    n_samples: int = 20000,
    seed: int = 7,
    loop: bool = False,
) -> dict:
    """Run the MC comparison along ``frame_path``.

    ``loop=True`` treats the walk as closed and analyses the log residual
    directly; ``loop=False`` analyses the open-chain total perturbation in the
    log frame of the nominal composed transform.
    """
    rng = np.random.default_rng(seed)
    parse = parse_edges(edge_specs, convention)
    steps, err = build_steps(parse, frame_path)
    if err is not None:
        raise ValueError(f"cannot resolve path {frame_path}: {err}")
    if any(s.edge.cov is None for s in steps):
        missing = [s.edge.edge_id for s in steps if s.edge.cov is None]
        return {
            "status": "skipped",
            "reason": "missing_covariance",
            "missing_edges": missing,
            "message": (
                "Monte Carlo skipped: edges "
                + ", ".join(missing)
                + " have no covariance. The covariance is NOT treated as zero; "
                "the audit refuses to invent noise statistics. Rerun with "
                "explicit assumed covariances."
            ),
        }

    T_total_nom = np.eye(4)
    for s in steps:
        T_total_nom = s.X @ T_total_nom

    prop = assemble_propagation(
        parse,
        steps,
        convention,
        correlation_policy,
        rho,
        cross_blocks or [],
        closed=loop,
        T_total=T_total_nom,
    )
    Sigma_analytic = prop.Sigma

    local_covs = [step_local_cov(s, convention) for s in steps]
    if np.isscalar(rho) or rho is None:
        rho_eff = np.full((len(steps), len(steps)), float(rho or 0.0))
    else:
        order = [s.edge.index for s in steps]
        rho_eff = np.asarray(rho, dtype=float)[np.ix_(order, order)]
    cross_map = {}
    if correlation_policy == "cross_blocks":
        id_to_pos = {}
        for k, s in enumerate(steps):
            id_to_pos.setdefault(s.edge.edge_id, []).append(k)
        for cb in cross_blocks or []:
            pi, pj = id_to_pos[cb.i][0], id_to_pos[cb.j][0]
            C = np.asarray(cb.block, dtype=float)
            if not steps[pi].forward:
                C = reverse_edge_map(steps[pi].edge, convention) @ C
            if not steps[pj].forward:
                C = C @ reverse_edge_map(steps[pj].edge, convention).T
            if pi > pj:
                pi, pj = pj, pi
                C = C.T
            cross_map[(pi, pj)] = C

    joint = _joint_cov(local_covs, correlation_policy, rho_eff, cross_map)
    # Cholesky on the PSD joint covariance (repaired numerically)
    Jpsd, shift = nearest_psd(joint)
    try:
        Lall = np.linalg.cholesky(Jpsd + 1e-14 * np.eye(joint.shape[0]))
    except np.linalg.LinAlgError:
        w, V = np.linalg.eigh(Jpsd)
        Lall = V * np.sqrt(np.clip(w, 0, None))

    Z = rng.standard_normal((6 * len(steps), n_samples))
    Xis = Lall @ Z  # (6n, N)

    n_steps = len(steps)
    residuals = np.zeros((6, n_samples))

    for sample in range(n_samples):
        T_total = np.eye(4)
        for k, s in enumerate(steps):
            xi_k = Xis[6 * k : 6 * k + 6, sample]
            Xk = s.X
            if convention == "right":
                Xk_pert = Xk @ exp_se3(xi_k)
            else:
                Xk_pert = exp_se3(xi_k) @ Xk
            # Walk product: later steps leftmost, W = X_{n-1} ... X_0.
            T_total = Xk_pert @ T_total
        if loop:
            # closure residual is convention-independent at first order
            residuals[:, sample] = log_se3(T_total)
        elif convention == "left":
            # spatial-frame increment log(Tn^-1 T')
            residuals[:, sample] = log_se3(compose(invert(T_total_nom), T_total))
        else:
            # body-frame increment log(T' Tn^-1)
            residuals[:, sample] = log_se3(compose(T_total, invert(T_total_nom)))
    emp = np.cov(residuals, bias=False)
    emp, _ = nearest_psd(emp)

    diff = emp - Sigma_analytic
    fro_analytic = float(np.linalg.norm(Sigma_analytic, "fro"))
    rel_fro = float(np.linalg.norm(diff, "fro") / max(fro_analytic, 1e-30))

    wa = np.linalg.eigvalsh(Sigma_analytic)
    we = np.linalg.eigvalsh(emp)
    pos = wa > 1e-12 * max(wa.max(), 1.0)
    eig_ratio = (we[pos] / wa[pos]).tolist() if pos.any() else []

    # Mahalanobis calibration: fraction of samples inside 95% chi2 ellipse
    Sreg = Sigma_analytic + 1e-10 * np.eye(6)
    maha = np.einsum("in,ij,jn->n", residuals, np.linalg.inv(Sreg), residuals)
    inside95 = float(np.mean(maha <= 12.592))

    rot_std = float(
        np.sqrt(np.mean([np.sum(local_covs[k][3:, 3:].diagonal()) for k in range(n_steps)]))
    )
    return {
        "status": "ok",
        "n_samples": n_samples,
        "seed": seed,
        "n_edges": n_steps,
        "convention": convention,
        "correlation_policy": correlation_policy,
        "per_edge_mean_rotation_std_rad": rot_std,
        "analytic_covariance": Sigma_analytic.tolist(),
        "empirical_covariance": emp.tolist(),
        "relative_frobenius_error": rel_fro,
        "eigenvalue_ratios_empirical_over_analytic": eig_ratio,
        "fraction_inside_chi2_95_ellipse": inside95,
        "joint_cov_spectral_repair": shift,
    }
