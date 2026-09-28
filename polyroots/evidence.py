"""误差证据。

独立于内核计算可信度证据，核心原则：**小残差不等于根准确**，近重根必须通过
聚类间距与敏感性指标额外标注。

- 相对残差：|f(z)| / S(z)，S(z)=Σ|c_k||z|^k（首一系数），尺度无关。
- 近重根聚类：相对根间距 |z_i-z_j|/(1+|z|) 小于 cluster_tol 即并簇。
- 敏感性指标 κ_i = S(z_i)/(|f'(z_i)|(1+|z_i|))：首一多项式单根条件数，
  近重根处 f' 趋零，κ 必然很大；根的真实误差量级 ≈ κ * 系数相对扰动。
- 因子重构：由根逐次相乘重建首一多项式，与规范化输入逐系数比较。
- Vieta：根和 = -c_{n-1}，根积 = (-1)^n c_0。
"""

from __future__ import annotations

import numpy as np

from .models import FactorError, VietaCheck


def residual_scale(c: np.ndarray, z: complex) -> float:
    az = abs(z)
    scale = 0.0
    power = 1.0
    for k in range(c.size):
        scale += abs(c[k]) * power
        power *= az
    return scale if scale > 0.0 else 1.0


def relative_residual(c: np.ndarray, z: complex) -> float:
    fval = _horner(c, z)
    return abs(fval) / residual_scale(c, z)


def _horner(c: np.ndarray, z: complex) -> complex:
    val = c[-1] + 0.0j
    for k in range(c.size - 2, -1, -1):
        val = val * z + c[k]
    return val


def derivative(c: np.ndarray, z: complex) -> complex:
    n = c.size - 1
    if n == 0:
        return 0.0j
    val = n * c[n] + 0.0j  # c[n] = 1
    for k in range(n - 1, 0, -1):
        val = val * z + k * c[k]
    return val


def pair_distances(roots: np.ndarray) -> np.ndarray:
    """相对距离矩阵 D_ij = |z_i-z_j| / (1 + (|z_i|+|z_j|)/2)。"""
    n = roots.size
    d = np.zeros((n, n))
    for i in range(n):
        for j in range(i + 1, n):
            scale = 1.0 + 0.5 * (abs(roots[i]) + abs(roots[j]))
            dij = abs(roots[i] - roots[j]) / scale
            d[i, j] = d[j, i] = dij
    return d


def cluster_roots(roots: np.ndarray, cluster_tol: float) -> list[list[int]]:
    """用并查集聚类；返回成员数 >=2 的簇（单根不成簇）。"""
    n = roots.size
    parent = list(range(n))

    def find(x: int) -> int:
        while parent[x] != x:
            parent[x] = parent[parent[x]]
            x = parent[x]
        return x

    def union(a: int, b: int) -> None:
        ra, rb = find(a), find(b)
        if ra != rb:
            parent[max(ra, rb)] = min(ra, rb)

    d = pair_distances(roots)
    for i in range(n):
        for j in range(i + 1, n):
            if d[i, j] < cluster_tol:
                union(i, j)

    groups: dict[int, list[int]] = {}
    for i in range(n):
        groups.setdefault(find(i), []).append(i)
    clusters = [sorted(members) for members in groups.values() if len(members) >= 2]
    clusters.sort(key=lambda members: members[0])
    return clusters


def nearest_separation(roots: np.ndarray) -> np.ndarray:
    """每根到最近邻的相对距离（近重根证据之一）。"""
    n = roots.size
    d = pair_distances(roots)
    out = np.full(n, np.inf)
    for i in range(n):
        if n >= 2:
            out[i] = float(np.min(np.delete(d[i], i)))
    return out


def sensitivity(c: np.ndarray, roots: np.ndarray) -> np.ndarray:
    """κ_i = S(z_i)/(|f'(z_i)|(1+|z_i|))，单根条件数指标。"""
    kappa = np.zeros(roots.size)
    for i, z in enumerate(roots):
        fp = derivative(c, z)
        denom = abs(fp) * (1.0 + abs(z))
        kappa[i] = residual_scale(c, z) / denom if denom > 0.0 else np.inf
    return kappa


