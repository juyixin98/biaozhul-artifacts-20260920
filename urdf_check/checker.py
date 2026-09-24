"""URDF 结构与物理参数检查核心。

检查内容：
* link / joint 引用完整性、link/joint 重名；
* 运动链必须是单棵有根树：固定关节允许存在，但循环、多根（森林）均报错；
* 关节轴为有限数、非零且单位归一化（浮点容差）；
* revolute / continuous / prismatic 关节的 effort、velocity 限位参数，
  以及 revolute / prismatic 的 lower <= upper；
* 每个 link 必须有 <inertial>；质量与惯量矩阵分量必须为有限数；
  质量为零与数值非法是两类不同的问题码；
* 惯量矩阵对称正定（对特征值判定，含浮点容差与近半正定告警），
  并对主转动惯量验证三角不等式 I1 + I2 >= I3（轮换三个）；
* 单位/量级异常（由质量与主惯量反推的等效回转半径不在机械臂合理范围）
  仅产生告警，不阻断。
"""

from __future__ import annotations

import math
from dataclasses import dataclass

import numpy as np
from lxml import etree

from .issues import Code, Issue, Severity
from .parser import parse_urdf

# ---------------------------------------------------------------- 容差

DEFAULT_TOL = {
    "axis_norm": 1e-6,        # | |axis| - 1 | 允许的绝对偏差
    "eig_rel": 1e-9,          # 特征值判定的相对容差（按最大特征值缩放）
    "eig_abs": 1e-30,         # 绝对下限，避免近零矩阵下相对容差失效
    "semi_warn_rel": 1e-6,    # 最小主惯量小于最大主惯量的该比例 -> 近奇异告警
    "tri_viol": 1e-9,         # 三角不等式相对容差（按最大主惯量缩放）
    "radius_min_m": 1e-4,     # 等效回转半径合理下限（0.1 mm）
    "radius_max_m": 1e2,      # 等效回转半径合理上限（100 m）
}

REVOLUTE_LIMIT_TYPES = {"revolute", "prismatic"}   # 必须有 lower/upper
LIMITED_JOINT_TYPES = REVOLUTE_LIMIT_TYPES | {"continuous"}
JOINT_TYPES = LIMITED_JOINT_TYPES | {"fixed", "floating", "planar"}

INERTIA_ATTRS = ("ixx", "ixy", "ixz", "iyy", "iyz", "izz")


# ---------------------------------------------------------------- 工具

def _node_path(el: etree._Element) -> str:
    """生成带 name 的定位路径，如 /robot/link[@name='arm']/inertial/inertia。"""
    parts: list[str] = []
    cur = el
    while cur is not None and isinstance(cur.tag, str):
        tag = etree.QName(cur).localname
        name = cur.get("name")
        if name is not None:
            parts.append(f"{tag}[@name='{name}']")
        elif tag == "joint" and (p := cur.find("parent")) is not None \
                and (c := cur.find("child")) is not None:
            parts.append(f"{tag}[{p.get('link')}->{c.get('link')}]")
        else:
            parts.append(tag)
        cur = cur.getparent()
    return "/" + "/".join(reversed(parts))


def _line(el: etree._Element) -> int:
    return getattr(el, "sourceline", 0) or 0


def _finite_float(raw: str | None):
    """返回 (value, ok)：非空、可解析、有限（非 NaN/Inf）才合法。"""
    if raw is None:
        return None, False
    try:
        v = float(raw.strip())
    except (ValueError, AttributeError):
        return None, False
    if not math.isfinite(v):
        return None, False
    return v, True


# ---------------------------------------------------------------- 入口

@dataclass
class Report:
    issues: list[Issue]

    @property
    def errors(self) -> list[Issue]:
        return [i for i in self.issues if i.severity == Severity.ERROR]

    @property
    def warnings(self) -> list[Issue]:
        return [i for i in self.issues if i.severity == Severity.WARNING]

    @property
    def ok(self) -> bool:
        return not self.errors

    def to_dict(self) -> dict:
        return {
            "ok": self.ok,
            "error_count": len(self.errors),
            "warning_count": len(self.warnings),
            "issues": [i.to_dict() for i in self.issues],
        }


def inspect_urdf_bytes(data: bytes | str, tol: dict | None = None) -> Report:
    merged = {**DEFAULT_TOL, **(tol or {})}
    parsed = parse_urdf(data)
    if parsed.root is None:
        return Report(parsed.issues)

    issues = list(parsed.issues)
    issues.extend(_check_document(parsed.root, merged))
    return Report(issues)


