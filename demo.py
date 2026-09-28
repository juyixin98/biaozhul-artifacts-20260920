#!/usr/bin/env python3
"""本地演示脚本 —— 无需启动服务即可跑通全部验证案例与失败分类。

运行:
    python demo.py

内容:
1. 对角 / 重复谱 / 近退化 / 尺度悬殊 / 随机矩阵, 内核结果对照
   SciPy(LAPACK) 与 mpmath 高精度参考, 报告重构误差与子空间主角。
2. 失败分类演示: 非对称输入、迭代预算耗尽、质量门超阈值。
所有数据均为本地确定性合成夹具。
"""

from __future__ import annotations

import numpy as np

from eigenservice.config import EigenConfig
from eigenservice.errors import EigenserviceError
from eigenservice.evidence import compare_eigenbasis
from eigenservice.kernel import eigh_core
from eigenservice.reference import mpmath_reference_eigh, scipy_reference_eigh
from eigenservice.service import decompose
from eigenservice.trace import RequestLog
from tests.fixtures import (
    diagonal_matrix,
    mildly_asymmetric_matrix,
    near_degenerate_matrix,
    random_symmetric,
    repeated_spectrum_matrix,
    scale_disparity_matrix,
)

LINE = "=" * 72


def _spectrum(label: str, matrix: np.ndarray) -> None:
    n = matrix.shape[0]
    print(f"\n[{label}]  n={n}")
    result = eigh_core(matrix, max_sweeps=max(30, 12 * n), eig_tol=1e-14)
    sci = scipy_reference_eigh(matrix)
    mpr = mpmath_reference_eigh(matrix, precision_digits=80)
    comparison = compare_eigenbasis(
        result.eigenvalues, result.eigenvectors,
        mpr.eigenvalues, mpr.eigenvectors, gap_tol=1e-7,
    )
    reconstructed = (result.eigenvectors * result.eigenvalues) @ result.eigenvectors.T
    recon = np.linalg.norm(reconstructed - matrix, "fro") / np.linalg.norm(matrix)
    clusters = [(c["multiplicity"], f'{c["max_principal_angle_rad"]:.1e}')
                for c in comparison["clusters"] if c["multiplicity"] > 1]
    print(f"  收敛              : {result.converged} (QL 步数 {result.sweeps})")
    print(f"  特征值(内核)      : {np.array2string(result.eigenvalues, precision=6)}")
    print(f"  特征值(mpmath 80位): {np.array2string(mpr.eigenvalues, precision=6)}")
    print(f"  对 LAPACK 最大绝对误差 : {np.max(np.abs(result.eigenvalues - sci.eigenvalues)):.2e}")
    print(f"  对 mpmath 最大绝对误差 : {comparison['eigenvalue_max_abs_error']:.2e}")
    print(f"  重构相对误差      : {recon:.2e}")
    print(f"  重特征值簇(重数,主角): {clusters if clusters else '无'}")
    print(f"  最大子空间主角    : {comparison['max_principal_angle_rad']:.2e} rad")


def _failure_demo() -> None:
    print(f"\n{LINE}\n失败分类演示\n{LINE}")

    # 1) 非对称
    try:
        decompose(mildly_asymmetric_matrix(1e-5), EigenConfig(sym_tol=1e-9))
    except EigenserviceError as exc:
        print(f"\n[1] code={exc.code}")
        print(f"    {exc.message}")
        print(f"    相对不对称度={exc.details['relative_asymmetry']:.2e} "
              f"(容差 {exc.details['sym_tol']:.0e})")

    # 2) 迭代预算耗尽 —— 不能因为停止就报成功
    try:
        decompose(random_symmetric(6, seed=21),
                  EigenConfig(base_sweeps=0, sweep_multiplier=0))
    except EigenserviceError as exc:
        trace = exc.details.get("trace", {})
        print(f"\n[2] code={exc.code}")
        print(f"    {exc.message}")
        print(f"    卡住位置={exc.details['stalled_index']} "
              f"预算={exc.details['max_sweeps']}")
        print(f"    不确定结论单列: {[u['reason'] for u in trace['uncertainties']]}")

    # 3) 质量门超阈值
    try:
        decompose(random_symmetric(5, seed=33), EigenConfig(residual_tol=0.0))
    except EigenserviceError as exc:
        print(f"\n[3] code={exc.code}")
        print(f"    {exc.message}")
        print(f"    超标项: {list(exc.details['violations'].keys())}")


def _service_trace_demo() -> None:
    print(f"\n{LINE}\n服务成功路径 (含请求身份与关键步骤轨迹)\n{LINE}")
    log = RequestLog(request_id="demo-0001")
    result = decompose(repeated_spectrum_matrix().tolist(), log=log)
    print(f"request_id : {result.trace['request_id']}")
    print(f"version    : {result.trace['service_version']}")
    print(f"core       : {result.trace['core']}")
    print(f"特征值     : {[round(v, 6) for v in result.eigenvalues]}")
    print(f"残差(相对) : {result.quality['residual']['relative_fro']:.2e}")
    print(f"正交性偏差 : {result.quality['orthogonality']['deviation_fro']:.2e}")
    print("关键步骤:")
    for step in result.trace["steps"]:
        print(f"  +{step['elapsed_ms']:>8.3f} ms  {step['step']}  {step['detail']}")


def main() -> None:
    print(LINE)
    print("对称矩阵特征分解服务 —— 本地合成数据验证")
    print("内核: Householder 三对角化 + 隐式 Wilkinson 移位 QL")
    print(LINE)
    _spectrum("对角矩阵", diagonal_matrix())
    _spectrum("重复谱 (三重2.0/二重-1.5)", repeated_spectrum_matrix())
    _spectrum("近退化 (间隙 1e-10)", near_degenerate_matrix())
    _spectrum("尺度悬殊 (1e6/1/1e-6)", scale_disparity_matrix())
    _spectrum("随机稠密对称矩阵 (n=20)", random_symmetric(20, seed=77))
    _failure_demo()
    _service_trace_demo()
    print(f"\n{LINE}\n演示完成。\n")


if __name__ == "__main__":
    main()
