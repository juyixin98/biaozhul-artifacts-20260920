"""Feature transformation pipeline (fit-on-train, read-only inference).

Public modules:
    schema     - column/schema definitions and validation
    transforms - imputer, standard scaler, one-hot encoder
    pipeline   - ordered per-column transformation pipeline
    model      - least-squares linear regression on transformed features
    artifact   - serializable model artifact (schema + pipeline + model)
    service    - JSON file based CLI service (train / infer)
    data       - reproducible synthetic dataset generator
"""

from .schema import ColumnSpec, Schema, SchemaError
from .errors import NotFittedError
from .transforms import (
    CategoricalImputer,
    NumericImputer,
    OneHotEncoder,
    StandardScaler,
)
from .pipeline import Pipeline, NotFittedError
from .model import LinearRegressionModel
from .artifact import ModelArtifact

__all__ = [
    "ColumnSpec",
    "Schema",
    "SchemaError",
    "NumericImputer",
    "CategoricalImputer",
    "StandardScaler",
    "OneHotEncoder",
    "Pipeline",
    "NotFittedError",
    "LinearRegressionModel",
    "ModelArtifact",
]