def inspect_urdf_file(path: str, tol: dict | None = None) -> Report:
    with open(path, "rb") as fh:
        return inspect_urdf_bytes(fh.read(), tol=tol)


# ---------------------------------------------------------------- 结构

def _check_document(root: etree._Element, tol: dict) -> list[Issue]:
    issues: list[Issue] = []

    if etree.QName(root).localname != "robot":
        return [Issue(
            Severity.ERROR, Code.ROOT_NOT_ROBOT,
            f"根元素必须是 <robot>，实际为 <{root.tag}>。",
            node=_node_path(root), line=_line(root),
        )]

    name = root.get("name")
    if name is None or not name.strip():
        issues.append(Issue(
            Severity.ERROR, Code.ROBOT_NAME_MISSING,
            "<robot> 缺少 name 属性。", node=_node_path(root),
            attribute="name", line=_line(root),
        ))

    links = root.findall("link")
    joints = root.findall("joint")

    link_names: set[str] = set()
    link_els: dict[str, etree._Element] = {}
    for link in links:
        lname = link.get("name")
        if lname is None or not lname.strip():
            issues.append(Issue(
                Severity.ERROR, Code.LINK_NAME_MISSING,
                "<link> 缺少 name 属性。", node=_node_path(link),
                attribute="name", line=_line(link),
            ))
            continue
        if lname in link_names:
            issues.append(Issue(
                Severity.ERROR, Code.DUPLICATE_LINK,
                f"link 名称 '{lname}' 重复。", node=_node_path(link),
                attribute="name", line=_line(link),
            ))
        else:
            link_names.add(lname)
            link_els[lname] = link
        issues.extend(_check_link_inertial(link, tol))

    joint_names: set[str] = set()
    edges: list[tuple[str, str, etree._Element]] = []
    for joint in joints:
        jname = joint.get("name")
        path = _node_path(joint)
        jline = _line(joint)
        if jname is None or not jname.strip():
            issues.append(Issue(
                Severity.ERROR, Code.JOINT_NAME_MISSING,
                "<joint> 缺少 name 属性。", node=path,
                attribute="name", line=jline,
            ))
        elif jname in joint_names:
            issues.append(Issue(
                Severity.ERROR, Code.DUPLICATE_JOINT,
                f"joint 名称 '{jname}' 重复。", node=path,
                attribute="name", line=jline,
            ))
        else:
            joint_names.add(jname)

        jtype, parent_name, child_name = _check_joint_refs(
            joint, link_names, issues)
        issues.extend(_check_joint_axis_limit(joint, jtype, tol))

        if parent_name is not None and child_name is not None \
                and parent_name in link_names and child_name in link_names:
            edges.append((parent_name, child_name, joint))

    _check_tree(edges, link_names, link_els, issues)
    return issues


