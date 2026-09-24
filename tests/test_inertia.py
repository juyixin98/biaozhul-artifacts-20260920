"""惯性检查测试:零质量、负定矩阵、三角不等式、浮点容差、量级异常。

边界矩阵夹具:
  * BOUNDARY_TRIANGLE_OK   — izz 恰好等于 ixx+iyy(边界,应通过)
  * BOUNDARY_TRIANGLE_BAD  — izz 超出 ixx+iyy 一个微小量(应失败)
  * BOUNDARY_PSD_OK        — 最小特征值 -1e-12,在默认容差内(应通过)
  * BOUNDARY_PSD_BAD       — 最小特征值 -1e-6,超出容差(应失败)
"""

from urdf_check import CheckConfig, check_urdf

from .conftest import inertial_xml, link_xml, make_urdf

BOUNDARY_TRIANGLE_OK = dict(ixx="1.0", iyy="1.0", izz="2.0")
BOUNDARY_TRIANGLE_BAD = dict(ixx="1.0", iyy="1.0", izz="2.00000001")
BOUNDARY_PSD_OK = dict(ixx="1.0", iyy="1.0", izz="-1e-12")
BOUNDARY_PSD_BAD = dict(ixx="1.0", iyy="1.0", izz="-1e-6")


def codes(report):
    return {d.code for d in report.diagnostics}


def errors(report):
    return {d.code for d in report.errors}


def warnings(report):
    return {d.code for d in report.warnings}


def single_link_urdf(**inertia_kwargs) -> bytes:
    mass = inertia_kwargs.pop("mass", "1.0")
    return make_urdf(link_xml("only", inertial_xml(mass=mass, **inertia_kwargs)))


# ---------- 缺失与非法分开 ----------

def test_missing_inertial_is_warning_not_error():
    report = check_urdf(make_urdf(link_xml("bare")))
    assert report.ok  # 缺失惯性只告警
    assert "MISSING_INERTIAL" in warnings(report)
    assert "MISSING_INERTIAL" not in errors(report)


def test_invalid_numeric_is_error_and_distinct():
    report = check_urdf(single_link_urdf(ixx="not_a_number"))
    assert not report.ok
    assert "INVALID_NUMERIC" in errors(report)
    # 与缺失惯性是不同类别
    assert "MISSING_INERTIAL" not in codes(report)
    diag = next(d for d in report.errors if d.code == "INVALID_NUMERIC")
    assert diag.attribute == "ixx"
    assert diag.node and "inertia" in diag.node


def test_nan_mass_rejected():
    report = check_urdf(single_link_urdf(mass="nan"))
    assert "INVALID_NUMERIC" in errors(report)


def test_inf_inertia_rejected():
    report = check_urdf(single_link_urdf(izz="inf"))
    assert "INVALID_NUMERIC" in errors(report)


# ---------- 质量 ----------

def test_zero_mass_is_error():
    report = check_urdf(single_link_urdf(mass="0.0"))
    assert "MASS_ZERO" in errors(report)
    diag = next(d for d in report.errors if d.code == "MASS_ZERO")
    assert diag.attribute == "value"
    assert diag.line is not None


def test_negative_mass_is_error():
    report = check_urdf(single_link_urdf(mass="-2.5"))
    assert "MASS_NEGATIVE" in errors(report)


# ---------- 正定性(浮点容差) ----------

def test_psd_boundary_within_tolerance():
    # 最小特征值 -1e-12,默认 psd_atol=1e-9 → 通过(仅边界告警)
    report = check_urdf(single_link_urdf(**BOUNDARY_PSD_OK))
    assert "INERTIA_NOT_POSITIVE_DEFINITE" not in errors(report)
    assert "INERTIA_NEAR_SINGULAR" in warnings(report)


def test_psd_boundary_beyond_tolerance():
    # 最小特征值 -1e-6,远超容差 → 错误
    report = check_urdf(single_link_urdf(**BOUNDARY_PSD_BAD))
    assert "INERTIA_NOT_POSITIVE_DEFINITE" in errors(report)


def test_psd_tolerance_configurable():
    src = single_link_urdf(**BOUNDARY_PSD_BAD)
    strict = check_urdf(src, CheckConfig(psd_atol=1e-3))
    # 容差放宽到 1e-3 后,-1e-6 在容差内 → 不再报非正定
    assert "INERTIA_NOT_POSITIVE_DEFINITE" not in errors(strict)


def test_indefinite_matrix_with_offdiagonal():
    # ixy=2 使矩阵不定: [[0.01, 2], [2, 0.01]] 特征值约 ±2
    report = check_urdf(single_link_urdf(ixy="2.0"))
    assert "INERTIA_NOT_POSITIVE_DEFINITE" in errors(report)


# ---------- 三角不等式(边界矩阵) ----------

def test_triangle_boundary_exact_passes():
    # izz == ixx + iyy 恰好取等(薄板边界),应通过
    report = check_urdf(single_link_urdf(**BOUNDARY_TRIANGLE_OK))
    assert "INERTIA_TRIANGLE_VIOLATION" not in errors(report)


def test_triangle_boundary_slightly_over_fails():
    # izz 超出 ixx+iyy 共 1e-8,超过默认相对容差 → 失败
    report = check_urdf(single_link_urdf(**BOUNDARY_TRIANGLE_BAD))
    assert "INERTIA_TRIANGLE_VIOLATION" in errors(report)
    diag = next(
        d for d in report.errors if d.code == "INERTIA_TRIANGLE_VIOLATION"
    )
    assert diag.attribute == "izz"


def test_triangle_violation_large():
    report = check_urdf(single_link_urdf(ixx="0.001", iyy="0.001", izz="1.0"))
    assert "INERTIA_TRIANGLE_VIOLATION" in errors(report)


# ---------- 单位量级异常 ----------

def test_mass_magnitude_warning():
    report = check_urdf(single_link_urdf(mass="500000.0"))
    assert report.ok  # 量级异常只告警
    assert "MASS_MAGNITUDE_UNUSUAL" in warnings(report)


def test_inertia_magnitude_warning():
    report = check_urdf(
        single_link_urdf(ixx="50000.0", iyy="50000.0", izz="90000.0")
    )
    assert "INERTIA_MAGNITUDE_UNUSUAL" in warnings(report)


def test_tiny_inertia_magnitude_warning():
    report = check_urdf(
        single_link_urdf(ixx="1e-14", iyy="1e-14", izz="1e-14")
    )
    assert "INERTIA_MAGNITUDE_UNUSUAL" in warnings(report)


# ---------- 定位信息 ----------

def test_diagnostic_has_node_and_line():
    src = make_urdf(
        link_xml("only", inertial_xml(mass="0.0"))
    )
    report = check_urdf(src)
    diag = next(d for d in report.errors if d.code == "MASS_ZERO")
    assert diag.node.startswith("/robot/link")
    assert "mass" in diag.node
    assert diag.line and diag.line >= 3
