"""结构检查测试:引用、重名、树结构、环、固定关节、轴、限位。"""

from urdf_check import check_urdf

from .conftest import (
    GOOD_AXIS,
    GOOD_LIMIT,
    inertial_xml,
    joint_xml,
    link_xml,
    make_urdf,
)


def codes(report):
    return {d.code for d in report.diagnostics}


def errors(report):
    return {d.code for d in report.errors}


# ---------- 夹具通过 ----------

def test_two_link_arm_passes(two_link_arm):
    report = check_urdf(two_link_arm)
    assert report.ok, [d.message for d in report.diagnostics]
    assert report.link_count == 2 and report.joint_count == 1


def test_fixed_joint_rig_passes(fixed_sensor_rig):
    # 固定关节合法:无 limit/axis 也不应报错
    report = check_urdf(fixed_sensor_rig)
    assert report.ok, [d.message for d in report.diagnostics]


# ---------- 引用与重名 ----------

def test_duplicate_link_name():
    src = make_urdf(
        link_xml("a", inertial_xml()) + "\n" + link_xml("a", inertial_xml())
    )
    report = check_urdf(src)
    assert "DUPLICATE_LINK_NAME" in errors(report)
    diag = next(d for d in report.errors if d.code == "DUPLICATE_LINK_NAME")
    assert diag.attribute == "name"
    assert diag.node and "link" in diag.node
    assert diag.line is not None


def test_duplicate_joint_name():
    links = "\n".join(link_xml(n, inertial_xml()) for n in ("a", "b", "c"))
    joints = "\n".join(
        joint_xml("j", "fixed", p, c) for p, c in (("a", "b"), ("a", "c"))
    )
    report = check_urdf(make_urdf(links, joints))
    assert "DUPLICATE_JOINT_NAME" in errors(report)


def test_joint_unknown_link():
    links = link_xml("a", inertial_xml())
    joints = joint_xml("j", "fixed", "a", "ghost")
    report = check_urdf(make_urdf(links, joints))
    assert "JOINT_UNKNOWN_LINK" in errors(report)


def test_unknown_joint_type():
    links = "\n".join(link_xml(n, inertial_xml()) for n in ("a", "b"))
    joints = joint_xml("j", "telescopic", "a", "b")
    report = check_urdf(make_urdf(links, joints))
    assert "UNKNOWN_JOINT_TYPE" in errors(report)


# ---------- 树结构与环 ----------

def test_cycle_rejected():
    # a→b→c→a 纯环:无根
    links = "\n".join(link_xml(n, inertial_xml()) for n in ("a", "b", "c"))
    joints = "\n".join(
        joint_xml(f"j{i}", "fixed", p, c)
        for i, (p, c) in enumerate((("a", "b"), ("b", "c"), ("c", "a")))
    )
    report = check_urdf(make_urdf(links, joints))
    assert not report.ok
    assert codes(report) & {"NO_ROOT_LINK", "KINEMATIC_CYCLE"}


def test_disconnected_cycle_rejected():
    # 合法树 root→leaf,外加孤立环 x→y→x
    links = "\n".join(
        link_xml(n, inertial_xml()) for n in ("root", "leaf", "x", "y")
    )
    joints = "\n".join(
        [
            joint_xml("ok", "fixed", "root", "leaf"),
            joint_xml("c1", "fixed", "x", "y"),
            joint_xml("c2", "fixed", "y", "x"),
        ]
    )
    report = check_urdf(make_urdf(links, joints))
    assert not report.ok
    assert "KINEMATIC_CYCLE" in errors(report)


def test_multiple_roots_rejected():
    links = "\n".join(link_xml(n, inertial_xml()) for n in ("a", "b"))
    report = check_urdf(make_urdf(links))
    assert "MULTIPLE_ROOTS" in errors(report)


def test_multiple_parents_rejected():
    links = "\n".join(link_xml(n, inertial_xml()) for n in ("a", "b", "c"))
    joints = "\n".join(
        [joint_xml("j1", "fixed", "a", "c"),
         joint_xml("j2", "fixed", "b", "c")]
    )
    report = check_urdf(make_urdf(links, joints))
    assert "MULTIPLE_PARENTS" in errors(report)


# ---------- 关节轴 ----------

def test_axis_zero_is_error():
    links = "\n".join(link_xml(n, inertial_xml()) for n in ("a", "b"))
    joints = joint_xml(
        "j", "revolute", "a", "b",
        extra='<axis xyz="0 0 0"/>' + GOOD_LIMIT,
    )
    report = check_urdf(make_urdf(links, joints))
    assert "AXIS_ZERO" in errors(report)
    diag = next(d for d in report.errors if d.code == "AXIS_ZERO")
    assert diag.attribute == "xyz"


def test_axis_not_normalized_is_warning():
    links = "\n".join(link_xml(n, inertial_xml()) for n in ("a", "b"))
    joints = joint_xml(
        "j", "revolute", "a", "b",
        extra='<axis xyz="0 0 2"/>' + GOOD_LIMIT,
    )
    report = check_urdf(make_urdf(links, joints))
    assert report.ok  # 仅告警,不阻断
    assert "AXIS_NOT_NORMALIZED" in codes(report)


def test_axis_default_ok():
    # 未给 axis 时使用 URDF 默认轴 (1,0,0),不报任何问题
    links = "\n".join(link_xml(n, inertial_xml()) for n in ("a", "b"))
    joints = joint_xml("j", "revolute", "a", "b", extra=GOOD_LIMIT)
    report = check_urdf(make_urdf(links, joints))
    assert report.ok, [d.message for d in report.diagnostics]


# ---------- 限位 ----------

def test_missing_limit_is_error():
    links = "\n".join(link_xml(n, inertial_xml()) for n in ("a", "b"))
    joints = joint_xml("j", "prismatic", "a", "b", extra=GOOD_AXIS)
    report = check_urdf(make_urdf(links, joints))
    assert "MISSING_LIMIT" in errors(report)


def test_limit_bounds_inverted():
    links = "\n".join(link_xml(n, inertial_xml()) for n in ("a", "b"))
    joints = joint_xml(
        "j", "revolute", "a", "b",
        extra=GOOD_AXIS
        + '<limit lower="1.0" upper="-1.0" effort="10" velocity="1"/>',
    )
    report = check_urdf(make_urdf(links, joints))
    assert "LIMIT_BOUNDS_INVERTED" in errors(report)
    diag = next(
        d for d in report.errors if d.code == "LIMIT_BOUNDS_INVERTED"
    )
    assert diag.attribute == "lower"


def test_limit_negative_effort():
    links = "\n".join(link_xml(n, inertial_xml()) for n in ("a", "b"))
    joints = joint_xml(
        "j", "revolute", "a", "b",
        extra=GOOD_AXIS
        + '<limit lower="0" upper="1" effort="-5" velocity="1"/>',
    )
    report = check_urdf(make_urdf(links, joints))
    assert "LIMIT_NEGATIVE" in errors(report)


def test_limit_nan_rejected():
    links = "\n".join(link_xml(n, inertial_xml()) for n in ("a", "b"))
    joints = joint_xml(
        "j", "revolute", "a", "b",
        extra=GOOD_AXIS
        + '<limit lower="nan" upper="1" effort="5" velocity="1"/>',
    )
    report = check_urdf(make_urdf(links, joints))
    assert "INVALID_NUMERIC" in errors(report)
