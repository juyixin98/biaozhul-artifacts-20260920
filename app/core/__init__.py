"""Core package: ATMS reasoning kernel."""

from .engine import ATMS
from .types import CONTRADICTION, Environment, OpResult, Rule

__all__ = ["ATMS", "CONTRADICTION", "Environment", "OpResult", "Rule"]
