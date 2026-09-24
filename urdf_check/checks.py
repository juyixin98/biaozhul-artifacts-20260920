"""各项结构与物理参数检查。

检查项:
  * 名称唯一性(link/joint 重名);
  * joint 的 parent/child 引用必须存在;
  * 运动学树:单根、连通、无环(固定关节合法,环不容忍);
  * 关节轴归一化(零向量报错,非单位向量告警);
  * 限位上下界(revolute/prismatic 必须有 limit,lower <= upper,
    effort/velocity 必须为非负有限数);
  * 惯性:缺失(单独告警类别)与数值非法(错误)分开;
    质量为正、惯性矩阵对称正定、满足三角不等式;
  * 单位量级异常(质量/惯量超出常见量级时告警)。
"""

from __future__ import annotations

import math
from dataclasses import dataclass

import numpy as np

from .diagnostics import Diagnostic, Severity
from .model import Joint, Link, RobotModel, node_path, parse_float

_ACTUATED_TYPES = {"revolute", "prismatic", "continuous"}
_LIMITED_TYPES = {"revolute", "prismatic"}


@dataclass
class CheckConfig:
    """容差与量级阈值(均可通过 CLI/HTTP 参数覆盖)。"""

    axis_norm_tol: float = 1e-6       # 轴向量模长与 1 的允许偏差
    symmetry_atol: float = 1e-9       # 惯性矩阵对称性绝对容差
    psd_atol: float = 1e-9            # 正定性:最小特征值允许的下探(绝对)
    triangle_rtol: float = 1e-9       # 三角不等式相对容差(乘矩阵量级)
    mass_warn_min: float = 1e-6       # 质量量级告警下限 (kg)
    mass_warn_max: float = 1e4        # 质量量级告警上限 (kg)
    inertia_warn_min: float = 1e-12   # 惯量量级告警下限 (kg·m²)
    inertia_warn_max: float = 1e3     # 惯量量级告警上限 (kg·m²)


def check_unique_names(model: RobotModel) -> list:
    """link/joint 重名检查,定位到重复节点的 name 属性。"""
    diags = []
    for kind, elements in (
        ("link", [(l.name, l.element) for l in model.links]),
        ("joint", [(j.name, j.element) for j in model.joints]),
    ):
        seen = {}
        for name, el in elements:
            if name in seen:
                diags.append(
                    Diagnostic(
                        Severity.ERROR.value,
                        f"DUPLICATE_{kind.upper()}_NAME",
                        f"{kind} 名称 {name!r} 重复(首次出现于第 "
                        f"{seen[name].sourceline} 行)",
                        node=node_path(el),
                        attribute="name",
                        line=el.sourceline,
                    )
                )
            else:
                seen[name] = el
    return diags


def check_references(model: RobotModel) -> list:
    """joint 的 parent/child 必须引用已定义的 link。"""
    diags = []
    link_names = {l.name for l in model.links}
    for joint in model.joints:
        for role, ref in (("parent", joint.parent), ("child", joint.child)):
            if ref is not None and ref not in link_names:
                diags.append(
                    Diagnostic(
                        Severity.ERROR.value,
                        "JOINT_UNKNOWN_LINK",
                        f"joint '{joint.name}' 的 {role} 引用了未定义的 "
                        f"link {ref!r}",
                        node=node_path(joint.element),
                        attribute="link",
                        line=joint.element.sourceline,
                    )
                )
    return diags