def _check_joint_refs(joint, link_names: set[str], issues: list[Issue]):
    path = _node_path(joint)
    jline = _line(joint)

    jtype = joint.get("type")
    if jtype is None:
        issues.append(Issue(
            Severity.ERROR, Code.JOINT_TYPE_MISSING,
            f"joint '{joint.get('name')}' 缺少 type 属性。",
            node=path, attribute="type", line=jline,
        ))
        jtype = ""
    elif jtype not in JOINT_TYPES:
        issues.append(Issue(
            Severity.ERROR, Code.UNKNOWN_JOINT_TYPE,
            f"joint '{joint.get('name')}' 的 type='{jtype}' 不支持，"
            f"允许：{sorted(JOINT_TYPES)}。",
            node=path, attribute="type", line=jline,
        ))

    parent = joint.find("parent")
    child = joint.find("child")
    parent_name = parent.get("link") if parent is not None else None
    child_name = child.get("link") if child is not None else None

    if parent is None:
        issues.append(Issue(
            Severity.ERROR, Code.JOINT_PARENT_MISSING,
            f"joint '{joint.get('name')}' 缺少 <parent link='...'/>。",
            node=path, line=jline,
        ))
    elif not (parent_name or "").strip():
        issues.append(Issue(
            Severity.ERROR, Code.JOINT_PARENT_MISSING,
            f"joint '{joint.get('name')}' 的 <parent> 缺少 link 属性。",
            node=_node_path(parent), attribute="link", line=_line(parent),
        ))
    elif parent_name not in link_names:
        issues.append(Issue(
            Severity.ERROR, Code.JOINT_PARENT_UNKNOWN,
            f"joint '{joint.get('name')}' 的 parent link '{parent_name}' "
            "未在本文件中定义。",
            node=_node_path(parent), attribute="link", line=_line(parent),
        ))

    if child is None:
        issues.append(Issue(
            Severity.ERROR, Code.JOINT_CHILD_MISSING,
            f"joint '{joint.get('name')}' 缺少 <child link='...'/>。",
            node=path, line=jline,
        ))
    elif not (child_name or "").strip():
        issues.append(Issue(
            Severity.ERROR, Code.JOINT_CHILD_MISSING,
            f"joint '{joint.get('name')}' 的 <child> 缺少 link 属性。",
            node=_node_path(child), attribute="link", line=_line(child),
        ))
    elif child_name not in link_names:
        issues.append(Issue(
            Severity.ERROR, Code.JOINT_CHILD_UNKNOWN,
            f"joint '{joint.get('name')}' 的 child link '{child_name}' "
            "未在本文件中定义。",
            node=_node_path(child), attribute="link", line=_line(child),
        ))
    elif parent_name == child_name and parent_name in link_names:
        issues.append(Issue(
            Severity.ERROR, Code.JOINT_SELF_LOOP,
            f"joint '{joint.get('name')}' 的 parent 与 child 同为 "
            f"'{child_name}'，形成自环。",
            node=path, line=jline,
        ))

    return jtype, parent_name, child_name


def _check_tree(edges, link_names: set[str], link_els: dict,
                issues: list[Issue]) -> None:
    """有向边 parent -> child；要求整图恰为一棵有根树。"""
    children: dict[str, list[tuple[str, etree._Element]]] = {}
    indeg: dict[str, int] = {name: 0 for name in link_names}
    for parent, child, joint in edges:
        children.setdefault(parent, []).append((child, joint))
        indeg[child] = indeg.get(child, 0) + 1

    roots = sorted(n for n in link_names if indeg.get(n, 0) == 0)
    for name in roots[1:]:
        el = link_els.get(name)
        issues.append(Issue(
            Severity.ERROR, Code.TREE_MULTIPLE_ROOTS,
            f"link '{name}' 没有任何父关节，运动链存在多个根"
            f"（根包括：{', '.join(roots)}）；要求单棵树。",
            node=f"/robot/link[@name='{name}']", attribute="name",
            line=_line(el) if el is not None else 0,
        ))

    # 无向图 DFS：URDF 运动链必须是无向无环图。多父汇合（菱形，D 有
    # A→B→D 与 A→C→D 两条路径）在有向染色里不是回边，但在无向图中是
    # 环；纯有向环（A→B→C→A）同样在此被捕获。
    seen: set[str] = set()
    cycle_reported = False
    for start in roots or sorted(link_names):
        if start in seen or cycle_reported:
            break
        stack: list[tuple[str, str | None, int]] = [(start, None, 0)]
        seen.add(start)
        while stack and not cycle_reported:
            node, parent, idx = stack[-1]
            neigh = children.get(node, [])
            if idx < len(neigh):
                nxt, joint = neigh[idx]
                stack[-1] = (node, parent, idx + 1)
                if nxt == node:
                    # 自环节点：parent==child，无向图中也是环。
                    issues.append(Issue(
                        Severity.ERROR, Code.TREE_CYCLE,
                        f"joint '{joint.get('name')}' 是自环"
                        f"（parent 与 child 同为 '{node}'）。",
                        node=_node_path(joint), line=_line(joint),
                    ))
                    cycle_reported = True
                    break
                if nxt == parent:
                    continue
                if nxt in seen:
                    issues.append(Issue(
                        Severity.ERROR, Code.TREE_CYCLE,
                        f"joint '{joint.get('name')}' 使运动链形成循环"
                        f"（link '{nxt}' 已存在另一条父子路径）；"
                        "固定关节也不能成环。",
                        node=_node_path(joint), line=_line(joint),
                    ))
                    cycle_reported = True
                    break
                seen.add(nxt)
                stack.append((nxt, node, 0))
            else:
                stack.pop()


# ------------------------------------------------------- 关节轴与限位

