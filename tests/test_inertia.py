"""惯性检查：缺少项 vs 数值非法、零质量、正定、三角不等式、容差、量级。"""

import numpy as np

from urdf_check.checker import DEFAULT_TOL
from urdf_check.issues import Code, Severity

from conftest import check, find_issue, has, inertia_block, single_link_urdf


def inertia_xml(**kw):
    return single_link_urdf(body=inertia_block(**kw))


# --- 「缺少」与「数值非法」严格分开 ---

def test_missing_inertial_is_distinct_from_bad_number():
    report = check(inertia_xml(inertial=False))
    assert has(report, Code.INERTIAL_MISSING)
    assert not has(report, Code.MASS_INVALID_NUMERIC)
    assert not has(report, Code.INERTIA_INVALID_NUMERIC)


def test_missing_mass_tag_vs_missing_value_attr():
    assert has(check(inertia_xml(mass_tag=False)), Code.MASS_MISSING)
    xml = """<?xml version="1.0"?>
<robot name="r"><link name="L"><inertial>
  <mass/>
  <inertia ixx="1" ixy="0" ixz="0" iyy="1" iyz="0" izz="1"/>
</inertial></link></robot>
"""
    assert has(check(xml), Code.MASS_VALUE_MISSING)


def test_missing_inertia_tag_and_missing_component_attr():
    assert has(check(inertia_xml(inertia_tag=False)), Code.INERTIA_MISSING)
    xml = """<?xml version="1.0"?>
<robot name="r"><link name="L"><inertial>
  <mass value="1"/>
  <inertia ixx="1" ixy="0" ixz="0" iyy="1" iyz="0"/>
</inertial></link></robot>
"""
    report = check(xml)
    assert has(report, Code.INERTIA_ATTR_MISSING)
    assert find_issue(report, Code.INERTIA_ATTR_MISSING).attribute == "izz"


def test_bad_numeric_is_invalid_not_missing():
    report = check(inertia_xml(mass="abc"))
    assert has(report, Code.MASS_INVALID_NUMERIC)
    assert not has(report, Code.MASS_MISSING)
    issue = find_issue(report, Code.MASS_INVALID_NUMERIC)
    assert issue.attribute == "value"


def test_mass_nan_inf_and_garbage():
    for val in ("nan", "inf", "-inf", "1.2.3"):
        report = check(inertia_xml(mass=val))
        assert has(report, Code.MASS_INVALID_NUMERIC), val


def test_inertia_component_bad_numeric():
    report = check(inertia_xml(ixx="nan"))
    assert has(report, Code.INERTIA_INVALID_NUMERIC)
    assert find_issue(report, Code.INERTIA_INVALID_NUMERIC).attribute == "ixx"


# --- 质量为零 / 为负（独立问题码） ---

def test_zero_mass_is_its_own_code():
    report = check(inertia_xml(mass="0.0", ixx="0.01", iyy="0.01", izz="0.01"))
    assert has(report, Code.MASS_ZERO)
    assert not has(report, Code.MASS_INVALID_NUMERIC)


def test_negative_mass():
    report = check(inertia_xml(mass="-2", ixx="1", iyy="1", izz="1"))
    assert has(report, Code.MASS_NEGATIVE)


# --- 正定（边界矩阵 + 浮点容差） ---

def test_diag_spd_passes():
    assert check(inertia_xml(ixx="2", iyy="3", izz="4")).ok


def test_negative_eigenvalue_not_spd():
    # 对角有负分量，最小特征值 < 0，超出容差。
    report = check(inertia_xml(mass="1", ixx="-1", iyy="2", izz="3"))
    assert has(report, Code.INERTIA_NOT_POSITIVE_DEFINITE)


def test_rotated_matrix_uses_eigenvalues():
    # 非对角旋转后的正定矩阵：原始 ixx/iyy 看不出正负，必须用特征值。
    angle = 0.6
    c, s = np.cos(angle), np.sin(angle)
    R = np.array([[c, -s, 0], [s, c, 0], [0, 0, 1]])
    D = np.diag([1.0, 2.0, 2.5])
    I = R @ D @ R.T
    xml = inertia_xml(
        mass="3", ixx=f"{I[0,0]:.15f}", ixy=f"{I[0,1]:.15f}",
        ixz="0", iyy=f"{I[1,1]:.15f}", iyz="0", izz=f"{I[2,2]:.15f}")
    report = check(xml)
    assert not has(report, Code.INERTIA_NOT_POSITIVE_DEFINITE), report.to_dict()


def test_indefinite_via_large_offdiagonal():
    # diag(1,1) 但 ixy=2 -> 特征值 3 与 -1：不定矩阵。
    report = check(inertia_xml(mass="1", ixx="1", ixy="2", iyy="1", izz="3"))
    assert has(report, Code.INERTIA_NOT_POSITIVE_DEFINITE)


def test_zero_eigenvalue_is_boundary_warning_not_error():
    # diag(1,1,0)：半正定边界（理想细杆）。不判 error，给近奇异告警。
    report = check(inertia_xml(mass="1", ixx="1", iyy="1", izz="0"))
    assert not has(report, Code.INERTIA_NOT_POSITIVE_DEFINITE)
    assert has(report, Code.INERTIA_NEAR_SEMIDEFINITE)


