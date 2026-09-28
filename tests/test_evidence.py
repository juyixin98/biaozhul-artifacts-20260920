"""证据模块测试: 残差、正交性、重特征值子空间比较。"""

from __future__ import annotations

import numpy as np
import pytest

from eigenservice.evidence import (
    cluster_eigenvalues,
    compare_eigenbasis,
    orthogonality_evidence,
    residual_evidence,
    subspace_principal_angles,
)


@pytest.mark.unit
def test_residual_zero_for_exact_eigenpairs() -> None:
    matrix = np.diag([1.0, 2.0, 3.0])
    vecs = np.eye(3)
    vals = np.array([1.0, 2.0, 3.0])
    ev = residual_evidence(matrix, vals, vecs)
    assert ev.absolute_fro == 0.0
    assert ev.max_per_pair_relative == 0.0


@pytest.mark.unit
def test_residual_detects_bad_vector() -> None:
    matrix = np.diag([1.0, 2.0])
    # 第二列用正确向量 e2, 但谎报特征值为 -2 (真值 2): 残差必然显著
    vals = np.array([1.0, -2.0])
    vecs = np.eye(2)
    ev = residual_evidence(matrix, vals, vecs)
    assert ev.per_pair_relative[0] == 0.0
    assert ev.per_pair_relative[1] > 1.0


@pytest.mark.unit
def test_orthogonality_evidence_flags_nonorthogonal() -> None:
    good = np.eye(3)
    assert orthogonality_evidence(good).deviation_fro == 0.0
    bad = np.array([[1.0, 1.0], [0.0, 1.0]])
    assert orthogonality_evidence(bad).max_abs_deviation == 1.0


@pytest.mark.unit
def test_cluster_groups_repeated_and_near_degenerate() -> None:
    vals = np.array([1.0, 1.0, 1.0, 4.0, 4.0 + 1e-11, 9.0])
    clusters = cluster_eigenvalues(vals, gap_tol=1e-7)
    # 尺度 9, gap 阈值 ~9e-7: [1,1,1], [4, 4+1e-11], [9]
    assert clusters == [(0, 1, 2), (3, 4), (5,)]


@pytest.mark.unit
def test_cluster_separates_distinct_spectrum() -> None:
    vals = np.array([1.0, 3.0, 5.0])
    assert cluster_eigenvalues(vals, gap_tol=1e-7) == [(0,), (1,), (2,)]


@pytest.mark.unit
def test_cluster_empty_spectrum() -> None:
    assert cluster_eigenvalues(np.array([]), gap_tol=1e-7) == []


@pytest.mark.unit
def test_principal_angles_same_subspace_zero() -> None:
    rng = np.random.default_rng(0)
    base, _ = np.linalg.qr(rng.standard_normal((5, 3)))
    # 同一子空间内的任意旋转, 主角应全为 0
    rot, _ = np.linalg.qr(rng.standard_normal((3, 3)))
    angles = subspace_principal_angles(base, base @ rot)
    # arccos(1) 附近 1-ULP 会放大到 ~1e-8, 用 1e-6 容差
    assert np.max(angles) < 1e-6


@pytest.mark.unit
def test_principal_angles_detect_rotated_subspace() -> None:
    # 二维平面内旋转 30 度, 一维子空间偏差可测
    theta = np.pi / 6
    a = np.array([[1.0], [0.0]])
    b = np.array([[np.cos(theta)], [np.sin(theta)]])
    angles = subspace_principal_angles(a, b)
    assert abs(float(angles[0]) - theta) < 1e-12


@pytest.mark.unit
def test_compare_eigenbasis_uses_subspace_for_repeated_roots() -> None:
    """重特征值: 逐向量比较会失败, 子空间比较必须通过。"""
    # 参考: 二重特征值 2, 标准基; 被测: 同一子空间内旋转 45 度
    ref_vals = np.array([2.0, 2.0, 5.0])
    ref_vecs = np.eye(3)
    rot45 = np.array([
        [np.cos(np.pi / 4), -np.sin(np.pi / 4), 0.0],
        [np.sin(np.pi / 4), np.cos(np.pi / 4), 0.0],
        [0.0, 0.0, 1.0],
    ])
    test_vecs = ref_vecs @ rot45
    report = compare_eigenbasis(ref_vals, test_vecs, ref_vals, ref_vecs,
                                gap_tol=1e-7)
    assert report["eigenvalue_max_abs_error"] == 0.0
    assert report["max_principal_angle_rad"] < 1e-12
    multiplicity_cluster = [c for c in report["clusters"] if c["multiplicity"] == 2]
    assert len(multiplicity_cluster) == 1

    # 反证: 若把旋转后的列错误地逐向量比较, 内积仅 ~0.707
    assert abs(float(test_vecs[:, 0] @ ref_vecs[:, 0])) < 0.71
