"""结构检查：重名、引用、树/森林/循环、固定关节、关节类型。"""

from urdf_check.issues import Code

from conftest import chain_urdf, check, codes, has, single_link_urdf


def test_minimal_single_link_is_tree():
    assert check(single_link_urdf()).ok


def test_fixed_joint_allowed():
    xml = chain_urdf(
        ["a", "b"],
        [("f", "fixed", "a", "b", "")],
    )
    assert check(xml).ok, check(xml).to_dict()


def test_duplicate_link_rejected():
    xml = """<?xml version="1.0"?>
<robot name="r">
  <link name="dup"><inertial><mass value="1"/>
    <inertia ixx="1" ixy="0" ixz="0" iyy="1" iyz="0" izz="1"/></inertial></link>
  <link name="dup"><inertial><mass value="1"/>
    <inertia ixx="1" ixy="0" ixz="0" iyy="1" iyz="0" izz="1"/></inertial></link>
</robot>
"""
    report = check(xml)
    assert has(report, Code.DUPLICATE_LINK)
    issue = next(i for i in report.issues if i.code == Code.DUPLICATE_LINK)
    assert issue.attribute == "name"
    assert issue.node.endswith("<link name='dup'>") or "link[@name='dup']" in issue.node
    assert issue.line > 0


def test_duplicate_joint_rejected():
    xml = chain_urdf(
        ["a", "b", "c"],
        [("j", "fixed", "a", "b", ""),
         ("j", "fixed", "b", "c", "")],
    )
    assert has(check(xml), Code.DUPLICATE_JOINT)


def test_missing_link_name_and_joint_name():
    xml = """<?xml version="1.0"?>
<robot name="r">
  <link><inertial><mass value="1"/>
    <inertia ixx="1" ixy="0" ixz="0" iyy="1" iyz="0" izz="1"/></inertial></link>
  <joint type="fixed"><parent link="x"/><child link="y"/></joint>
</robot>
"""
    report = check(xml)
    assert has(report, Code.LINK_NAME_MISSING)
    assert has(report, Code.JOINT_NAME_MISSING)


def test_parent_link_unknown():
    xml = chain_urdf(["b"], [("j", "fixed", "ghost", "b", "")])
    report = check(xml)
    assert has(report, Code.JOINT_PARENT_UNKNOWN)
    assert not has(report, Code.JOINT_CHILD_UNKNOWN)


def test_child_link_unknown():
    xml = chain_urdf(["a"], [("j", "fixed", "a", "ghost", "")])
    report = check(xml)
    assert has(report, Code.JOINT_CHILD_UNKNOWN)


def test_parent_child_elements_missing():
    xml = """<?xml version="1.0"?>
<robot name="r">
  <link name="a"><inertial><mass value="1"/>
    <inertia ixx="1" ixy="0" ixz="0" iyy="1" iyz="0" izz="1"/></inertial></link>
  <joint name="j" type="fixed"/>
</robot>
"""
    report = check(xml)
    assert has(report, Code.JOINT_PARENT_MISSING)
    assert has(report, Code.JOINT_CHILD_MISSING)


def test_unknown_joint_type():
    xml = chain_urdf(["a", "b"], [("j", "wobbly", "a", "b", "")])
    assert has(check(xml), Code.UNKNOWN_JOINT_TYPE)


def test_joint_type_missing():
    xml = """<?xml version="1.0"?>
<robot name="r">
  <link name="a"/>
  <link name="b"/>
  <joint name="j"><parent link="a"/><child link="b"/></joint>
</robot>
"""
    assert has(check(xml), Code.JOINT_TYPE_MISSING)


def test_two_roots_is_forest_error():
    # 两条不相交的链：固定关节也不能让模型成为森林。
    xml = chain_urdf(
        ["a", "b", "c", "d"],
        [("j1", "fixed", "a", "b", ""),
         ("j2", "fixed", "c", "d", "")],
    )
    report = check(xml)
    assert has(report, Code.TREE_MULTIPLE_ROOTS)
    assert not has(report, Code.TREE_CYCLE)


def test_directed_cycle_rejected_even_with_fixed_joints():
    # a -> b -> c -> a：三个全是 fixed，循环仍然不允许。
    xml = chain_urdf(
        ["a", "b", "c"],
        [("j1", "fixed", "a", "b", ""),
         ("j2", "fixed", "b", "c", ""),
         ("j3", "fixed", "c", "a", "")],
    )
    report = check(xml)
    assert has(report, Code.TREE_CYCLE)


def test_diamond_multi_parent_is_undirected_cycle():
    # root -> a,b -> leaf：leaf 有两个父节点，无向图成环。
    xml = chain_urdf(
        ["root", "a", "b", "leaf"],
        [("j1", "fixed", "root", "a", ""),
         ("j2", "fixed", "root", "b", ""),
         ("j3", "fixed", "a", "leaf", ""),
         ("j4", "fixed", "b", "leaf", "")],
    )
    assert has(check(xml), Code.TREE_CYCLE)


def test_self_loop_joint():
    xml = chain_urdf(["a"], [("j", "fixed", "a", "a", "")])
    report = check(xml)
    assert has(report, Code.JOINT_SELF_LOOP)
    assert has(report, Code.TREE_CYCLE)


def test_chain_through_fixed_and_revolute_is_ok():
    xml = chain_urdf(
        ["a", "b", "c"],
        [("f", "fixed", "a", "b", ""),
         ("r", "revolute", "b", "c",
          '<axis xyz="0 0 1"/><limit lower="-1" upper="1" effort="1" velocity="1"/>')],
    )
    report = check(xml)
    assert report.ok, report.to_dict()
