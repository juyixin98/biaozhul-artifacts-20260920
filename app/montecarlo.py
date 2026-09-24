"""Monte Carlo 核对一阶协方差传播近似。

对给定标定链，按各边协方差（及相关性模型）抽样扰动，真实地按
``E' = E Exp(ξ)``（右）或 ``Exp(ξ) E``（左）合成带噪变换，沿链求实际
SE(3) 乘积，再统计闭环/末端扰动的样本协方差，与一阶线性传播
``Σ = J blockdiag(Σ_k) Jᵀ`` 比较。

覆盖三类场景：
* 小角度（σ_rot ~ 1e-3 rad）：一阶近似应高度吻合；
* 长链（8~12 条边）：一阶近似的误差随链长与扰动累积；
* 缺失协方差：传播结果必须标记 unknown，且不得被独立假设替代。

向量化实现：se3_exp 支持 (S,6) 批量输入。
"""

from __future__ import annotations

from dataclasses import dataclass

import numpy as np

from .chain import Factor, evaluate_chain, aggregate_covariance
from .se3 import se3_exp, se3_log, inv_tf


def se3_exp_batch(xi: np.ndarray) -> np.ndarray:
    """批量 se3_exp：xi shape (S,6) -> T shape (S,4,4)。"""
    S = xi.shape[0]
    v = xi[:, :3]
    phi = xi[:, 3:]
    angles = np.linalg.norm(phi, axis=1)  # (S,)
    T = np.broadcast_to(np.eye(4), (S, 4, 4)).copy()

    small = angles < 1e-8
    # ---- 小角度分支 ----
    if small.any():
        ph = phi[small]
        K = _skew_batch(ph)  # (s,3,3)
        R = np.eye(3) + K + 0.5 * K @ K
        Vm = np.eye(3) + 0.5 * K + (1.0 / 6.0) * (K @ K)
        T[small, :3, :3] = R
        T[small, :3, 3] = np.einsum("sij,sj->si", Vm, v[small])

    # ---- 常规分支 ----
    big = ~small
    if big.any():
        ph = phi[big]
        th = angles[big][:, None, None]  # (s,1,1)
        n = ph / angles[big][:, None]
        K = _skew_batch(n)
        s_t = np.sin(th)
        c_t = np.cos(th)
        R = np.eye(3) + s_t * K + (1.0 - c_t) * (K @ K)
        # V = I + (1-cos)/θ² [φ] + (θ-sin)/θ³ [φ]²
        Kt = _skew_batch(ph)
        Vm = (
            np.eye(3)
            + ((1.0 - np.cos(th)) / th**2) * Kt
            + ((th - np.sin(th)) / th**3) * (Kt @ Kt)
        )
        T[big, :3, :3] = R
        T[big, :3, 3] = np.einsum("sij,sj->si", Vm, v[big])
    return T


def _skew_batch(v: np.ndarray) -> np.ndarray:
    # 与 se3.skew 一致：
    # [0  -v2  v1; v2  0  -v0; -v1  v0  0]
    S = v.shape[0]
    K = np.zeros((S, 3, 3))
    K[:, 0, 1] = -v[:, 2]
    K[:, 0, 2] = v[:, 1]
    K[:, 1, 0] = v[:, 2]
    K[:, 1, 2] = -v[:, 0]
    K[:, 2, 0] = -v[:, 1]
    K[:, 2, 1] = v[:, 0]
    return K


def inv_batch(T: np.ndarray) -> np.ndarray:
    """批量 SE(3) 逆，(S,4,4)->(S,4,4)。"""
    R = T[:, :3, :3]
    t = T[:, :3, 3]
    out = np.broadcast_to(np.eye(4), T.shape).copy()
    Rt = np.transpose(R, (0, 2, 1))
    out[:, :3, :3] = Rt
    out[:, :3, 3] = -np.einsum("sij,sj->si", Rt, t)
    return out


def matmul_batch(A: np.ndarray, B: np.ndarray) -> np.ndarray:
    return np.einsum("sij,sjk->sik", A, B)


