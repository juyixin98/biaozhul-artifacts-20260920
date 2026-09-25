"""Offline fixed-sample-size A/B metric analysis toolkit.

Scope (explicit):
- Fixed sample size, offline (post-hoc) analysis only.
- Welch two-sample confidence interval for the difference of means.
- NOT valid for sequential peeking / optional stopping. See README.
"""

from .stats import AnalysisResult, welch_mean_diff_ci
from .missing import MissingStrategy, apply_missing_strategy

__all__ = [
    "AnalysisResult",
    "welch_mean_diff_ci",
    "MissingStrategy",
    "apply_missing_strategy",
]

__version__ = "0.1.0"
