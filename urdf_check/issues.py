"""检查结果模型：每条问题可定位到具体节点与属性。"""

from __future__ import annotations

from dataclasses import dataclass
from enum import Enum


class Severity(str, Enum):
    ERROR = "error"
    WARNING = "warning"


class Code:
    # --- 解析 / 安全 ---
    XML_PARSE_ERROR = "XML_PARSE_ERROR"
    DOCTYPE_FORBIDDEN = "DOCTYPE_FORBIDDEN"          # 禁止 DOCTYPE（实体声明入口，含 XXE）
    ENTITY_FORBIDDEN = "ENTITY_FORBIDDEN"            # 禁止 <!ENTITY ...>
    XACRO_FORBIDDEN = "XACRO_FORBIDDEN"              # 禁止 xacro 宏，需离线预展开
    # --- 文档结构 ---
    ROOT_NOT_ROBOT = "ROOT_NOT_ROBOT"
    ROBOT_NAME_MISSING = "ROBOT_NAME_MISSING"
    LINK_NAME_MISSING = "LINK_NAME_MISSING"
    JOINT_NAME_MISSING = "JOINT_NAME_MISSING"
    DUPLICATE_LINK = "DUPLICATE_LINK"
    DUPLICATE_JOINT = "DUPLICATE_JOINT"
    UNKNOWN_JOINT_TYPE = "UNKNOWN_JOINT_TYPE"
    JOINT_TYPE_MISSING = "JOINT_TYPE_MISSING"
    JOINT_PARENT_MISSING = "JOINT_PARENT_MISSING"
    JOINT_CHILD_MISSING = "JOINT_CHILD_MISSING"
    JOINT_PARENT_UNKNOWN = "JOINT_PARENT_UNKNOWN"
    JOINT_CHILD_UNKNOWN = "JOINT_CHILD_UNKNOWN"
    JOINT_SELF_LOOP = "JOINT_SELF_LOOP"
    TREE_MULTIPLE_ROOTS = "TREE_MULTIPLE_ROOTS"      # 多棵不连通树
    TREE_CYCLE = "TREE_CYCLE"                        # 循环（固定关节也不允许）
    # --- 关节轴 / 限位 ---
    AXIS_ZERO_NORM = "AXIS_ZERO_NORM"
    AXIS_NOT_NORMALIZED = "AXIS_NOT_NORMALIZED"
    AXIS_INVALID_NUMERIC = "AXIS_INVALID_NUMERIC"
    LIMIT_MISSING = "LIMIT_MISSING"
    LIMIT_ATTR_MISSING = "LIMIT_ATTR_MISSING"
    LIMIT_INVALID_NUMERIC = "LIMIT_INVALID_NUMERIC"
    LIMIT_LOWER_GT_UPPER = "LIMIT_LOWER_GT_UPPER"
    # --- 惯性 ---
    INERTIAL_MISSING = "INERTIAL_MISSING"            # 缺少整个 <inertial>
    MASS_MISSING = "MASS_MISSING"                    # 缺少 <mass>
    MASS_VALUE_MISSING = "MASS_VALUE_MISSING"        # 缺少 mass 的 value 属性
    MASS_INVALID_NUMERIC = "MASS_INVALID_NUMERIC"    # 非有限数
    MASS_ZERO = "MASS_ZERO"                          # 质量为零
    MASS_NEGATIVE = "MASS_NEGATIVE"
    INERTIA_MISSING = "INERTIA_MISSING"              # 缺少 <inertia>
    INERTIA_ATTR_MISSING = "INERTIA_ATTR_MISSING"    # 缺少六分量中的某个属性
    INERTIA_INVALID_NUMERIC = "INERTIA_INVALID_NUMERIC"
    INERTIA_NOT_SYMMETRIC = "INERTIA_NOT_SYMMETRIC"
    INERTIA_NOT_POSITIVE_DEFINITE = "INERTIA_NOT_POSITIVE_DEFINITE"
    INERTIA_NEAR_SEMIDEFINITE = "INERTIA_NEAR_SEMIDEFINITE"
    INERTIA_TRIANGLE_VIOLATION = "INERTIA_TRIANGLE_VIOLATION"
    INERTIA_SCALE_ANOMALY = "INERTIA_SCALE_ANOMALY"  # 单位/量级异常告警
    ORIGIN_INVALID_NUMERIC = "ORIGIN_INVALID_NUMERIC"


@dataclass
class Issue:
    severity: str
    code: str
    message: str
    node: str = ""          # 节点路径（带 name），例如 /robot/link[@name='arm']/inertial
    attribute: str = ""     # 相关属性，例如 "value"、"ixx"、"xyz"
    line: int = 0           # XML 源行号；预扫描问题为 0

    def to_dict(self) -> dict:
        return {
            "severity": self.severity,
            "code": self.code,
            "message": self.message,
            "node": self.node,
            "attribute": self.attribute,
            "line": self.line,
        }
