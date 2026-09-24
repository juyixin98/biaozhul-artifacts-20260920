"""URDF 数据模型:从已安全解析的 XML 树构建 Link/Joint/Inertial。

数值解析集中在这里:所有非法数值(非浮点、NaN、Inf)都转成定位到
节点属性的诊断,绝不抛出未捕获异常。
"""

from __future__ import annotations

import math
from dataclasses import dataclass, field
from typing import Optional

from lxml import etree

from .diagnostics import Diagnostic, Severity

# 关节类型(URDF 规范)
JOINT_TYPES = {
    "revolute",
    "continuous",
    "prismatic",
    "fixed",
    "floating",
    "planar",
}
# 需要限位与轴检查的关节
_ACTUATED_TYPES = {"revolute", "prismatic", "continuous"}


def node_path(el: etree._Element) -> str:
    """元素的定位路径(含谓词下标),用于错误定位。"""
    return el.getroottree().getpath(el)


def parse_float(
    el: etree._Element,
    attr: str,
    diagnostics: list,
    *,
    required: bool = True,
    code: str = "INVALID_NUMERIC",
) -> Optional[float]:
    """解析单个浮点属性;非法时记录诊断并返回 None。

    NaN/Inf 视为非法数值(有限性检查)。
    """
    raw = el.get(attr)
    if raw is None:
        if required:
            diagnostics.append(
                Diagnostic(
                    Severity.ERROR.value,
                    "MISSING_ATTRIBUTE",
                    f"缺少必需属性 '{attr}'",
                    node=node_path(el),
                    attribute=attr,
                    line=el.sourceline,
                )
            )
        return None
    try:
        value = float(raw.strip())
    except (ValueError, AttributeError):
        diagnostics.append(
            Diagnostic(
                Severity.ERROR.value,
                code,
                f"属性 '{attr}' 的值 {raw!r} 不是合法浮点数",
                node=node_path(el),
                attribute=attr,
                line=el.sourceline,
            )
        )
        return None
    if not math.isfinite(value):
        diagnostics.append(
            Diagnostic(
                Severity.ERROR.value,
                code,
                f"属性 '{attr}' 的值 {raw!r} 不是有限数值(NaN/Inf 不允许)",
                node=node_path(el),
                attribute=attr,
                line=el.sourceline,
            )
        )
        return None
    return value


@dataclass
class Inertial:
    """link 的惯性参数。matrix 为 None 表示惯性张量数值非法。"""

    element: etree._Element
    mass: Optional[float] = None
    matrix: Optional[object] = None  # numpy.ndarray (3,3) 或 None
    origin_xyz: tuple = (0.0, 0.0, 0.0)
    origin_rpy: tuple = (0.0, 0.0, 0.0)


@dataclass
class Link:
    name: str
    element: etree._Element
    inertial: Optional[Inertial] = None


@dataclass
class Joint:
    name: str
    type: str
    element: etree._Element
    parent: Optional[str] = None
    child: Optional[str] = None
    axis: Optional[tuple] = None       # 解析后的 (x,y,z);无 axis 元素时为 None
    axis_element: Optional[etree._Element] = None
    limit_element: Optional[etree._Element] = None


@dataclass
class RobotModel:
    name: str
    links: list = field(default_factory=list)   # list[Link],保留重复名以便诊断
    joints: list = field(default_factory=list)  # list[Joint]


def _parse_vector3(
    el: etree._Element, attr: str, diagnostics: list
) -> Optional[tuple]:
    """解析 'x y z' 三浮点属性。"""
    raw = el.get(attr)
    if raw is None:
        return None
    parts = raw.split()
    if len(parts) != 3:
        diagnostics.append(
            Diagnostic(
                Severity.ERROR.value,
                "INVALID_NUMERIC",
                f"属性 '{attr}' 需要 3 个浮点数,实际为 {raw!r}",
                node=node_path(el),
                attribute=attr,
                line=el.sourceline,
            )
        )
        return None
    values = []
    for token in parts:
        try:
            v = float(token)
        except ValueError:
            v = None
        if v is None or not math.isfinite(v):
            diagnostics.append(
                Diagnostic(
                    Severity.ERROR.value,
                    "INVALID_NUMERIC",
                    f"属性 '{attr}' 含非法数值 {token!r}",
                    node=node_path(el),
                    attribute=attr,
                    line=el.sourceline,
                )
            )
            return None
        values.append(v)
    return tuple(values)


