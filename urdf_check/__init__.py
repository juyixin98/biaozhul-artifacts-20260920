"""URDF 惯性与结构离线检查服务.

纯后端库:安全解析 URDF(禁止外部实体与宏执行),检查 link/joint 引用、
树结构、关节轴归一化、限位上下界、惯性矩阵对称正定与三角不等式。
"""

from .diagnostics import Diagnostic, Report, Severity
from .service import CheckConfig, check_urdf

__all__ = [
    "CheckConfig",
    "Diagnostic",
    "Report",
    "Severity",
    "check_urdf",
]

__version__ = "1.0.0"
