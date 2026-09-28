"""Error evidence layer.

Computes the numbers that justify (or refuse to justify) an accuracy claim:

- per-root absolute/relative residuals
- factor-reconstruction error: rebuild monic p from the roots and compare
  coefficients against the input polynomial
- Vieta deviation: elementary symmetric sums recomputed from power sums via
  Newton's identities (an independent path from the convolution-based
  reconstruction) compared against c_k / c_0
- near-multiple-root clusters: roots whose mutual distance is below
  cluster_tol. For a cluster of multiplicity m, residuals of order eps are
  compatible with root errors of order eps**(1/m), so small residuals are
  explicitly NOT accepted as accuracy evidence there; the cluster diameter
  is reported as the honest bound.
"""

from __future__ import annotations

from typing import List

import numpy as np

from .domain import Cluster, Evidence, NormalizedPolynomial
from .kernel.polyeval import poly_derivative_coeffs, poly_eval, poly_from_roots


def root_residuals(poly: NormalizedPolynomial, roots: np.ndarray) -> tuple[np.ndarray, np.ndarray]:
    """(|p(z_i)|, |p(z_i)| / sum_k |c_k| |z_i|^(n-k)) per root.

    The denominator is the standard coefficient-wise scaling, so the relative
    residual is a componentwise backward-error proxy.
    """
    abs_z = np.abs(roots)
    n = poly.degree
    scale = np.zeros_like(abs_z)
    powers = np.ones_like(abs_z)
    for k in range(n, -1, -1):  # c[n-k] multiplies |z|^k
        scale += abs(poly.coeffs[n - k]) * powers
        powers *= abs_z
    scale = np.maximum(scale, np.finfo(float).tiny)
    res_abs = np.abs(poly_eval(poly.coeffs, roots))
    return res_abs, res_abs / scale


def reconstruction_error(poly: NormalizedPolynomial, roots: np.ndarray) -> float:
    """Max relative coefficient deviation between the input polynomial and
    the monic polynomial rebuilt from the computed roots."""
    rebuilt = poly_from_roots(roots)
    monic = poly.coeffs / poly.coeffs[0]
    denom = np.maximum(1.0, np.abs(monic))
    return float(np.max(np.abs(rebuilt - monic) / denom))


def vieta_deviation(poly: NormalizedPolynomial, roots: np.ndarray) -> float:
    """Max |e_k(roots) - (-1)^k c_k/c_0| (relative), with e_k recomputed
    from power sums via Newton's identities — independent of the
    convolution-based reconstruction path."""
    n = poly.degree
    monic = poly.coeffs / poly.coeffs[0]
    # Power sums p_k = sum_i z_i**k, computed incrementally.
    p = np.empty(n + 1, dtype=np.complex128)
    zpow = np.ones(n, dtype=np.complex128)
    for k in range(1, n + 1):
        zpow = zpow * roots
        p[k] = np.sum(zpow)
    # Newton's identities: k*e_k = sum_{i=1..k} (-1)^{i-1} e_{k-i} p_i.
    e = np.empty(n + 1, dtype=np.complex128)
    e[0] = 1.0
    for k in range(1, n + 1):
        s = 0.0 + 0.0j
        for i in range(1, k + 1):
            s += (-1.0) ** (i - 1) * e[k - i] * p[i]
        e[k] = s / k
    # Vieta: c_k / c_0 = (-1)^k e_k for monic coefficients c_k.
    signs = np.array([(-1.0) ** k for k in range(1, n + 1)])
    target = signs * monic[1:]
    deviations = np.abs(e[1:] - target) / np.maximum(1.0, np.abs(target))
    return float(np.max(deviations))


def detect_clusters(roots: np.ndarray, cluster_tol: float) -> List[Cluster]:
    """Union-find clustering: i~j when |z_i - z_j| <= cluster_tol * max(1, |z_i|, |z_j|)."""
    n = len(roots)
    parent = list(range(n))

    def find(a: int) -> int:
        while parent[a] != a:
            parent[a] = parent[parent[a]]
            a = parent[a]
        return a

    def union(a: int, b: int) -> None:
        ra, rb = find(a), find(b)
        if ra != rb:
            parent[rb] = ra

    for i in range(n):
        for j in range(i + 1, n):
            if abs(roots[i] - roots[j]) <= cluster_tol * max(1.0, abs(roots[i]), abs(roots[j])):
                union(i, j)

    groups: dict[int, List[int]] = {}
    for i in range(n):
        groups.setdefault(find(i), []).append(i)

    clusters: List[Cluster] = []
    cid = 0
    for members in sorted(groups.values(), key=lambda m: m[0]):
        if len(members) < 2:
            continue
        pts = roots[members]
        diameter = float(max(abs(a - b) for a in pts for b in pts))
        clusters.append(Cluster(cluster_id=cid, member_indices=list(members), diameter=diameter))
        cid += 1
    return clusters


def isolated_error_bounds(poly: NormalizedPolynomial, roots: np.ndarray) -> np.ndarray:
    """First-order forward-error proxy |p(z)| / |p'(z)| per root (inf if p'=0)."""
    dcoeffs = poly_derivative_coeffs(poly.coeffs)
    dp = np.abs(poly_eval(dcoeffs, roots))
    res_abs, _ = root_residuals(poly, roots)
    with np.errstate(divide="ignore", invalid="ignore"):
        bound = np.where(dp > 0.0, res_abs / dp, np.inf)
    return bound


def build_evidence(
    poly: NormalizedPolynomial,
    roots: np.ndarray,
    *,
    cluster_tol: float,
) -> tuple[Evidence, np.ndarray, np.ndarray, np.ndarray]:
    """Assemble the full evidence block.

    Returns (evidence, residual_abs, residual_rel, error_bound_per_root).
    """
    res_abs, res_rel = root_residuals(poly, roots)
    recon = reconstruction_error(poly, roots)
    vieta = vieta_deviation(poly, roots)
    clusters = detect_clusters(roots, cluster_tol)
    bounds = isolated_error_bounds(poly, roots)

    cluster_of = {}
    for cl in clusters:
        for idx in cl.member_indices:
            cluster_of[idx] = cl

    error_bound = np.empty(len(roots))
    for i in range(len(roots)):
        cl = cluster_of.get(i)
        if cl is None:
            error_bound[i] = bounds[i]
        else:
            # Honest bound for clustered roots: residuals understate the
            # error by up to a 1/m power, so report the cluster diameter.
            error_bound[i] = cl.diameter

    if clusters:
        note = (
            "near-multiple root cluster(s) detected: for a cluster of "
            "multiplicity m, residuals of order eps are compatible with root "
            "errors of order eps**(1/m); per-root residuals are therefore NOT "
            "accepted as accuracy evidence for clustered roots, and the "
            "cluster diameter is reported as the error bound instead"
        )
    else:
        note = (
            "all roots isolated at the cluster tolerance; per-root residuals "
            "and first-order bounds apply"
        )

    evidence = Evidence(
        max_residual_rel=float(np.max(res_rel)),
        reconstruction_error=recon,
        vieta_max_deviation=vieta,
        clusters=clusters,
        accuracy_note=note,
    )
    return evidence, res_abs, res_rel, error_bound
