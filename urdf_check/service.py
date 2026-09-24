"""检查编排:安全解析 → 建模 → 全部检查项 → 汇总报告。"""

from __future__ import annotations

from . import checks
from .checks import CheckConfig
from .diagnostics import Diagnostic, Report, Severity
from .model import build_model
from .safe_xml import UnsafeXMLError, URDFSyntaxError, parse_urdf

__all__ = ["CheckConfig", "check_urdf"]


def check_urdf(source: bytes, config: CheckConfig | None = None) -> Report:
    """检查一段 URDF 文本,返回结构化报告(绝不抛出解析异常)。"""
    cfg = config or CheckConfig()
    report = Report()

    try:
        root = parse_urdf(source)
    except UnsafeXMLError as exc:
        report.diagnostics.append(
            Diagnostic(Severity.ERROR.value, exc.code, exc.message,
                       node="/", attribute=None, line=1)
        )
        return report
    except URDFSyntaxError as exc:
        report.diagnostics.append(
            Diagnostic(Severity.ERROR.value, "XML_SYNTAX_ERROR", exc.message,
                       node="/", attribute=None, line=exc.line)
        )
        return report

    report.robot_name = root.get("name", "")

    build_diags: list = []
    model = build_model(root, build_diags)
    report.link_count = len(model.links)
    report.joint_count = len(model.joints)

    report.diagnostics.extend(build_diags)
    report.diagnostics.extend(checks.check_unique_names(model))
    report.diagnostics.extend(checks.check_references(model))
    report.diagnostics.extend(checks.check_tree(model))
    report.diagnostics.extend(checks.check_axes(model, cfg))
    report.diagnostics.extend(checks.check_limits(model, cfg))
    report.diagnostics.extend(checks.check_inertials(model, cfg))
    return report
