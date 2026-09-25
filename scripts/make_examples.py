"""生成 examples/ 下的 JSON 请求样例。

运行：``python scripts/make_examples.py``

样例清单：
01 一维拉普拉斯（良态 SPD，已知解）
02 病态缩放系统（Jacobi 预条件对比）
03 零右端、非零初始猜测
04 单位阵自定义容差与无预条件
05 非法 CSR（列下标越界）——预期 ok=false
06 不定矩阵（对称、正对角但非正定）——预期 negative_curvature
"""

from __future__ import annotations

import json
import sys
from pathlib import Path

import numpy as np

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from sparse_cg import CSRMatrix

EXAMPLES_DIR = Path(__file__).resolve().parents[1] / "examples"


def csr_field(dense: np.ndarray) -> dict:
    a = CSRMatrix.from_dense(dense)
    return {
        "n": int(a.n),
        "data": [float(v) for v in a.data],
        "indices": [int(v) for v in a.indices],
        "indptr": [int(v) for v in a.indptr],
    }


def laplacian(n: int) -> np.ndarray:
    return (
        2.0 * np.eye(n)
        - np.eye(n, k=1)
        - np.eye(n, k=-1)
    )


def write(name: str, payload: dict) -> None:
    path = EXAMPLES_DIR / name
    with open(path, "w", encoding="utf-8") as fh:
        json.dump(payload, fh, ensure_ascii=False, indent=2)
        fh.write("\n")
    print(f"写出 {path.relative_to(EXAMPLES_DIR.parent)}")


def main() -> None:
    EXAMPLES_DIR.mkdir(parents=True, exist_ok=True)
    rng = np.random.default_rng(42)

    # 01 良态：n=20 的一维拉普拉斯，已知解
    n1 = 20
    x1 = np.sin(np.linspace(0.0, np.pi, n1))
    A1 = laplacian(n1)
    write(
        "01_laplacian.json",
        {
            "matrix": csr_field(A1),
            "b": [float(v) for v in A1 @ x1],
            "rtol": 1e-10,
            "atol": 1e-12,
            "preconditioner": "jacobi",
        },
    )

    # 02 病态：对角相似变换缩放，条件数 ~ 1e8
    n2 = 60
    s = np.geomspace(1.0, 1e4, n2)
    A2 = (s[:, None] * laplacian(n2)) * s[None, :]
    x2 = rng.standard_normal(n2)
    write(
        "02_ill_conditioned.json",
        {
            "matrix": csr_field(A2),
            "b": [float(v) for v in A2 @ x2],
            "rtol": 1e-10,
            "preconditioner": "jacobi",
            "max_iter": 600,
        },
    )

    # 03 零右端 + 非零 x0：解应为零向量
    n3 = 15
    write(
        "03_zero_rhs.json",
        {
            "matrix": csr_field(laplacian(n3)),
            "b": [0.0] * n3,
            "x0": [1.0] * n3,
            "rtol": 1e-10,
            "atol": 1e-12,
        },
    )

    # 04 单位阵 + 无预条件
    n4 = 5
    write(
        "04_identity_no_precond.json",
        {
            "matrix": csr_field(np.eye(n4)),
            "b": [1.0, 2.0, 3.0, 4.0, 5.0],
            "preconditioner": "none",
            "rtol": 1e-12,
        },
    )

    # 05 非法 CSR：第 1 行的列下标 9 越界（n=3，合法列为 0,1,2）
    write(
        "05_invalid_csr.json",
        {
            "matrix": {
                "n": 3,
                "data": [4.0, 1.0, 1.0, 4.0, 1.0, 1.0, 4.0],
                "indices": [0, 9, 0, 1, 2, 1, 2],
                "indptr": [0, 2, 5, 7],
            },
            "b": [1.0, 1.0, 1.0],
        },
    )

    # 06 不定矩阵 [[1,2],[2,1]]：对称、对角为正，但特征值 {3,-1}
    write(
        "06_indefinite.json",
        {
            "matrix": {
                "n": 2,
                "data": [1.0, 2.0, 2.0, 1.0],
                "indices": [0, 1, 0, 1],
                "indptr": [0, 2, 4],
            },
            "b": [1.0, 0.0],
            "preconditioner": "none",
        },
    )


if __name__ == "__main__":
    main()
