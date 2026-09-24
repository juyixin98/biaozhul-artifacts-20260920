"""诊断条目与检查报告的数据结构."""

from __future__ import annotations

from dataclasses import asdict, dataclass, field
from enum import Enum
from typing import Optional


class Severity(str, Enum):
    """诊断严重级别。"""

    ERROR = "error"
    WARNING = "warning"


@dataclass
class Diagnostic:
    """单条诊断,定位到节点与属性。

    node:      节点的 XPath(如 /robot/link[@name='a']/inertial/inertia 的位置路径)。
    attribute: 出问题的属性名(如 "ixx"、"lower");元素级问题为 None。
    line:      源文件行号(由 lxml 提供,未知时为 None)。
    """

    severity: str
    code: str
    message: str
    node: Optional[str] = None
    attribute: Optional[str] = None
    line: Optional[int] = None

    def to_dict(self) -> dict:
        return asdict(self)


@dataclass
class Report:
    """一次检查的完整结果。"""

    diagnostics: list = field(default_factory=list)
    robot_name: Optional[str] = None
    link_count: int = 0
    joint_count: int = 0

    @property
    def errors(self) -> list:
        return [d for d in self.diagnostics if d.severity == Severity.ERROR.value]

    @property
    def warnings(self) -> list:
        return [d for d in self.diagnostics if d.severity == Severity.WARNING.value]

    @property
    def ok(self) -> bool:
        """无 error 即通过(warning 不阻断)。"""
        return not self.errors

    def to_dict(self) -> dict:
        return {
            "ok": self.ok,
            "robot_name": self.robot_name,
            "link_count": self.link_count,
            "joint_count": self.joint_count,
            "error_count": len(self.errors),
            "warning_count": len(self.warnings),
            "diagnostics": [d.to_dict() for d in self.diagnostics],
        }
