"""Tensor memory reuse planner (static DAG, known shapes).

Public API:
    build_dag          - construct + validate a DAG from a request dict
    plan_memory        - run liveness analysis and buffer reuse allocation
    execute            - run a DAG with the no-reuse or reuse executor
    service_request    - end-to-end entry point used by the local service / CLI
"""

from .dag import DAG, Node, build_dag
from .planner import (
    AllocationError,
    BudgetExceeded,
    Lifetime,
    MemoryPlan,
    plan_memory,
)
from .executor import execute, ExecutionResult

__all__ = [
    "DAG",
    "Node",
    "build_dag",
    "plan_memory",
    "MemoryPlan",
    "Lifetime",
    "AllocationError",
    "BudgetExceeded",
    "execute",
    "ExecutionResult",
    "service_request",
]


def service_request(request: dict) -> dict:
    """End-to-end: build a DAG from a request, plan, execute both executors.

    See examples/ for the request schema. Returns a JSON-serialisable report.
    """
    from .service import handle_request

    return handle_request(request)