def check_tree(model: RobotModel) -> list:
    """运动学树:恰好一个根、全连通、无环。固定关节照常参与连边。"""
    diags = []
    if not model.links:
        diags.append(
            Diagnostic(
                Severity.ERROR.value,
                "NO_LINKS",
                "robot 中没有任何 link",
                node="/robot",
                attribute=None,
                line=1,
            )
        )
        return diags

    link_names = {l.name for l in model.links}
    # 每个 link 至多是一个 joint 的 child
    child_to_joints: dict = {}
    for joint in model.joints:
        if joint.child in link_names:
            child_to_joints.setdefault(joint.child, []).append(joint)
    for child, joints in child_to_joints.items():
        if len(joints) > 1:
            names = [j.name for j in joints]
            diags.append(
                Diagnostic(
                    Severity.ERROR.value,
                    "MULTIPLE_PARENTS",
                    f"link '{child}' 同时是多个 joint 的 child: {names}",
                    node=node_path(joints[1].element),
                    attribute="link",
                    line=joints[1].element.sourceline,
                )
            )

    roots = sorted(link_names - set(child_to_joints))
    if not roots:
        diags.append(
            Diagnostic(
                Severity.ERROR.value,
                "NO_ROOT_LINK",
                "没有根 link(每个 link 都是某 joint 的 child),"
                "结构必然含环",
                node="/robot",
                attribute=None,
                line=1,
            )
        )
    elif len(roots) > 1:
        diags.append(
            Diagnostic(
                Severity.ERROR.value,
                "MULTIPLE_ROOTS",
                f"存在 {len(roots)} 个根 link {roots},"
                "运动学结构必须是单棵树",
                node="/robot",
                attribute=None,
                line=1,
            )
        )

    # Kahn 拓扑遍历:从各根沿 parent->child 边走,走不到的即环/断链
    adjacency: dict = {}
    for joint in model.joints:
        if joint.parent in link_names and joint.child in link_names:
            adjacency.setdefault(joint.parent, []).append(joint)
    visited = set()
    stack = list(roots)
    while stack:
        current = stack.pop()
        if current in visited:
            continue
        visited.add(current)
        for joint in adjacency.get(current, []):
            stack.append(joint.child)
    unreached = sorted(link_names - visited)
    if unreached and roots:
        # 有根却走不到:这些 link 处于环上或环的下游
        culprit = next(
            (j for j in model.joints if j.child in unreached), None
        )
        diags.append(
            Diagnostic(
                Severity.ERROR.value,
                "KINEMATIC_CYCLE",
                f"检测到运动学环或断链,不可达 link: {unreached}"
                "(固定关节允许,环不容忍)",
                node=node_path(culprit.element) if culprit is not None else "/robot",
                attribute="link" if culprit is not None else None,
                line=culprit.element.sourceline if culprit is not None else 1,
            )
        )
    return diags


def check_axes(model: RobotModel, cfg: CheckConfig) -> list:
    """关节轴:零向量报错;非单位向量告警(URDF 默认轴为 1 0 0)。"""
    diags = []
    for joint in model.joints:
        if joint.type not in _ACTUATED_TYPES:
            continue
        if joint.axis_element is None:
            continue  # 未显式给出,采用 URDF 默认轴 (1,0,0),无需检查
        if joint.axis is None:
            continue  # 数值非法已在模型构建时记录
        norm = math.sqrt(sum(c * c for c in joint.axis))
        el = joint.axis_element
        if norm <= cfg.axis_norm_tol:
            diags.append(
                Diagnostic(
                    Severity.ERROR.value,
                    "AXIS_ZERO",
                    f"joint '{joint.name}' 的轴向量接近零向量 "
                    f"(norm={norm:.3e}),无法定义旋转/平移方向",
                    node=node_path(el),
                    attribute="xyz",
                    line=el.sourceline,
                )
            )
        elif abs(norm - 1.0) > cfg.axis_norm_tol:
            diags.append(
                Diagnostic(
                    Severity.WARNING.value,
                    "AXIS_NOT_NORMALIZED",
                    f"joint '{joint.name}' 的轴向量未归一化 "
                    f"(norm={norm:.6g},容差 {cfg.axis_norm_tol:g})",
                    node=node_path(el),
                    attribute="xyz",
                    line=el.sourceline,
                )
            )
    return diags


