"""Mining/maths kernel: weighted finite-state transducers.

This package is deliberately independent of the HTTP and SQLite layers:
it operates only on immutable in-memory objects and plain value types, which
makes it exhaustively testable against an independent oracle.
"""

from .fst import Arc, Fst
from .compose import compose, CompositionTrace
from .cycles import analyze_cycles, CycleReport
from .search import kbest, KBestResult, SearchTrace
from .errors import CoreError, BudgetExhausted

__all__ = [
    "Arc",
    "Fst",
    "from_normalized_spec",
    "compose",
    "CompositionTrace",
    "analyze_cycles",
    "CycleReport",
    "kbest",
    "KBestResult",
    "SearchTrace",
    "CoreError",
    "BudgetExhausted",
]
