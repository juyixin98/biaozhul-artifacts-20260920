"""Taint analysis engine package."""

from .engine import AnalysisResult, TaintEngine
from .evidence import Chain, Hop, Value
from .policy import (
    BuiltinSpec,
    DEFAULT_POLICY,
    PolicyExtension,
    build_policy,
)
from .service import AnalysisRequest, run_analysis
from .taint import Taint

__all__ = [
    "run_analysis", "AnalysisRequest", "TaintEngine", "AnalysisResult",
    "Taint", "Value", "Chain", "Hop",
    "DEFAULT_POLICY", "build_policy", "PolicyExtension", "BuiltinSpec",
]