def check_limits(model: RobotModel, cfg: CheckConfig) -> list:
    """限位检查:revolute/prismatic 必须有 limit,lower <= upper,
    effort/velocity 为非负有限数。"""
    diags = []
    for joint in model.joints:
        if joint.type not in _LIMITED_TYPES:
            continue
        el = joint.limit_element
        if el is None:
            diags.append(
                Diagnostic(
                    Severity.ERROR.value,
                    "MISSING_LIMIT",
                    f"joint '{joint.name}'(type={joint.type})"
                    "缺少 <limit> 子元素",
                    node=node_path(joint.element),
                    attribute=None,
                    line=joint.element.sourceline,
                )
            )
            continue
        lower = parse_float(el, "lower", cfg_list := [], required=False)
        upper = parse_float(el, "upper", cfg_list, required=False)
        diags.extend(cfg_list)
        if lower is not None and upper is not None and lower > upper:
            diags.append(
                Diagnostic(
                    Severity.ERROR.value,
                    "LIMIT_BOUNDS_INVERTED",
                    f"joint '{joint.name}' 限位下界 lower={lower} "
                    f"大于上界 upper={upper}",
                    node=node_path(el),
                    attribute="lower",
                    line=el.sourceline,
                )
            )
        scratch: list = []
        effort = parse_float(el, "effort", scratch)
        velocity = parse_float(el, "velocity", scratch)
        diags.extend(scratch)
        for attr, value in (("effort", effort), ("velocity", velocity)):
            if value is not None and value < 0:
                diags.append(
                    Diagnostic(
                        Severity.ERROR.value,
                        "LIMIT_NEGATIVE",
                        f"joint '{joint.name}' 的 {attr}={value} 为负,"
                        "必须是非负有限数",
                        node=node_path(el),
                        attribute=attr,
                        line=el.sourceline,
                    )
                )
    return diags


def _check_inertia_matrix(
    link: Link, matrix: np.ndarray, cfg: CheckConfig
) -> list:
    """惯性张量:对称、正定、三角不等式、量级。"""
    diags = []
    el = link.inertial.element.find("inertia")
    path = node_path(el)
    line = el.sourceline

    # 对称性(URDF 只给 6 个分量,构造时必然对称;此处仍真实校验)
    asym = float(np.max(np.abs(matrix - matrix.T)))
    if asym > cfg.symmetry_atol:
        diags.append(
            Diagnostic(
                Severity.ERROR.value,
                "INERTIA_NOT_SYMMETRIC",
                f"link '{link.name}' 的惯性矩阵不对称,"
                f"最大偏差 {asym:.3e}",
                node=path,
                attribute="ixy",
                line=line,
            )
        )

    # 正定性:对称矩阵特征值必须全为正(允许 psd_atol 的浮点下探)
    eigvals = np.linalg.eigvalsh(matrix)
    min_eig = float(eigvals[0])
    if min_eig < -cfg.psd_atol:
        diags.append(
            Diagnostic(
                Severity.ERROR.value,
                "INERTIA_NOT_POSITIVE_DEFINITE",
                f"link '{link.name}' 的惯性矩阵非正定,"
                f"最小特征值 {min_eig:.6e} < -{cfg.psd_atol:g}",
                node=path,
                attribute="ixx",
                line=line,
            )
        )
    elif min_eig <= cfg.psd_atol:
        diags.append(
            Diagnostic(
                Severity.WARNING.value,
                "INERTIA_NEAR_SINGULAR",
                f"link '{link.name}' 的惯性矩阵接近半正定边界,"
                f"最小特征值 {min_eig:.6e}",
                node=path,
                attribute="ixx",
                line=line,
            )
        )

    # 三角不等式:ixx+iyy>=izz, iyy+izz>=ixx, izz+ixx>=iyy
    # (对主对角元与主惯量特征值分别校验)
    ixx, iyy, izz = matrix[0, 0], matrix[1, 1], matrix[2, 2]
    scale = max(1.0, abs(ixx), abs(iyy), abs(izz))
    tol = cfg.triangle_rtol * scale
    for a, b, c, ca, cb, cc in (
        (ixx, iyy, izz, "ixx", "iyy", "izz"),
        (iyy, izz, ixx, "iyy", "izz", "ixx"),
        (izz, ixx, iyy, "izz", "ixx", "iyy"),
    ):
        violation = c - (a + b)
        if violation > tol:
            diags.append(
                Diagnostic(
                    Severity.ERROR.value,
                    "INERTIA_TRIANGLE_VIOLATION",
                    f"link '{link.name}' 违反惯性三角不等式: "
                    f"{ca}+{cb}={a + b:.6g} < {cc}={c:.6g} "
                    f"(超出 {violation:.3e},容差 {tol:.3e})",
                    node=path,
                    attribute=cc,
                    line=line,
                )
            )
    # 主惯量(特征值)同样必须满足三角不等式
    e = [float(v) for v in eigvals]
    escale = max(1.0, *(abs(v) for v in e))
    etol = cfg.triangle_rtol * escale
    for i in range(3):
        a, b = e[(i + 1) % 3], e[(i + 2) % 3]
        if e[i] - (a + b) > etol:
            diags.append(
                Diagnostic(
                    Severity.ERROR.value,
                    "INERTIA_TRIANGLE_VIOLATION",
                    f"link '{link.name}' 的主惯量违反三角不等式: "
                    f"λ{i + 1}={e[i]:.6g} > 其余两主惯量之和 {a + b:.6g}",
                    node=path,
                    attribute="ixx",
                    line=line,
                )
            )
            break

    # 单位量级异常:非零分量超出常见量级时告警
    for attr, value in (
        ("ixx", ixx), ("iyy", iyy), ("izz", izz),
        ("ixy", matrix[0, 1]), ("ixz", matrix[0, 2]), ("iyz", matrix[1, 2]),
    ):
        av = abs(value)
        if av > cfg.inertia_warn_max or (0 < av < cfg.inertia_warn_min):
            diags.append(
                Diagnostic(
                    Severity.WARNING.value,
                    "INERTIA_MAGNITUDE_UNUSUAL",
                    f"link '{link.name}' 的 {attr}={value:.6g} kg·m² "
                    f"超出常见量级 [{cfg.inertia_warn_min:g}, "
                    f"{cfg.inertia_warn_max:g}],请确认单位是 kg·m²",
                    node=path,
                    attribute=attr,
                    line=line,
                )
            )
    return diags


