"""abseq: fixed-horizon offline A/B metric analysis and sequential-bias simulation.

This package implements a local, NumPy-only toolkit for analyzing A/B
experiment metrics under a *fixed sample size* design. It deliberately does
NOT provide any valid inference for "peek and stop early" workflows; the
simulation module exists to quantify how that practice inflates error rates.
"""

from .analysis import analyze, analyze_groups
from .simulation import coverage_simulation, peeking_type1_simulation
from .synthetic import make_synthetic

__all__ = [
    "analyze",
    "analyze_groups",
    "coverage_simulation",
    "peeking_type1_simulation",
    "make_synthetic",
]

__version__ = "0.1.0"
