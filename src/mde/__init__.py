"""mde — 最小披露记录导出（minimum-disclosure export）纯后端服务。

本包只提供字段级导出策略引擎、可核验决策记录与本地密码学工具，
不提供任何匿名化保证。
"""

__version__ = "1.0.0"

PACKAGE_FORMAT = "mde/export-package@v1"
OUTPUT_SCHEMA = "mde/output@v1"
DECISION_SCHEMA = "mde/decision-record@v1"
POLICY_SCHEMA = "mde/policy@v1"
