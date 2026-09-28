"""核心层：数据结构、epsilon 语义、环检测。"""

from wfst.core.cycles import (
    can_reach_final,
    negative_cycle_on_accepting_paths,
    reachable_from_start,
    reachable_input_epsilon_cycle,
)
from wfst.core.fst import (
    EPSILON,
    INFINITY,
    Arc,
    ArcKind,
    AlignmentError,
    CompositionError,
    FST,
    FinalState,
    NegativeCycleError,
)

__all__ = [
    "EPSILON",
    "INFINITY",
    "Arc",
    "ArcKind",
    "AlignmentError",
    "CompositionError",
    "FST",
    "FinalState",
    "NegativeCycleError",
    "can_reach_final",
    "negative_cycle_on_accepting_paths",
    "reachable_from_start",
    "reachable_input_epsilon_cycle",
]
