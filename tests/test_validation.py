"""输入校验测试：非法旋转、非正定/不对称协方差、证据路径。"""

import numpy as np

from app.validation import (
    validate_covariance,
    validate_cross_covariance,
    validate_rotation,
)


def test_valid_identity_and_quaternion():
    R, issues = validate_rotation(np.eye(3), "edge:x")
    assert R is not None and issues == []
    R2, issues2 = validate_rotation([1, 0, 0, 0], "edge:y")
    assert R2 is not None and issues2 == []
    assert np.allclose(R2, np.eye(3))


def test_reflection_is_illegal():
    M = np.diag([1.0, 1.0, -1.0])  # det=-1，反射
    R, issues = validate_rotation(M, "edge:ref")
    assert R is None and len(issues) >= 1
    assert all(i["code"] == "ILLEGAL_ROTATION" for i in issues)
    assert issues[0]["evidence_path"] == ["edge:ref"]


def test_non_orthogonal_is_illegal():
    M = np.eye(3) * 1.5
    R, issues = validate_rotation(M, "edge:scale")
    assert R is None
    codes = {i["code"] for i in issues}
    assert "ILLEGAL_ROTATION" in codes


def test_non_finite_rotation():
    M = np.eye(3)
    M[0, 0] = np.nan
    R, issues = validate_rotation(M, "edge:nan")
    assert R is None and issues[0]["code"] == "ILLEGAL_ROTATION"


def test_unnormalized_quaternion():
    R, issues = validate_rotation([2, 0, 0, 0], "edge:q")
    assert R is None and issues[0]["code"] == "ILLEGAL_ROTATION"
    assert "归一化" in issues[0]["message"]


def test_wrong_shape():
    R, issues = validate_rotation(np.eye(2), "edge:shape")
    assert R is None and issues[0]["code"] == "MALFORMED_TRANSFORM"


def test_positive_covariance_passes():
    C = np.diag([1e-6] * 6)
    out, issues = validate_covariance(C, "edge:c")
    assert out is not None and issues == []


def test_negative_eigenvalue_rejected():
    C = np.diag([1e-6] * 6)
    C[0, 0] = -1e-5
    out, issues = validate_covariance(C, "edge:badcov")
    assert out is None
    assert issues[0]["code"] == "COVARIANCE_NOT_PSD"
    assert issues[0]["details"]["eigenvalue_min"] < -1e-10
    assert "worst_eigenvector" in issues[0]["details"]
    assert issues[0]["evidence_path"] == ["edge:badcov"]


def test_nonsymmetric_covariance_rejected():
    C = np.eye(6)
    C[0, 1] = 1e-3  # 不对称
    out, issues = validate_covariance(C, "edge:asym")
    assert out is None and issues[0]["code"] == "COVARIANCE_NOT_SYMMETRIC"


def test_wrong_shape_covariance():
    out, issues = validate_covariance(np.eye(5), "edge:s5")
    assert out is None and issues[0]["code"] == "MALFORMED_COVARIANCE"


def test_tiny_negative_eigenvalue_clipped():
    # -1e-13 属数值舍入范围：钳为 0 并放行（半正定）
    C = np.eye(6) * 1e-6
    C[0, 0] = -1e-13
    out, issues = validate_covariance(C, "edge:tiny")
    assert out is not None and issues == []
    assert np.linalg.eigvalsh(out).min() >= 0


def test_singular_covariance_allowed():
    # 物理上允许某方向零不确定度（半正定）
    C = np.diag([1e-6, 1e-6, 1e-6, 0.0, 0.0, 0.0])
    out, issues = validate_covariance(C, "edge:singular")
    assert out is not None and issues == []


def test_missing_covariance_allowed():
    out, issues = validate_covariance(None, "edge:miss")
    assert out is None and issues == []


def test_cross_covariance_shape():
    out, issues = validate_cross_covariance(np.zeros((5, 5)), "a", "b")
    assert out is None and issues[0]["code"] == "MALFORMED_COVARIANCE"
    out2, issues2 = validate_cross_covariance(np.zeros((6, 6)), "a", "b")
    assert out2 is not None and issues2 == []
