"""最小披露记录导出（Minimal Disclosure Export, mde）。

纯后端字段级导出策略引擎：按用途（purpose）对导出数据逐字段执行
允许 / 拒绝 / 泛化决策，策略版本固定到导出任务，并输出可核验的
字段决策记录（Ed25519 签名 + SHA-256 摘要）。

注意：本项目实施的是**披露策略**，不提供、也不声称任何匿名化保证。
"""

__version__ = "0.1.0"

from .models import Policy, Rule
from .policy import PolicyStore, FilePolicyStore, InMemoryPolicyStore
from .engine import apply_policy, ExportResult
from .service import ExportService
from . import crypto, generalizers

__all__ = [
    "__version__",
    "Policy",
    "Rule",
    "PolicyStore",
    "FilePolicyStore",
    "InMemoryPolicyStore",
    "apply_policy",
    "ExportResult",
    "ExportService",
    "crypto",
    "generalizers",
]
