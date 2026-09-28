"""独立参考对照测试。

参考答案来自 SciPy LAPACK 与 mpmath 高精度路径 —— 均不经过被测内核。
夹具矩阵通过显式谱构造, 真值已知。断言具体数值而非"接口可调用"。
"""

from __future__ import annotations

import numpy as np
import pytest

from eigenservice.evidence import (
    compare_eigenbasis,
    orthogonality_evidence,
    reconstruction_evidence,
    residual_evidence,
)
from eigenservice.kernel import eigh_core
from eigenservice.reference import mpmath_reference_eigh, scipy_reference_eigh

from .fixtures import (
    diagonal_matrix,
    near_degenerate_matrix,
    random_symmetric,
    repeated_spectrum_matrix,
    scale_disparity_matrix,
    with_spectrum,
)

SUBSPACE_ANGLE_TOL = 1e-6  # arccos 在 1 附近的机器精度放大


def _run(matrix: np.ndarray):
    n = matrix.shape[0]
    return eigh_core(matrix, max_sweeps=max(30, 12 * n), eig_tol=1e-14)


@pytest.mark.integration
@pytest.mark.parametrize("n", [4, 8, 16])
def test_kernel_agrees_with_both_references_random(n: int) -> None:
    matrix = random_symmetric(n, seed=900 + n)
    result = _run(matrix)
    sci = scipy_reference_eigh(matrix)
    mpr = mpmath_reference_eigh(matrix, precision_digits=60)

    assert result.converged is True
    np.testing.assert_allclose(result.eigenvalues, sci.eigenvalues,
                               atol=1e-9, rtol=1e-9)
    np.testing.assert_allclose(result.eigenvalues, mpr.eigenvalues,
                               atol=1e-8, rtol=1e-8)
    report = compare_eigenbasis(
        result.eigenvalues, result.eigenvectors,
        sci.eigenvalues, sci.eigenvectors, gap_tol=1e-7,
    )
    assert report["max_principal_angle_rad"] < SUBSPACE_ANGLE_TOL


@pytest.mark.integration
def test_diagonal_case() -> None:
    matrix = diagonal_matrix()
    result = _run(matrix)
    mpr = mpmath_reference_eigh(matrix, 60)
    expected = np.array([-3.0, -1.0, 0.5, 2.0, 7.0])
    np.testing.assert_allclose(result.eigenvalues, expected, atol=1e-12)
    np.testing.assert_allclose(mpr.eigenvalues, expected, atol=1e-12)
    # 对角阵的特征向量必须就是标准基 (列置换意义下)
    assert np.linalg.norm(np.abs(result.eigenvectors) - np.eye(5)) < 1e-12


@pytest.mark.integration
def test_repeated_spectrum_subspace_vs_references() -> None:
    matrix = repeated_spectrum_matrix()
    result = _run(matrix)
    sci = scipy_reference_eigh(matrix)
    mpr = mpmath_reference_eigh(matrix, 60)

    expected = np.array([-1.5, -1.5, 2.0, 2.0, 2.0])
    np.testing.assert_allclose(result.eigenvalues, expected, atol=1e-11)
    np.testing.assert_allclose(mpr.eigenvalues, expected, atol=1e-20)

    report = compare_eigenbasis(
        result.eigenvalues, result.eigenvectors,
        mpr.eigenvalues, mpr.eigenvectors, gap_tol=1e-7,
    )
    multiplicities = sorted(c["multiplicity"] for c in report["clusters"])
    assert multiplicities == [2, 3]
    assert report["max_principal_angle_rad"] < SUBSPACE_ANGLE_TOL
    assert report["eigenvalue_max_abs_error"] < 1e-12

    # 重构与残差同样达标
    rec = reconstruction_evidence(
        matrix, result.eigenvalues, result.eigenvectors
    )
    assert rec.relative_fro < 1e-12


@pytest.mark.integration
def test_near_degenerate_resolves_cluster_against_mpmath() -> None:
    matrix = near_degenerate_matrix()
    result = _run(matrix)
    mpr = mpmath_reference_eigh(matrix, precision_digits=80)
    sci = scipy_reference_eigh(matrix)

    # 双精度对 1e-10 间隙只能分辨簇; 但特征值整体仍应对齐高精度真值
    assert result.converged is True
    np.testing.assert_allclose(result.eigenvalues, mpr.eigenvalues, atol=1e-8)
    np.testing.assert_allclose(sci.eigenvalues, mpr.eigenvalues, atol=1e-8)

    report = compare_eigenbasis(
        result.eigenvalues, result.eigenvectors,
        mpr.eigenvalues, mpr.eigenvectors, gap_tol=1e-7,
    )
    # 近退化三向量必须作为一个子空间一致
    near_cluster = max(report["clusters"], key=lambda c: c["multiplicity"])
    assert near_cluster["multiplicity"] == 3
    assert near_cluster["max_principal_angle_rad"] < 1e-4
    # 孤立特征值必须精确
    np.testing.assert_allclose(result.eigenvalues[0], -5.0, atol=1e-10)
    np.testing.assert_allclose(result.eigenvalues[-1], 4.0, atol=1e-10)


@pytest.mark.integration
def test_scale_disparity_residual_and_reference() -> None:
    matrix = scale_disparity_matrix()
    result = _run(matrix)
    mpr = mpmath_reference_eigh(matrix, precision_digits=80)

    assert result.converged is True
    # 小特征值受双精度限制有绝对误差, 但大特征值必须精确
    assert abs(result.eigenvalues[-1] - 1e6) < 1e-7
    # 逐对残差才是尺度悬殊场景下有意义的判据
    res = residual_evidence(
        matrix, result.eigenvalues, result.eigenvectors
    )
    assert res.relative_fro < 1e-12
    # 高精度参考的小特征值 ~1e-6, 双精度结果应在其合理误差带内
    assert abs(result.eigenvalues[0] - mpr.eigenvalues[0]) < 1e-8
    assert orthogonality_evidence(
        result.eigenvectors
    ).deviation_fro < 1e-10


@pytest.mark.integration
def test_constructed_spectrum_is_independent_truth() -> None:
    # 真值来自构造参数, 而非被测实现
    spectrum = [-7.0, -2.0, 0.0, 0.0, 3.0, 8.0]
    matrix = with_spectrum(spectrum, seed=2024)
    result = _run(matrix)
    np.testing.assert_allclose(result.eigenvalues, sorted(spectrum), atol=1e-10)
