"""Feature statistical drift monitoring service (pure backend).

Public API:
    FixedBinner       - fixed bin boundaries learned on a baseline window
    DriftMonitor      - fit baseline feature bins and score a current window
    psi / js_divergence / total_variation / wasserstein_1 - drift metrics
    make_dataset      - reproducible synthetic data generator
    LogisticModel     - tiny NumPy logistic regression used by the demo
"""
from .binning import FixedBinner, bucket_labels
from .metrics import psi, js_divergence, total_variation, wasserstein_1, smooth_probs
from .monitor import DriftMonitor, DriftResult, PSI_STABLE, PSI_WARNING
from .synthetic import make_dataset
from .model import LogisticModel

__all__ = [
    "FixedBinner",
    "bucket_labels",
    "psi",
    "js_divergence",
    "total_variation",
    "wasserstein_1",
    "smooth_probs",
    "DriftMonitor",
    "DriftResult",
    "PSI_STABLE",
    "PSI_WARNING",
    "make_dataset",
    "LogisticModel",
]