def reconstruct_from_roots(roots: np.ndarray) -> np.ndarray:
    """逐次乘 (x-z_i) 重建升序首一系数。"""
    poly = np.array([1.0 + 0.0j])
    for z in roots:
        poly = np.convolve(poly, np.array([-z, 1.0 + 0.0j]))
    return poly


def _envelope(roots: np.ndarray) -> np.ndarray:
    """积多项式 Π(x+|z_i|) 的系数：重构第 k 系数的“积模长上界包络”。"""
    env = np.array([1.0])
    for z in roots:
        env = np.convolve(env, np.array([abs(z), 1.0]))
    return env


def high_precision_reconstruction_error(
    c: np.ndarray, roots: np.ndarray, dps: int
) -> float:
    """用 mpmath 高精度对同一批 float64 根做重构（独立算术路径）。

    这检验的是“给定这批根，float64 重构的大误差是否纯属消去假象”，
    而不是另算一套根。
    """
    import mpmath as mp

    mp.mp.dps = dps
    poly: list = [mp.mpf("1")]
    mroots = [mp.mpc(mp.mpf(float(z.real)), mp.mpf(float(z.imag))) for z in roots]
    for z in mroots:
        nxt = [mp.mpf("0")] * (len(poly) + 1)
        for k, v in enumerate(poly):
            nxt[k] += -z * v
            nxt[k + 1] += v
        poly = nxt
    target = [mp.mpc(mp.mpf(float(c[k].real)), mp.mpf(float(c[k].imag)))
              for k in range(c.size)]
    worst = mp.mpf("0")
    for got, want in zip(poly, target):
        worst = max(worst, abs(got - want) / (1 + abs(want)))
    return float(worst)


def factor_error(
    c: np.ndarray,
    roots: np.ndarray,
    *,
    high_precision: bool = True,
    max_hp_degree: int = 200,
) -> FactorError:
    rec = reconstruct_from_roots(roots)
    if rec.size != c.size:
        raise ValueError("重构系数长度与输入不一致")

    env = _envelope(roots)
    robust_denom = env + np.abs(c)
    rel_robust = np.abs(rec - c) / np.maximum(robust_denom, 1e-300)
    strict = np.abs(rec - c) / (1.0 + np.abs(c))

    strict_max = float(np.max(strict))
    robust_max = float(np.max(rel_robust))
    ratio = strict_max / robust_max if robust_max > 0.0 else 1.0

    zero_count = int(np.sum(np.abs(c) < 1e-14 * (1.0 + np.max(np.abs(c)))))
    hp_error: float | None = None
    # 仅当严格误差疑似被消去放大（稀疏/近抵消）时才付出高精度成本
    if high_precision and c.size - 1 <= max_hp_degree and ratio > 1e3:
        dps = min(90, 50 + (c.size - 1))
        hp_error = high_precision_reconstruction_error(c, roots, dps)

    return FactorError(
        max_rel_coeff_error=robust_max,
        rms_rel_coeff_error=float(np.sqrt(np.mean(rel_robust ** 2))),
        constant_term_error=float(rel_robust[0]),
        strict_float64_error=strict_max,
        high_precision_error=hp_error,
        cancellation_ratio=float(ratio),
        zero_coeff_count=zero_count,
        reconstructed_coeffs_asc=[complex(v) for v in rec],
    )


def vieta_check(c: np.ndarray, roots: np.ndarray) -> VietaCheck:
    n = c.size - 1
    root_sum = complex(np.sum(roots)) if roots.size else 0.0j
    root_product = complex(np.prod(roots)) if roots.size else 1.0j

    target_sum = -c[n - 1] if n >= 1 else 0.0j
    target_prod = ((-1.0) ** n) * c[0]

    def rel_err(got: complex, want: complex) -> float:
        return abs(got - want) / (1.0 + abs(want))

    return VietaCheck(
        sum_abs_error=abs(root_sum - target_sum),
        sum_rel_error=rel_err(root_sum, target_sum),
        product_abs_error=abs(root_product - target_prod),
        product_rel_error=rel_err(root_product, target_prod),
    )