def _check_joint_axis_limit(joint, jtype: str, tol: dict) -> list[Issue]:
    issues: list[Issue] = []
    path = _node_path(joint)
    jline = _line(joint)
    jname = joint.get("name")

    axis_el = joint.find("axis")
    if axis_el is not None:
        raw = axis_el.get("xyz", "")
        comps = [p.strip() for p in raw.split()]
        vals = []
        bad = len(comps) != 3
        if not bad:
            for c in comps:
                v, ok = _finite_float(c)
                vals.append(v)
                if not ok:
                    bad = True
        if bad:
            issues.append(Issue(
                Severity.ERROR, Code.AXIS_INVALID_NUMERIC,
                f"joint '{jname}' 的 axis.xyz 必须是 3 个有限浮点数，"
                f"实际为 '{raw}'。",
                node=_node_path(axis_el), attribute="xyz",
                line=_line(axis_el),
            ))
        else:
            norm = float(np.linalg.norm(vals))
            if norm == 0.0:
                issues.append(Issue(
                    Severity.ERROR, Code.AXIS_ZERO_NORM,
                    f"joint '{jname}' 的关节轴为零向量，无法定义运动方向。",
                    node=_node_path(axis_el), attribute="xyz",
                    line=_line(axis_el),
                ))
            elif abs(norm - 1.0) > tol["axis_norm"]:
                issues.append(Issue(
                    Severity.ERROR, Code.AXIS_NOT_NORMALIZED,
                    f"joint '{jname}' 的关节轴未归一化：|xyz|={norm:.12g}，"
                    f"偏差 {abs(norm - 1.0):.3g} 超过容差 "
                    f"{tol['axis_norm']:g}（应单位向量）。",
                    node=_node_path(axis_el), attribute="xyz",
                    line=_line(axis_el),
                ))

    limit_el = joint.find("limit")
    if jtype in LIMITED_JOINT_TYPES:
        if limit_el is None:
            issues.append(Issue(
                Severity.ERROR, Code.LIMIT_MISSING,
                f"{jtype} joint '{jname}' 必须包含 <limit>。",
                node=path, line=jline,
            ))
            return issues
        issues.extend(_check_limit_attrs(limit_el, jname, bounded=jtype
                                        in REVOLUTE_LIMIT_TYPES))
    elif limit_el is not None:
        # 其它关节类型若显式给了 limit，仍校验 effort/velocity 数值。
        issues.extend(_check_limit_attrs(limit_el, jname, bounded=False))

    return issues


def _check_limit_attrs(limit_el, jname: str, bounded: bool) -> list[Issue]:
    issues: list[Issue] = []
    path = _node_path(limit_el)
    line = _line(limit_el)
    values: dict[str, float] = {}

    required = ["effort", "velocity"] + (["lower", "upper"] if bounded else [])
    for attr in required:
        raw = limit_el.get(attr)
        if raw is None:
            issues.append(Issue(
                Severity.ERROR, Code.LIMIT_ATTR_MISSING,
                f"<limit> of joint '{jname}' 缺少 {attr} 属性。",
                node=path, attribute=attr, line=line,
            ))
            continue
        v, ok = _finite_float(raw)
        if not ok:
            issues.append(Issue(
                Severity.ERROR, Code.LIMIT_INVALID_NUMERIC,
                f"<limit> of joint '{jname}' 的 {attr}='{raw}' 不是有限数。",
                node=path, attribute=attr, line=line,
            ))
            continue
        values[attr] = v

    if bounded and "lower" in values and "upper" in values:
        if values["lower"] > values["upper"]:
            issues.append(Issue(
                Severity.ERROR, Code.LIMIT_LOWER_GT_UPPER,
                f"joint '{jname}' 限位下界 {values['lower']:g} 大于上界 "
                f"{values['upper']:g}。",
                node=path, attribute="lower,upper", line=line,
            ))
    return issues


# -------------------------------------------------------------- 惯性

