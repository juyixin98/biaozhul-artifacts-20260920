"""Group-aware stratified dataset splitting (pure backend, Python + NumPy).

Public API:
    split_dataset      -- core splitting function
    SplitResult        -- result container with deviations and diagnostics
    generate_synthetic_dataset
    run_synthetic_demo
"""
from .models import (
    ClassDeviation,
    GroupReport,
    SplitDiagnostics,
    SplitResult,
)
from .splitter import split_dataset
from .synthetic import Dataset, generate_synthetic_dataset
from .pipeline import run_synthetic_demo

__all__ = [
    "split_dataset",
    "SplitResult",
    "SplitDiagnostics",
    "ClassDeviation",
    "GroupReport",
    "Dataset",
    "generate_synthetic_dataset",
    "run_synthetic_demo",
]
