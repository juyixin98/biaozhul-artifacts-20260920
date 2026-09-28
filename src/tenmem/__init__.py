"""tenmem: constrained tensor graph memory-reuse planner and executor."""

from .errors import (
    ComputationError,
    GraphValidationError,
    PlanningError,
    ReplanningRequiredError,
    ResourceExhaustedError,
    StateConflictError,
    TenmemError,
)
from .executor import Session, execute, execute_no_reuse
from .graph import Graph, Node, Schedule, validate_and_schedule
from .planner.memory import Plan, plan_memory
from .state import Checkpoint, TrainingState
from .tensor import Buffer, DimBound, TensorSpec, align_up

__all__ = [
    "TenmemError",
    "GraphValidationError",
    "PlanningError",
    "ReplanningRequiredError",
    "StateConflictError",
    "ResourceExhaustedError",
    "ComputationError",
    "Graph",
    "Node",
    "Schedule",
    "validate_and_schedule",
    "TensorSpec",
    "DimBound",
    "Buffer",
    "align_up",
    "Plan",
    "plan_memory",
    "Session",
    "execute",
    "execute_no_reuse",
    "TrainingState",
    "Checkpoint",
]
