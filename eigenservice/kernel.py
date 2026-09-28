"""计算内核: Householder 三对角化 + 隐式 Wilkinson 移位 QL 迭代。

对任意实对称矩阵实际执行 (非硬编码演示):

1. ``householder_tridiagonal`` —— 逐列 Householder 反射把 A 化为
   三对角 T, 并累积正交变换 Q (A = Q T Q^T)。
2. ``tridiagonal_ql`` —— 对三对角阵做带 Wilkinson 移位的隐式 QL
   (EISPACK tql2 型), Givens 旋转同时作用于累积特征向量。
   每个未收敛特征值都要在预算内达到相对机器精度判据; 预算耗尽则
   ``converged=False`` —— 上层绝不能据此报成功。

索引约定: ``f[i]`` 是位置 i 与 i+1 之间的次对角元 (0 <= i < n-1),
``f[n-1]`` 作为零哨兵。
"""

from __future__ import annotations

from dataclasses import dataclass

import numpy as np


@dataclass(frozen=True)
class KernelResult:
    eigenvalues: np.ndarray          # 升序 (仅 converged 时排序)
    eigenvectors: np.ndarray         # 列 j 对应 eigenvalues[j]
    sweeps: int                      # QL 隐式移位步数
    converged: bool
    stalled_index: int | None        # 预算耗尽时卡住的指标
    tridiagonal_offdiag: np.ndarray


def householder_tridiagonal(
    matrix: np.ndarray,
) -> tuple[np.ndarray, np.ndarray, np.ndarray]:
    """Householder 三对角化。

    Returns:
        (diag, offdiag, Q): 三对角对角元、次对角元 (长度 n-1,
        ``offdiag[i]`` 位于 i 与 i+1 之间), 以及 A = Q T Q^T 的 Q。
    """
    n = matrix.shape[0]
    tri = matrix.copy()
    q_acc = np.eye(n)

    for k in range(n - 2):
        x = tri[k + 1:, k]
        tail_norm = float(np.linalg.norm(x[1:]))
        x_norm = float(np.hypot(x[0], tail_norm))
        if x_norm == 0.0:
            continue
        alpha = -np.copysign(x_norm, x[0])
        v = x.copy()
        v[0] -= alpha
        v /= np.linalg.norm(v)

        sub = tri[k + 1:, k + 1:]
        # sub <- (I - 2 v v^T) sub (I - 2 v v^T), v 已归一化
        p = sub @ v
        vtv = float(v @ p)
        w = p - vtv * v
        sub -= 2.0 * (np.outer(w, v) + np.outer(v, w))

        # Q <- Q (I - 2 v v^T), 作用于第 k+1 列之后
        q_tail = q_acc[:, k + 1:]
        qv = q_tail @ v
        q_tail -= 2.0 * np.outer(qv, v)

        tri[k, k + 1:] = 0.0
        tri[k + 1:, k] = 0.0
        tri[k, k + 1] = alpha
        tri[k + 1, k] = alpha

    diag = np.diag(tri).copy()
    offdiag = np.array([tri[i + 1, i] for i in range(n - 1)])
    return diag, offdiag, q_acc


def tridiagonal_ql(
    diag: np.ndarray,
    offdiag: np.ndarray,
    q_acc: np.ndarray,
    max_sweeps: int,
    eig_tol: float,
) -> tuple[np.ndarray, np.ndarray, int, bool, int | None]:
    """隐式 Wilkinson 移位 QL (tql2 型)。

    Returns:
        (d, z, sweeps, converged, stalled_index)
    """
    n = diag.shape[0]
    d = diag.astype(np.float64).copy()
    z = q_acc.astype(np.float64).copy()

    if n == 1:
        return d, z, 0, True, None

    f = np.zeros(n, dtype=np.float64)
    f[:n - 1] = offdiag

    sweeps = 0
    stalled: int | None = None

    for l in range(n):
        while True:
            # 从 l 向下寻找第一个可分离点: f[m] 相对可忽略
            m = l
            while m < n - 1 and abs(f[m]) > eig_tol * (abs(d[m]) + abs(d[m + 1])):
                m += 1
            if m == l:
                break  # d[l] 已收敛, deflate

            sweeps += 1
            if sweeps > max_sweeps:
                stalled = l
                return d, z, sweeps, False, stalled

            # Wilkinson 移位: 端部 2x2 块中靠近 d[l] 的特征值
            shift = (d[l + 1] - d[l]) / (2.0 * f[l])
            r0 = float(np.hypot(shift, 1.0))
            g = d[m] - d[l] + f[l] / (shift + np.copysign(r0, shift))
            s = 1.0
            c = 1.0
            p = 0.0
            recovered = False

            for i in range(m - 1, l - 1, -1):
                f_s = s * f[i]
                b = c * f[i]
                # tql2: r = hypot(f, g), g 为递推中的移位量
                r = float(np.hypot(f_s, g))
                f[i + 1] = r
                if r == 0.0:
                    # tql2 恢复步骤: 推回一步, 置零后从 l 重新扫描
                    d[i + 1] -= p
                    f[m] = 0.0
                    recovered = True
                    break
                s = f_s / r
                c = g / r
                g = d[i + 1] - p
                r = (d[i] - g) * s + 2.0 * c * b
                p = s * r
                d[i + 1] = g + p
                g = c * r - b
                # 特征向量同步 Givens 旋转 (列 i 与 i+1); 两列都先拷贝,
                # 避免视图在第一次赋值后被覆盖。
                z_i = z[:, i].copy()
                z_ip1 = z[:, i + 1].copy()
                z[:, i + 1] = s * z_i + c * z_ip1
                z[:, i] = c * z_i - s * z_ip1

            if recovered:
                continue
            d[l] -= p
            f[l] = g
            f[m] = 0.0

    return d, z, sweeps, True, None


def eigh_core(
    matrix: np.ndarray, *, max_sweeps: int, eig_tol: float
) -> KernelResult:
    """内核入口: 对称矩阵 -> (特征值, 特征向量) 与收敛证据。"""
    diag, offdiag, q_acc = householder_tridiagonal(matrix)
    d, z, sweeps, converged, stalled = tridiagonal_ql(
        diag, offdiag, q_acc, max_sweeps, eig_tol
    )

    if converged:
        order = np.argsort(d)
        d = d[order]
        z = z[:, order]

    return KernelResult(
        eigenvalues=d,
        eigenvectors=z,
        sweeps=sweeps,
        converged=converged,
        stalled_index=stalled,
        tridiagonal_offdiag=offdiag,
    )
