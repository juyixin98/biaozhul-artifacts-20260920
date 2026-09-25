"""验收演示脚本：用已知解生成稀疏系统并报告真实残差。

运行：``python scripts/acceptance_demo.py``

覆盖：
  A. 良态一维拉普拉斯（n=200，Jacobi 与无预条件对比）
  B. 病态缩放系统（条件数 ~1e8，真实残差/解误差如实报告）
  C. 零右端、非零初始猜测（唯一解为零向量）
  D. 不定矩阵（非正曲率诊断）
  E. 非法 CSR（错误码）

每例都独立重算 ||b - A x||（不只引用求解器自己报告的迭代次数）。
"""

from __future__ import annotations

import sys
from pathlib import Path

import numpy as np

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from sparse_cg import CSRMatrix, solve_request
from sparse_cg.pcg import pcg


def laplacian_dense(n: int) -> np.ndarray:
    return 2.0 * np.eye(n) - np.eye(n, k=1) - np.eye(n, k=-1)


def banner(title: str) -> None:
    print(f"\n{'=' * 70}\n{title}\n{'=' * 70}")


def report(name: str, a: CSRMatrix, b: np.ndarray, x_exact: np.ndarray | None,
           **opts) -> None:
    res = pcg(a, b, **opts)
    true_r = float(np.linalg.norm(b - a.matvec(res.x)))
    bnorm = float(np.linalg.norm(b))
    rel = true_r / bnorm if bnorm > 0 else true_r
    print(f"[{name}]")
    print(f"  status         : {res.status}")
    print(f"  iterations     : {res.iterations}")
    print(f"  ||b - Ax||_2   : {true_r:.6e}  (独立重算)")
    print(f"  相对残差       : {rel:.6e}")
    if x_exact is not None:
        err = float(np.linalg.norm(res.x - x_exact))
        print(f"  ||x - x*||_2  : {err:.6e}  (与已知解比较)")
    print(f"  求解器自报残差 : {res.residual_norm:.6e}  "
          f"{'一致' if np.isclose(true_r, res.residual_norm, rtol=1e-8) else '不一致!'}")
    print(f"  message        : {res.message[:100]}")


def main() -> int:
    rng = np.random.default_rng(2026)

    # ---- A. 良态拉普拉斯 --------------------------------------------------
    banner("A. 良态一维拉普拉斯 n=200（已知解）")
    n = 200
    a = CSRMatrix.from_dense(laplacian_dense(n))
    x_exact = rng.standard_normal(n)
    b = a.matvec(x_exact)
    report("Jacobi 预条件", a, b, x_exact, rtol=1e-10, atol=1e-12)
    report("无预条件", a, b, x_exact, rtol=1e-10, atol=1e-12,
           preconditioner="none")

    # ---- B. 病态系统（条件数 ~1e8） ---------------------------------------
    banner("B. 病态缩放系统 n=60（估计条件数 ~1e8）")
    n2 = 60
    s = np.geomspace(1.0, 1e4, n2)
    base = laplacian_dense(n2)
    dense_b = (s[:, None] * base) * s[None, :]
    eig = np.linalg.eigvalsh(dense_b)
    print(f"  稠密特征值估计条件数: {eig[-1] / eig[0]:.3e}")
    a2 = CSRMatrix.from_dense(dense_b)
    x2 = rng.standard_normal(n2)
    b2 = a2.matvec(x2)
    report("rtol=1e-12（可达：相对残差 ~1e-14）", a2, b2, x2, rtol=1e-12,
           max_iter=600)
    report("rtol=atol=0（数学不可达，演示停滞/迭代上限诊断）", a2, b2, x2,
           rtol=0.0, atol=0.0, max_iter=300, restart_period=30,
           stagnation_patience=3, stagnation_factor=0.9)

    # ---- C. 零右端 --------------------------------------------------------
    banner("C. 零右端 b=0、非零 x0（唯一解为零向量）")
    a3 = CSRMatrix.from_dense(laplacian_dense(40))
    x0 = rng.standard_normal(40)
    report("b=0", a3, np.zeros(40), np.zeros(40), x0=x0, rtol=1e-10)

    # ---- D. 不定矩阵 ------------------------------------------------------
    banner("D. 对称但不定矩阵 [[1,2],[2,1]]（特征值 3,-1）")
    a4 = CSRMatrix(
        np.array([1.0, 2.0, 2.0, 1.0]),
        np.array([0, 1, 0, 1]),
        np.array([0, 2, 4]),
        2,
    )
    report("非正曲率诊断", a4, np.array([1.0, 0.0]), None,
           preconditioner="none")

    # ---- E. 非法 CSR（走 JSON API 错误通道） ------------------------------
    banner("E. 非法 CSR：列下标越界")
    resp = solve_request({
        "matrix": {"n": 3,
                   "data": [4.0, 1.0, 1.0, 4.0, 1.0, 1.0, 4.0],
                   "indices": [0, 9, 0, 1, 2, 1, 2],
                   "indptr": [0, 2, 5, 7]},
        "b": [1.0, 1.0, 1.0],
    })
    print(f"  ok    : {resp['ok']}")
    print(f"  code  : {resp['error']['code']}")
    print(f"  msg   : {resp['error']['message']}")

    banner("完成")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