def _check_link_inertial(link, tol: dict) -> list[Issue]:
    issues: list[Issue] = []
    lname = link.get("name")
    path = _node_path(link)

    inertial = link.find("inertial")
    if inertial is None:
        # 与数值非法明确分开：这里是「缺少」一整段惯性定义。
        issues.append(Issue(
            Severity.ERROR, Code.INERTIAL_MISSING,
            f"link '{lname}' 缺少 <inertial>（质量与惯量均未定义）。",
            node=path, line=_line(link),
        ))
        return issues

    ipath = _node_path(inertial)
    iline = _line(inertial)

    # origin 仅做数值合法性检查（结构上可选）。
    origin = inertial.find("origin")
    if origin is not None:
        for attr in ("xyz", "rpy"):
            raw = origin.get(attr)
            if raw is not None:
                comps = [p.strip() for p in raw.split()]
                ok = len(comps) == 3
                vals = []
                if ok:
                    for c in comps:
                        v, fok = _finite_float(c)
                        vals.append(v)
                        if not fok:
                            ok = False
                if not ok:
                    issues.append(Issue(
                        Severity.ERROR, Code.ORIGIN_INVALID_NUMERIC,
                        f"link '{lname}' 的 inertial.origin.{attr}='{raw}' "
                        "必须是 3 个有限浮点数。",
                        node=_node_path(origin), attribute=attr,
                        line=_line(origin),
                    ))

    mass_ok = False
    mass = None
    mass_el = inertial.find("mass")
    if mass_el is None:
        issues.append(Issue(
            Severity.ERROR, Code.MASS_MISSING,
            f"link '{lname}' 的 <inertial> 缺少 <mass>。",
            node=ipath, line=iline,
        ))
    else:
        raw = mass_el.get("value")
        if raw is None:
            issues.append(Issue(
                Severity.ERROR, Code.MASS_VALUE_MISSING,
                f"link '{lname}' 的 <mass> 缺少 value 属性。",
                node=_node_path(mass_el), attribute="value",
                line=_line(mass_el),
            ))
        else:
            mass, ok = _finite_float(raw)
            if not ok:
                issues.append(Issue(
                    Severity.ERROR, Code.MASS_INVALID_NUMERIC,
                    f"link '{lname}' 的 mass.value='{raw}' 不是有限数"
                    "（NaN/Inf/不可解析均属数值非法，与缺项不同）。",
                    node=_node_path(mass_el), attribute="value",
                    line=_line(mass_el),
                ))
            elif mass == 0.0:
                issues.append(Issue(
                    Severity.ERROR, Code.MASS_ZERO,
                    f"link '{lname}' 的质量为零；刚体质量必须 > 0。",
                    node=_node_path(mass_el), attribute="value",
                    line=_line(mass_el),
                ))
            elif mass < 0.0:
                issues.append(Issue(
                    Severity.ERROR, Code.MASS_NEGATIVE,
                    f"link '{lname}' 的质量为负（{mass:g}）。",
                    node=_node_path(mass_el), attribute="value",
                    line=_line(mass_el),
                ))
            else:
                mass_ok = True

    inertia_ok = False
    eigvals = None
    inertia_el = inertial.find("inertia")
    if inertia_el is None:
        issues.append(Issue(
            Severity.ERROR, Code.INERTIA_MISSING,
            f"link '{lname}' 的 <inertial> 缺少 <inertia>。",
            node=ipath, line=iline,
        ))
    else:
        comps: dict[str, float] = {}
        numeric_bad = False
        for attr in INERTIA_ATTRS:
            raw = inertia_el.get(attr)
            if raw is None:
                issues.append(Issue(
                    Severity.ERROR, Code.INERTIA_ATTR_MISSING,
                    f"link '{lname}' 的 <inertia> 缺少 {attr} 属性。",
                    node=_node_path(inertia_el), attribute=attr,
                    line=_line(inertia_el),
                ))
                continue
            v, ok = _finite_float(raw)
            if not ok:
                numeric_bad = True
                issues.append(Issue(
                    Severity.ERROR, Code.INERTIA_INVALID_NUMERIC,
                    f"link '{lname}' 的 inertia.{attr}='{raw}' 不是有限数"
                    "（数值非法，与缺项不同）。",
                    node=_node_path(inertia_el), attribute=attr,
                    line=_line(inertia_el),
                ))
                continue
            comps[attr] = v

        if not numeric_bad and len(comps) == 6:
            eigvals = _check_inertia_matrix(lname, inertia_el, comps, tol,
                                            issues)
            inertia_ok = True

    if mass_ok and inertia_ok and eigvals is not None:
        _check_scale(lname, mass_el, mass, eigvals, tol, issues)

    return issues


