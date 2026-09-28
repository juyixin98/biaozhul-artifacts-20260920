"""IPW-ATE: cross-fitted inverse-probability-weighted ATE with overlap diagnostics.

Synthetic-data-only research tool. All causal outputs are conditional on the
*unconfoundedness* and *positivity* assumptions; the package never treats an
estimate as proof of causality.
"""

from .statcontract import EstimationResult, StatisticalContract
from .pipeline import run_ipw

__all__ = ["StatisticalContract", "EstimationResult", "run_ipw"]
__version__ = "0.1.0"