def check_inertials(model: RobotModel, cfg: CheckConfig) -> list:
    """惯性检查:缺失(告警,独立类别)与数值非法(错误)严格分开。"""
    diags = []
    for link in model.links:
        if link.inertial is None:
            diags.append(
                Diagnostic(
                    Severity.WARNING.value,
                    "MISSING_INERTIAL",
                    f"link '{link.name}' 缺少 <inertial>"
                    "(缺失惯性,与数值非法分开统计)",
                    node=node_path(link.element),
                    attribute=None,
                    line=link.element.sourceline,
                )
            )
            continue
        inertial = link.inertial
        mass_el = inertial.element.find("mass")
        if inertial.mass is not None:
            if inertial.mass < 0:
                diags.append(
                    Diagnostic(
                        Severity.ERROR.value,
                        "MASS_NEGATIVE",
                        f"link '{link.name}' 的质量为负: {inertial.mass}",
                        node=node_path(mass_el),
                        attribute="value",
                        line=mass_el.sourceline,
                    )
                )
            elif inertial.mass == 0.0:
                diags.append(
                    Diagnostic(
                        Severity.ERROR.value,
                        "MASS_ZERO",
                        f"link '{link.name}' 的质量为零"
                        "(若确需零质量虚链接,请省略整个 <inertial>)",
                        node=node_path(mass_el),
                        attribute="value",
                        line=mass_el.sourceline,
                    )
                )
            elif (
                inertial.mass < cfg.mass_warn_min
                or inertial.mass > cfg.mass_warn_max
            ):
                diags.append(
                    Diagnostic(
                        Severity.WARNING.value,
                        "MASS_MAGNITUDE_UNUSUAL",
                        f"link '{link.name}' 的质量 {inertial.mass:.6g} kg "
                        f"超出常见量级 [{cfg.mass_warn_min:g}, "
                        f"{cfg.mass_warn_max:g}],请确认单位是 kg",
                        node=node_path(mass_el),
                        attribute="value",
                        line=mass_el.sourceline,
                    )
                )
        if inertial.matrix is not None:
            diags.extend(_check_inertia_matrix(link, inertial.matrix, cfg))
    return diags