def _check_inertia_matrix(lname, inertia_el, comps: dict[str, float],
                          tol: dict, issues: list[Issue]):
    """返回升序主转动惯量（特征值）；矩阵非法时返回 None。"""
    I = np.array([
        [comps["ixx"], comps["ixy"], comps["ixz"]],
        [comps["ixy"], comps["iyy"], comps["iyz"]],
        [comps["ixz"], comps["iyz"], comps["izz"]],
    ], dtype=float)
    ipath = _node_path(inertia_el)
    line = _line(inertia_el)

    # URDF 用 6 个标量描述对称张量（Ixy=Iyx 等），此处用共享分量构阵，
    # 构造结果天然对称；再做一次对称化消除理论上的舍入不对称。
    S = (I + I.T) / 2.0
    if not np.allclose(I, S, atol=tol["eig_abs"], rtol=tol["eig_rel"]):
        issues.append(Issue(
            Severity.ERROR, Code.INERTIA_NOT_SYMMETRIC,
            f"link '{lname}' 的惯量矩阵不对称。",
            node=ipath, line=line,
        ))
        return None

    eigvals = np.sort(np.linalg.eigvalsh(S))
    eig_min = float(eigvals[0])
    eig_max = float(eigvals[-1])
    scale = max(abs(eig_max) * tol["eig_rel"], tol["eig_abs"])

    if eig_min < -scale:
        issues.append(Issue(
            Severity.ERROR, Code.INERTIA_NOT_POSITIVE_DEFINITE,
            f"link '{lname}' 的惯量矩阵非正定：特征值 "
            f"{_fmt_eig(eigvals)}，最小值 {eig_min:.6g} 超出容差 ±{scale:.3g}。",
            node=ipath, line=line,
        ))
        return None

    # 近半正定告警：最小主惯量小于最大主惯量的 semi_warn_rel 比例
    # （含恰好为 0 的边界与容差内的小负值）。零主惯量只对理想细杆成立。
    warn_scale = max(abs(eig_max) * tol["semi_warn_rel"], tol["eig_abs"])
    if eig_min <= warn_scale:
        issues.append(Issue(
            Severity.WARNING, Code.INERTIA_NEAR_SEMIDEFINITE,
            f"link '{lname}' 的惯量矩阵接近半正定：最小特征值 "
            f"{eig_min:.6g}（告警阈值 {warn_scale:.3g}）；零主惯量只对"
            "理想细杆成立，请确认是否建模误差。",
            node=ipath, line=line,
        ))

    # 三角不等式作用于升序主转动惯量：只需验证最大的 I3 <= I1 + I2。
    i1, i2, i3 = (float(v) for v in eigvals)
    tri_scale = max(i3 * tol["tri_viol"], tol["eig_abs"])
    if i3 - (i1 + i2) > tri_scale:
        issues.append(Issue(
            Severity.ERROR, Code.INERTIA_TRIANGLE_VIOLATION,
            f"link '{lname}' 的主转动惯量 {_fmt_eig(eigvals)} 违反三角"
            f"不等式：{i3:.6g} > {i1:.6g} + {i2:.6g}"
            f"（超出容差 {tri_scale:.3g}）。",
            node=ipath, line=line,
        ))

    return eigvals


def _check_scale(lname, mass_el, mass: float, eigvals, tol: dict,
                 issues: list[Issue]) -> None:
    """用等效回转半径 r = sqrt(trace(I)/(3m)) 探测单位/量级错误。"""
    trace = float(np.sum(eigvals))
    if trace <= 0.0:
        return
    r = math.sqrt(trace / (3.0 * mass))
    if r < tol["radius_min_m"]:
        issues.append(Issue(
            Severity.WARNING, Code.INERTIA_SCALE_ANOMALY,
            f"link '{lname}' 等效回转半径 {r:.3g} m 小于 "
            f"{tol['radius_min_m']:g} m：惯量相对质量过小，"
            "可能把 kg·m² 误写成 g·m² 或 mm 单位未换算。",
            node=_node_path(mass_el), attribute="value",
            line=_line(mass_el),
        ))
    elif r > tol["radius_max_m"]:
        issues.append(Issue(
            Severity.WARNING, Code.INERTIA_SCALE_ANOMALY,
            f"link '{lname}' 等效回转半径 {r:.3g} m 大于 "
            f"{tol['radius_max_m']:g} m：惯量相对质量过大，"
            "可能质量单位用了 g 而惯量按 kg·m² 填写，或长度用了 mm。",
            node=_node_path(mass_el), attribute="value",
            line=_line(mass_el),
        ))


def _fmt_eig(vals) -> str:
    return "[" + ", ".join(f"{float(v):.6g}" for v in vals) + "]"
