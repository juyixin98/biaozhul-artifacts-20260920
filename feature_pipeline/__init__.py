"""Feature transformation pipeline: fit-once, read-only inference preprocessing.

Public API:
    ColumnSpec, FeaturePipeline, TransformResult
    SimpleImputer, StandardScaler, OneHotEncoder
    save_pipeline, load_pipeline
    exceptions
"""
from .exceptions import (
    FeaturePipelineError,
    FittingError,
    NotFittedError,
    SchemaError,
    SerializationError,
)
from .pipeline import ColumnSpec, FeaturePipeline, TransformResult
from .serialization import load_pipeline, save_pipeline
from .transformers import OneHotEncoder, SimpleImputer, StandardScaler

__version__ = "1.0.0"

__all__ = [
    "ColumnSpec",
    "FeaturePipeline",
    "TransformResult",
    "SimpleImputer",
    "StandardScaler",
    "OneHotEncoder",
    "save_pipeline",
    "load_pipeline",
    "FeaturePipelineError",
    "FittingError",
    "NotFittedError",
    "SchemaError",
    "SerializationError",
    "__version__",
]
