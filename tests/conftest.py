"""pytest 共享辅助：生成 URDF 文本、按 code 收集问题。"""

from urdf_check import inspect_urdf_bytes
from urdf_check.issues import Severity


def check(xml: str, tol: dict | None = None):
    return inspect_urdf_bytes(xml, tol=tol)


def codes(report, severity: str | None = None) -> list[str]:
    return [i.code for i in report.issues
            if severity is None or i.severity == severity]


def has(report, code: str) -> bool:
    return code in codes(report)


def find_issue(report, code: str):
    return next(i for i in report.issues if i.code == code)


def inertia_block(mass="1.0", ixx="0.01", ixy="0", ixz="0",
                  iyy="0.01", iyz="0", izz="0.01",
                  inertial: bool = True, mass_tag: bool = True,
                  inertia_tag: bool = True):
    if not inertial:
        return ""
    parts = ["    <inertial>"]
    if mass_tag:
        parts.append(f'      <mass value="{mass}"/>')
    if inertia_tag:
        parts.append(
            f'      <inertia ixx="{ixx}" ixy="{ixy}" ixz="{ixz}" '
            f'iyy="{iyy}" iyz="{iyz}" izz="{izz}"/>')
    parts.append("    </inertial>")
    return "\n".join(parts)


def single_link_urdf(name="L", body: str | None = None, robot="r") -> str:
    if body is None:
        body = inertia_block()
    return f"""<?xml version="1.0"?>
<robot name="{robot}">
  <link name="{name}">
{body}
  </link>
</robot>
"""


def chain_urdf(links, joints, robot="chain") -> str:
    """links: list[str]；joints: list[(jname, jtype, parent, child, extra)]。"""
    out = ['<?xml version="1.0"?>', f'<robot name="{robot}">']
    for ln in links:
        out.append(f"  <link name='{ln}'>")
        out.append(inertia_block())
        out.append("  </link>")
    for jname, jtype, p, c, extra in joints:
        out.append(f"  <joint name='{jname}' type='{jtype}'>")
        out.append(f"    <parent link='{p}'/><child link='{c}'/>{extra}")
        out.append("  </joint>")
    out.append("</robot>")
    return "\n".join(out)


LIMIT = '<limit lower="-1" upper="1" effort="5" velocity="1"/>'
REV = ("revolute", LIMIT)


def rev_joint(jname, p, c, axis='<axis xyz="0 0 1"/>'):
    return (jname, "revolute", p, c, axis + LIMIT)
