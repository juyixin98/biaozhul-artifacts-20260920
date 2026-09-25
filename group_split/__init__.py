"""Group-constrained stratified dataset splitting.

Public API:
    split_groups  -- split samples into named splits keeping groups intact
    SplitResult   -- result container (assignment, indices, deviation report)
"""

from group_split.splitter import DEFAULT_TOLERANCE, SplitResult, split_groups

__all__ = ["split_groups", "SplitResult", "DEFAULT_TOLERANCE"]
__version__ = "0.1.0"
