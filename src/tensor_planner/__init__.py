"""张量内存复用规划（静态 DAG，仅支持已知形状的 add / matmul / slice）。"""

from .dag import DAG, DAGBuilder, Op
from .liveness import LiveInterval, storage_intervals, value_intervals
from .planner import Placement, Plan, build_plan
from .executor import NaiveExecutor, ReuseExecutor
from .verifier import Verification, verify_plan, outputs_max_abs_diff
from .api import handle_request

__all__ = [
    "DAG",
    "DAGBuilder",
    "Op",
    "LiveInterval",
    "value_intervals",
    "storage_intervals",
    "Placement",
    "Plan",
    "build_plan",
    "NaiveExecutor",
    "ReuseExecutor",
    "Verification",
    "verify_plan",
    "outputs_max_abs_diff",
    "handle_request",
]