def _build_inertial(el: etree._Element, diagnostics: list) -> Inertial:
    import numpy as np  # 延迟导入,保持模块加载轻量

    inertial = Inertial(element=el)

    mass_el = el.find("mass")
    if mass_el is None:
        diagnostics.append(
            Diagnostic(
                Severity.ERROR.value,
                "MISSING_MASS",
                "inertial 缺少 <mass> 子元素",
                node=node_path(el),
                attribute=None,
                line=el.sourceline,
            )
        )
    else:
        inertial.mass = parse_float(mass_el, "value", diagnostics)

    origin_el = el.find("origin")
    if origin_el is not None:
        xyz = _parse_vector3(origin_el, "xyz", diagnostics)
        rpy = _parse_vector3(origin_el, "rpy", diagnostics)
        if xyz is not None:
            inertial.origin_xyz = xyz
        if rpy is not None:
            inertial.origin_rpy = rpy

    inertia_el = el.find("inertia")
    if inertia_el is None:
        diagnostics.append(
            Diagnostic(
                Severity.ERROR.value,
                "MISSING_INERTIA_ELEMENT",
                "inertial 缺少 <inertia> 子元素",
                node=node_path(el),
                attribute=None,
                line=el.sourceline,
            )
        )
        return inertial

    comps = {}
    ok = True
    for attr in ("ixx", "iyy", "izz", "ixy", "ixz", "iyz"):
        v = parse_float(inertia_el, attr, diagnostics)
        if v is None:
            ok = False
        comps[attr] = v
    if ok:
        inertial.matrix = np.array(
            [
                [comps["ixx"], comps["ixy"], comps["ixz"]],
                [comps["ixy"], comps["iyy"], comps["iyz"]],
                [comps["ixz"], comps["iyz"], comps["izz"]],
            ],
            dtype=float,
        )
    return inertial


def build_model(root: etree._Element, diagnostics: list) -> RobotModel:
    """从 <robot> 根元素构建模型;结构问题记入 diagnostics。"""
    model = RobotModel(name=root.get("name", ""))

    for link_el in root.findall("link"):
        name = link_el.get("name")
        if not name:
            diagnostics.append(
                Diagnostic(
                    Severity.ERROR.value,
                    "MISSING_ATTRIBUTE",
                    "link 缺少 'name' 属性",
                    node=node_path(link_el),
                    attribute="name",
                    line=link_el.sourceline,
                )
            )
            continue
        link = Link(name=name, element=link_el)
        inertial_el = link_el.find("inertial")
        if inertial_el is not None:
            link.inertial = _build_inertial(inertial_el, diagnostics)
        model.links.append(link)

    for joint_el in root.findall("joint"):
        name = joint_el.get("name")
        jtype = joint_el.get("type")
        if not name:
            diagnostics.append(
                Diagnostic(
                    Severity.ERROR.value,
                    "MISSING_ATTRIBUTE",
                    "joint 缺少 'name' 属性",
                    node=node_path(joint_el),
                    attribute="name",
                    line=joint_el.sourceline,
                )
            )
            continue
        if not jtype:
            diagnostics.append(
                Diagnostic(
                    Severity.ERROR.value,
                    "MISSING_ATTRIBUTE",
                    f"joint '{name}' 缺少 'type' 属性",
                    node=node_path(joint_el),
                    attribute="type",
                    line=joint_el.sourceline,
                )
            )
            continue
        if jtype not in JOINT_TYPES:
            diagnostics.append(
                Diagnostic(
                    Severity.ERROR.value,
                    "UNKNOWN_JOINT_TYPE",
                    f"joint '{name}' 的 type={jtype!r} 非法,"
                    f"应为 {sorted(JOINT_TYPES)} 之一",
                    node=node_path(joint_el),
                    attribute="type",
                    line=joint_el.sourceline,
                )
            )
            continue

        joint = Joint(name=name, type=jtype, element=joint_el)

        parent_el = joint_el.find("parent")
        if parent_el is None or not parent_el.get("link"):
            diagnostics.append(
                Diagnostic(
                    Severity.ERROR.value,
                    "MISSING_ATTRIBUTE",
                    f"joint '{name}' 缺少 <parent link='...'>",
                    node=node_path(joint_el),
                    attribute="link",
                    line=joint_el.sourceline,
                )
            )
        else:
            joint.parent = parent_el.get("link")

        child_el = joint_el.find("child")
        if child_el is None or not child_el.get("link"):
            diagnostics.append(
                Diagnostic(
                    Severity.ERROR.value,
                    "MISSING_ATTRIBUTE",
                    f"joint '{name}' 缺少 <child link='...'>",
                    node=node_path(joint_el),
                    attribute="link",
                    line=joint_el.sourceline,
                )
            )
        else:
            joint.child = child_el.get("link")

        axis_el = joint_el.find("axis")
        if axis_el is not None:
            joint.axis_element = axis_el
            joint.axis = _parse_vector3(axis_el, "xyz", diagnostics)

        joint.limit_element = joint_el.find("limit")
        model.joints.append(joint)

    return model
