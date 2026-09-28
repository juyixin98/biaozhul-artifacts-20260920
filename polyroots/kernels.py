"""计算内核。

两个独立内核，共享输出契约 KernelOutput：
- companion: Frobenius 伴随矩阵 + scipy LAPACK(ZGEEV) 特征值。
- aberth:    Aberth–Ehrlich 同时迭代，逐根记录收敛/未收敛状态与迭代数，
             迭代耗尽不抛异常，由 status 字段表达。

内核只负责算根与收敛状态；残差、重构、Vieta、聚类等证据在 evidence 模块计算。
"""

from __future__ import annotations

import dataclasses
from typing import Any

import numpy as np
from scipy import linalg

from .errors import ComputationFailedError
from .models import KernelName


@dataclasses.dataclass(frozen=True)
class KernelOutput:
    roots: np.ndarray                  # complex128, 长度 = degree
    converged: np.ndarray              # bool, 逐根
    per_root_iterations: np.ndarray    # int，未逐根迭代的内核为 0
    iterations_used: int               # 总迭代轮数（companion 为 0）
    intermediate: dict[str, Any]       # 关键中间状态，供日志重放判断


# ---------------------------------------------------------------- Horner

def derivative_at(c: np.ndarray, z: complex) -> complex:
    """综合除法得到商多项式，再做一轮 Horner 求 f'(z)，实现直白可核对。"""
    n = c.size - 1
    # 商多项式（综合除法）系数：b_n=1, b_k = c_k + z b_{k+1}
    b = np.empty(n + 1, dtype=np.complex128)
    b[n] = 1.0 + 0.0j
    for k in range(n - 1, -1, -1):
        b[k] = c[k] + z * b[k + 1]
    # f(z) = b[0]；f'(z) = 商 b[1..n] 在 z 处取值
    d = b[n]
    for k in range(n - 1, 0, -1):
        d = d * z + b[k]
    return d


def evaluate_f(c: np.ndarray, z: complex) -> complex:
    """标准 Horner 求 f(z)，供残差模块与 Aberth 迭代共用。"""
    val = c[-1] + 0.0j
    for k in range(c.size - 2, -1, -1):
        val = val * z + c[k]
    return val


def _initial_guesses(c: np.ndarray, rng: np.random.Generator) -> np.ndarray:
    """等角分布 + 种子化随机扰动的初值；半径取根模 Cauchy 型估计。"""
    n = c.size - 1
    # monic 升序：根的上界 1 + max|c_k|，再用几何均值收紧
    radius_terms = [
        abs(c[k]) ** (1.0 / (n - k))
        for k in range(0, n)
        if abs(c[k]) > 0.0
    ]
    radius = 2.0 * max(radius_terms) if radius_terms else 1.0
    if not np.isfinite(radius) or radius <= 0.0:
        radius = 1.0
    angles = np.linspace(0.0, 2.0 * np.pi, n, endpoint=False)
    angles = angles + rng.uniform(0.0, 2.0 * np.pi / max(n, 1), n)
    radii = radius * (0.8 + 0.4 * rng.uniform(size=n))
    return radii * np.exp(1j * angles)


# ---------------------------------------------------------------- Aberth 内核

def aberth_kernel(
    c: np.ndarray,
    max_iterations: int,
    tol: float,
    seed: int,
) -> KernelOutput:
    """Aberth–Ehrlich 同时迭代。

    收敛判据（逐根，同时满足）：
      校正量 |Δz|/(1+|z|) < tol，且相对残差 |f(z)|/scale(z) < tol。
    近重根会让二者同时停滞 → 迭代耗尽时保留为 unconverged，而非伪装收敛。
    """
    n = c.size - 1
    if n == 0:
        return KernelOutput(np.array([], dtype=np.complex128),
                            np.array([], dtype=bool), np.array([], dtype=int),
                            0, {"note": "degree 0, no roots"})
    if n == 1:
        root = np.array([-c[0] / c[1]], dtype=np.complex128)
        return KernelOutput(root, np.array([True]), np.array([1]), 1,
                            {"note": "linear solved directly"})

    rng = np.random.default_rng(seed)
    z = _initial_guesses(c, rng)
    converged = np.zeros(n, dtype=bool)
    iters = np.zeros(n, dtype=int)
    last_corrections = np.full(n, np.inf)
    last_residuals = np.full(n, np.inf)
    stalled_rounds = 0
    iterations_used = max_iterations

    for it in range(1, max_iterations + 1):
        new_z = z.copy()
        for i in range(n):
            if converged[i]:
                continue
            candidate, rel_corr, rel_res = _aberth_step(c, z, i, it)
            last_corrections[i] = rel_corr
            last_residuals[i] = rel_res
            iters[i] = it
            if rel_corr < tol and rel_res < tol:
                converged[i] = True
            else:
                new_z[i] = candidate

        still_active = ~converged
        max_active_corr = (
            float(np.max(last_corrections[still_active])) if still_active.any() else 0.0
        )
        z = new_z
        if converged.all():
            return KernelOutput(z, converged, iters, it,
                                {"final_max_correction": max_active_corr,
                                 "final_max_residual": float(np.max(last_residuals))})
        # 停滞检测：活跃根的最大校正量已很小但跨不过 tol，连续若干轮几乎不变，
        # 提前退出并把这些根保留为未收敛，而不是空耗预算。
        if max_active_corr < 1e-3 and it >= 10:
            stalled_rounds += 1
        else:
            stalled_rounds = 0
        if stalled_rounds >= 20:
            iterations_used = it
            break

    return KernelOutput(
        z, converged, iters, iterations_used,
        {"final_max_correction": float(np.max(last_corrections[~converged]))
         if (~converged).any() else 0.0,
         "final_max_residual": float(np.max(last_residuals[~converged]))
         if (~converged).any() else 0.0,
         "stalled_rounds": stalled_rounds},
    )