def test_small_negative_within_tolerance_is_warning():
    # 最小特征量 -1e-12，相对主惯量 1 的比例小于 1e-9 容差：视为边界，告警。
    report = check(inertia_xml(mass="1", ixx="1", iyy="1",
                               izz=f"-{1e-12}"))
    assert not has(report, Code.INERTIA_NOT_POSITIVE_DEFINITE)
    assert has(report, Code.INERTIA_NEAR_SEMIDEFINITE)


def test_semidefinite_warning_threshold():
    # i_min/i_max = 1e-8 < 1e-6：触发近半正定告警（细杆状）。
    report = check(inertia_xml(mass="1", ixx="1", iyy="1", izz="1e-8"))
    assert has(report, Code.INERTIA_NEAR_SEMIDEFINITE)
    # 比值 1e-7 仍在阈值 1e-6 之内，告警。
    report2 = check(inertia_xml(mass="1", ixx="1", iyy="1", izz="1e-7"))
    assert has(report2, Code.INERTIA_NEAR_SEMIDEFINITE)
    # 比值 1e-5 超过告警阈值，为正常细长刚体，不告警。
    report3 = check(inertia_xml(mass="1", ixx="1", iyy="1", izz="1e-5"))
    assert not has(report3, Code.INERTIA_NEAR_SEMIDEFINITE)


# --- 三角不等式（边界矩阵两套） ---

def test_triangle_equality_boundary_passes():
    # diag(3,2,1)：1+2=3，等号合法。
    report = check(inertia_xml(mass="1", ixx="3", iyy="2", izz="1"))
    assert not has(report, Code.INERTIA_TRIANGLE_VIOLATION), report.to_dict()


def test_triangle_violation_diag_6_2_1():
    report = check(inertia_xml(mass="1", ixx="6", iyy="2", izz="1"))
    assert has(report, Code.INERTIA_TRIANGLE_VIOLATION)
    issue = find_issue(report, Code.INERTIA_TRIANGLE_VIOLATION)
    assert issue.node.endswith("inertia") or issue.node.rstrip("/").endswith("inertia")


def test_triangle_checked_on_eigenvalues_not_diagonals():
    # 旋转后的矩阵：对角线看似不违反，主转动惯量（特征值）才是判据。
    # 构造主惯量 (6,2,1)（违反三角），再整体旋转，对角线全部混合。
    L = np.diag([6.0, 2.0, 1.0])
    R = np.array([
        [0.6, -0.8, 0.0],
        [0.8, 0.6, 0.0],
        [0.0, 0.0, 1.0],
    ])
    I = R @ L @ R.T
    xml = inertia_xml(
        mass="2", ixx=f"{I[0,0]:.15f}", ixy=f"{I[0,1]:.15f}",
        ixz="0", iyy=f"{I[1,1]:.15f}", iyz="0", izz=f"{I[2,2]:.15f}")
    report = check(xml)
    assert has(report, Code.INERTIA_TRIANGLE_VIOLATION), report.to_dict()


def test_triangle_violation_tolerance_boundary():
    # 恰好越过容差一点点：6.00000002 vs 2+1=3（主惯量），应报错。
    report = check(inertia_xml(mass="1", ixx="6.00000002", iyy="2", izz="1"))
    assert has(report, Code.INERTIA_TRIANGLE_VIOLATION)
    # 恰好在容差内的小越界：3 + 3*1e-12 < 容差 3*1e-9，不报错。
    near = check(inertia_xml(mass="1", ixx="3.000000000003", iyy="2", izz="1"))
    assert not has(near, Code.INERTIA_TRIANGLE_VIOLATION)


# --- 单位 / 量级异常（告警，不阻断） ---

def test_scale_anomaly_too_small_radius_warns():
    # 质量 1 kg 但惯量 1e-12 -> 回转半径 ~5.8e-7 m，疑似 g·m² / mm 未换算。
    report = check(inertia_xml(mass="1", ixx="1e-12", iyy="1e-12",
                               izz="1e-12"))
    assert has(report, Code.INERTIA_SCALE_ANOMALY)
    assert find_issue(report, Code.INERTIA_SCALE_ANOMALY).severity == \
        Severity.WARNING


def test_scale_anomaly_too_large_radius_warns():
    # 质量 1 kg、惯量 1e6 -> 半径 ~577 m。
    report = check(inertia_xml(mass="1", ixx="1e6", iyy="1e6", izz="1e6"))
    assert has(report, Code.INERTIA_SCALE_ANOMALY)


def test_normal_si_robot_no_scale_warning():
    # 1 kg、0.1 m 量级方块：半径约 0.04 m，正常。
    v = "0.001666666666666667"
    report = check(inertia_xml(mass="1", ixx=v, iyy=v, izz=v))
    assert not has(report, Code.INERTIA_SCALE_ANOMALY)


def test_warning_does_not_fail_report():
    report = check(inertia_xml(mass="1", ixx="1", iyy="1", izz="0"))
    assert report.ok  # 只有 warning
    assert report.warnings and not report.errors


# --- 定位信息 ---

def test_issue_locates_node_attribute_and_line():
    report = check(inertia_xml(mass="0.0"))
    issue = find_issue(report, Code.MASS_ZERO)
    assert "link[@name='L']" in issue.node
    assert "mass" in issue.node
    assert issue.attribute == "value"
    assert issue.line >= 1


def test_default_tolerances_are_sane():
    assert DEFAULT_TOL["axis_norm"] == 1e-6
    assert DEFAULT_TOL["eig_rel"] == 1e-9
