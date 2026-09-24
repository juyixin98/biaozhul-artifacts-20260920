"""关节轴归一化（含浮点容差）与限位上下界检查。"""

from urdf_check.issues import Code

from conftest import chain_urdf, check, find_issue, has


def joint_xml(jtype="revolute", axis=None, limit='<limit lower="-1" upper="1" '
              'effort="5" velocity="1"/>'):
    axis_line = f"<axis xyz='{axis}'/>" if axis is not None else ""
    return chain_urdf(
        ["a", "b"],
        [("j", jtype, "a", "b", axis_line + limit)],
    )


# --- 轴 ---

def test_axis_unit_vectors_pass():
    for axis in ("1 0 0", "-1 0 0", "0.7071067811865476 0.7071067811865476 0"):
        assert check(joint_xml(axis=axis)).ok, axis


def test_axis_within_tolerance_passes():
    # 偏差恰好等于默认容差 1e-6 以内：(1+5e-7, 0, 0) 的模为 1.0000005。
    assert check(joint_xml(axis="1.0000005 0 0")).ok


def test_axis_outside_tolerance_fails_and_locates_attribute():
    # 明显未归一化：2 0 0 -> norm 2。
    report = check(joint_xml(axis="2 0 0"))
    assert has(report, Code.AXIS_NOT_NORMALIZED)
    issue = find_issue(report, Code.AXIS_NOT_NORMALIZED)
    assert issue.attribute == "xyz"
    assert "axis" in issue.node and issue.line > 0


def test_axis_near_unit_but_beyond_custom_tolerance():
    # 偏差约 5e-7：默认容差（1e-6）通过，收紧到 1e-9 则报错。
    assert check(joint_xml(axis="1.0000005 0 0")).ok
    report = check(joint_xml(axis="1.0000005 0 0"), tol={"axis_norm": 1e-9})
    assert has(report, Code.AXIS_NOT_NORMALIZED)


def test_axis_zero_vector():
    report = check(joint_xml(axis="0 0 0"))
    assert has(report, Code.AXIS_ZERO_NORM)
    assert not has(report, Code.AXIS_NOT_NORMALIZED)


def test_axis_nonfinite_and_wrong_arity():
    assert has(check(joint_xml(axis="1 0 nan")), Code.AXIS_INVALID_NUMERIC)
    assert has(check(joint_xml(axis="1 inf 0")), Code.AXIS_INVALID_NUMERIC)
    assert has(check(joint_xml(axis="1 0")), Code.AXIS_INVALID_NUMERIC)
    assert has(check(joint_xml(axis="abc 0 0")), Code.AXIS_INVALID_NUMERIC)


# --- 限位 ---

def test_revolute_requires_limit():
    xml = chain_urdf(["a", "b"], [("j", "revolute", "a", "b",
                                  '<axis xyz="0 0 1"/>')])
    assert has(check(xml), Code.LIMIT_MISSING)


def test_prismatic_requires_limit():
    xml = chain_urdf(["a", "b"], [("j", "prismatic", "a", "b", "")])
    assert has(check(xml), Code.LIMIT_MISSING)


def test_continuous_needs_effort_velocity_but_no_bounds():
    xml = chain_urdf(
        ["a", "b"],
        [("j", "continuous", "a", "b",
          '<axis xyz="0 0 1"/><limit effort="5" velocity="1"/>')],
    )
    report = check(xml)
    assert report.ok, report.to_dict()


def test_limit_missing_each_attribute():
    for attr in ("effort", "velocity", "lower", "upper"):
        full = {"lower": "-1", "upper": "1", "effort": "5", "velocity": "1"}
        del full[attr]
        lim = "<limit " + " ".join(f'{k}="{v}"' for k, v in full.items()) + "/>"
        report = check(joint_xml(limit=lim))
        assert has(report, Code.LIMIT_ATTR_MISSING), attr
        issue = find_issue(report, Code.LIMIT_ATTR_MISSING)
        assert issue.attribute == attr


def test_limit_nan_is_invalid_numeric():
    lim = '<limit lower="nan" upper="1" effort="5" velocity="1"/>'
    report = check(joint_xml(limit=lim))
    assert has(report, Code.LIMIT_INVALID_NUMERIC)
    assert find_issue(report, Code.LIMIT_INVALID_NUMERIC).attribute == "lower"


def test_lower_equal_upper_is_valid_boundary():
    lim = '<limit lower="0.5" upper="0.5" effort="5" velocity="1"/>'
    assert check(joint_xml(limit=lim)).ok


def test_lower_greater_than_upper():
    lim = '<limit lower="2" upper="1" effort="5" velocity="1"/>'
    report = check(joint_xml(limit=lim))
    assert has(report, Code.LIMIT_LOWER_GT_UPPER)


def test_fixed_joint_without_limit_passes():
    xml = chain_urdf(["a", "b"], [("j", "fixed", "a", "b", "")])
    assert check(xml).ok