def _repulsion_sum(z: np.ndarray, i: int) -> complex:
    """sum_{j!=i} 1/(z_i-z_j)；根重合时用最小正偏移保护除零。"""
    total = 0.0j
    for j in range(z.size):
        if j == i:
            continue
        diff = z[i] - z[j]
        if diff == 0.0j:
            diff = 1e-300 + 0.0j
        total += 1.0 / diff
    return total


def _nudge(z_i: complex, it: int, phase: float = 1.0) -> complex:
    return z_i + 1e-12 * (1.0 + abs(z_i)) * np.exp(1j * it * phase)


def _aberth_step(
    c: np.ndarray, z: np.ndarray, i: int, it: int
) -> tuple[complex, float, float]:
    """单根 Aberth-Ehrlich 更新。

    返回 (候选新值, 相对校正量, 相对残差)。导数为零或分母为零时退化为微小
    扰动并返回 inf 指标，使该根本轮不可能被判为收敛。
    """
    fval = evaluate_f(c, z[i])
    fp = derivative_at(c, z[i])
    if fp == 0.0j:  # 落在临界点：微扰后下一轮继续
        return _nudge(z[i], it), np.inf, np.inf

    newton = fval / fp
    denom = 1.0 - newton * _repulsion_sum(z, i)
    if denom == 0.0j:
        return _nudge(z[i], it, phase=0.7), np.inf, np.inf

    delta = newton / denom
    candidate = z[i] - delta
    if not (np.isfinite(candidate.real) and np.isfinite(candidate.imag)):
        raise ComputationFailedError(
            "Aberth 迭代产生非有限值（NaN/Inf），数值过程失败",
            {"iteration": it, "root_index": i, "z": repr(z[i])},
        )
    f_scale = _residual_scale(c, z[i])
    rel_corr = abs(delta) / (1.0 + abs(z[i]))
    rel_res = abs(fval) / f_scale if f_scale > 0 else abs(fval)
    return candidate, rel_corr, rel_res


def _residual_scale(c: np.ndarray, z: complex) -> float:
    """f(z) 的尺度估计：sum |c_k| |z|^k，使残差成为“相对”量。"""
    az = abs(z)
    scale = 0.0
    power = 1.0
    for k in range(c.size):
        scale += abs(c[k]) * power
        power *= az
    return scale if scale > 0 else 1.0


# ---------------------------------------------------------------- 伴随矩阵内核

def companion_kernel(c: np.ndarray) -> KernelOutput:
    """Frobenius 伴随矩阵特征值。首一升序系数 c，c[n]=1。"""
    n = c.size - 1
    if n == 0:
        return KernelOutput(np.array([], dtype=np.complex128),
                            np.array([], dtype=bool), np.array([], dtype=int),
                            0, {"note": "degree 0, no roots"})
    if n == 1:
        root = np.array([-c[0] / c[1]], dtype=np.complex128)
        return KernelOutput(root, np.array([True]), np.array([0]), 0,
                            {"note": "linear solved directly"})

    C = np.zeros((n, n), dtype=np.complex128)
    for k in range(n - 1):
        C[k + 1, k] = 1.0
    C[:, -1] = -c[:n]
    try:
        eig = linalg.eigvals(C)
    except linalg.LinAlgError as exc:
        raise ComputationFailedError(
            f"LAPACK 特征值求解失败: {exc}", {"shape": n}
        ) from exc
    if not np.all(np.isfinite(eig)):
        raise ComputationFailedError(
            "伴随矩阵特征值中出现非有限值", {"nonfinite": int(np.sum(~np.isfinite(eig)))}
        )
    return KernelOutput(
        np.asarray(eig, dtype=np.complex128),
        np.ones(n, dtype=bool),  # LAPACK 不提供逐根收敛信息；可信度由证据模块裁定
        np.zeros(n, dtype=int), 0,
        {"method": "ZGEEV via scipy.linalg.eigvals", "matrix_norm": float(linalg.norm(C, 1))},
    )


# ---------------------------------------------------------------- 选择

def resolve_kernel_name(name: KernelName, degree: int) -> KernelName:
    if name is KernelName.AUTO:
        return KernelName.COMPANION if degree <= 100 else KernelName.ABERTH
    return name


def run_kernel(name: KernelName, c: np.ndarray, max_iterations: int,
               tol: float, seed: int) -> tuple[KernelOutput, KernelName]:
    used = resolve_kernel_name(name, c.size - 1)
    if used is KernelName.COMPANION:
        return companion_kernel(c), used
    return aberth_kernel(c, max_iterations, tol, seed), used