@dataclass
class MCResult:
    scenario: str
    n_edges: int
    samples: int
    convention: str
    first_order_cov: np.ndarray | None
    sample_cov: np.ndarray | None
    covariance_status: str
    fro_relative_error: float | None
    max_abs_error: float | None
    trace_ratio: float | None  # tr(sample)/tr(first-order)
    mean_residual_norm: float | None
    notes: str


def sample_chain_residuals(
    factors: list[Factor],
    convention: str,
    n_samples: int,
    *,
    rho: float | None = None,
    seed: int = 0,
) -> np.ndarray:
    """对链上各边按其 6x6 协方差抽样扰动，返回 (S,6) 实际末端/闭环扰动。

    rho=None：边间独立；rho∈[0,1]：等相关共同因子模型
    ``ξ_k = Σ_k^½(√(1-ρ) z_k + √(ρ) z_0)``。
    """
    rng = np.random.default_rng(seed)
    n = len(factors)
    # 相关性建立在标准正态 z 空间：z_k = sqrt(1-rho) u_k + sqrt(rho) u_0，
    # 所有边共享同一个 u_0；再各自 ξ_k = L_k z_k（L_k 为 Σ_k 的 Cholesky）。
    # 这样 E[z_k z_lᵀ] = rho I（k≠l），经 L 变换后即等相关结构。
    chols = []
    for f in factors:
        if f.edge.covariance is None:
            raise ValueError("cannot sample edge without covariance")
        chols.append(np.linalg.cholesky(f.edge.covariance))

    u_ind = rng.standard_normal((n, n_samples, 6))
    if rho is None:
        z = u_ind
    else:
        u0 = rng.standard_normal((n_samples, 6))
        z = np.sqrt(1.0 - rho) * u_ind + np.sqrt(rho) * u0[None, :, :]

    xi = np.stack(
        [np.einsum("ij,sj->si", chols[k], z[k]) for k in range(n)]
    )  # (n,S,6)

    # 名义链乘积 P0
    P0 = np.eye(4)
    for f in factors:
        P0 = P0 @ f.matrix

    # 逐因子合成扰动矩阵并沿链相乘（批量）
    P = np.broadcast_to(np.eye(4), (n_samples, 4, 4)).copy()
    for k, f in enumerate(factors):
        dE = se3_exp_batch(xi[k])  # Exp(xi_k)
        E = f.edge.T
        if convention == "right":
            Ep = matmul_batch(
                np.broadcast_to(E, (n_samples, 4, 4)), dE
            )
        else:
            Ep = matmul_batch(dE, np.broadcast_to(E, (n_samples, 4, 4)))
        Fp = inv_batch(Ep) if f.inverse else Ep
        P = matmul_batch(P, Fp)

    if convention == "right":
        M = matmul_batch(np.broadcast_to(inv_tf(P0), P.shape), P)
    else:
        M = matmul_batch(P, np.broadcast_to(inv_tf(P0), P.shape))

    # 批量 se3_log（平移部分；旋转用 so3 批量）
    return _se3_log_batch(M)


def _se3_log_batch(T: np.ndarray) -> np.ndarray:
    """批量 se3_log，(S,4,4)->(S,6)。"""
    R = T[:, :3, :3]
    t = T[:, :3, 3]
    phi = _so3_log_batch(R)
    th = np.linalg.norm(phi, axis=1)
    Kt = _skew_batch(phi)
    eye = np.eye(3)
    small = th < 1e-4
    Vi = np.empty((T.shape[0], 3, 3))
    if small.any():
        K = Kt[small]
        Vi[small] = eye - 0.5 * K + (1.0 / 12.0) * (K @ K)
    big = ~small
    if big.any():
        ph = phi[big]
        tt = th[big]
        K = Kt[big]
        half = 0.5 * tt
        sinc = np.sin(half) / half
        b = 1.0 - np.cos(half) / sinc
        cb = (b / tt**2)[:, None, None]
        Vi[big] = eye - 0.5 * K + cb * (K @ K)
    v = np.einsum("sij,sj->si", Vi, t)
    return np.concatenate([v, phi], axis=1)


def _so3_log_batch(R: np.ndarray) -> np.ndarray:
    """批量 so3_log，(S,3,3)->(S,3)。与 se3.so3_log 同算法（向量化）。"""
    v = 0.5 * np.stack(
        [
            R[:, 2, 1] - R[:, 1, 2],
            R[:, 0, 2] - R[:, 2, 0],
            R[:, 1, 0] - R[:, 0, 1],
        ],
        axis=1,
    )
    s = np.linalg.norm(v, axis=1)
    c = np.clip((np.trace(R, axis1=1, axis2=2) - 1.0) / 2.0, -1.0, 1.0)
    phi = np.zeros_like(v)

    # 小角度
    m_small = (s < 1e-4) & (c > 0)
    phi[m_small] = v[m_small]

    # 恰为 pi
    m_pi0 = (c <= 0) & (s < 1e-12)
    if m_pi0.any():
        S = R[m_pi0] + np.eye(3)
        cols = np.argmax(np.linalg.norm(S, axis=1), axis=1)
        ax = S[np.arange(S.shape[0]), :, cols]
        ax /= np.linalg.norm(ax, axis=1, keepdims=True)
        phi[m_pi0] = np.pi * ax

    # 常规（含近 pi，但非上述两特例）
    m_rest = ~(m_small | m_pi0)
    if m_rest.any():
        sr = np.clip(s[m_rest], 0.0, 1.0)
        cr = c[m_rest]
        angle = np.where(
            cr > 0, np.arcsin(sr), np.pi - np.arcsin(sr)
        )
        axis = v[m_rest] / np.clip(s[m_rest], 1e-300, None)[:, None]
        phi[m_rest] = angle[:, None] * axis
    return phi


def run_scenario(
    factors: list[Factor],
    convention: str,
    n_samples: int,
    scenario: str,
    *,
    policy: str = "independent",
    rho_max: float | None = None,
    cross_covs: dict | None = None,
    rho_sample: float | None = None,
    seed: int = 0,
    notes: str = "",
) -> MCResult:
    """完整跑一个场景：一阶传播 vs 样本协方差。"""
    pr = evaluate_chain(factors, convention)
    fo, meta = aggregate_covariance(
        factors,
        pr.jacobian,
        policy=policy,
        rho_max=rho_max,
        cross_covs=cross_covs or {},
    )

    if fo is None:
        return MCResult(
            scenario=scenario,
            n_edges=len(factors),
            samples=n_samples,
            convention=convention,
            first_order_cov=None,
            sample_cov=None,
            covariance_status="unknown",
            fro_relative_error=None,
            max_abs_error=None,
            trace_ratio=None,
            mean_residual_norm=None,
            notes=notes
            or "存在缺失协方差的边：传播标记 unknown，不以独立假设替代",
        )

    residuals = sample_chain_residuals(
        factors, convention, n_samples, rho=rho_sample, seed=seed
    )
    sample_cov = np.cov(residuals, rowvar=False)
    denom = max(np.linalg.norm(sample_cov, "fro"), 1e-300)
    fro_rel = float(np.linalg.norm(fo - sample_cov, "fro") / denom)
    max_abs = float(np.abs(fo - sample_cov).max())
    trace_ratio = float(np.trace(sample_cov) / max(np.trace(fo), 1e-300))
    mean_norm = float(np.linalg.norm(residuals, axis=1).mean())

    return MCResult(
        scenario=scenario,
        n_edges=len(factors),
        samples=n_samples,
        convention=convention,
        first_order_cov=fo,
        sample_cov=sample_cov,
        covariance_status="ok",
        fro_relative_error=fro_rel,
        max_abs_error=max_abs,
        trace_ratio=trace_ratio,
        mean_residual_norm=mean_norm,
        notes=notes,
    )
