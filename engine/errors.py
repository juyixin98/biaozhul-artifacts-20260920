"""Typed errors.

Every failure carries an explicit :class:`DecisionCategory`:

* ACCEPT        - not an error; result is usable.
* REJECT        - deterministically unsafe/invalid input or state. The caller
                  can fix the request; saturation/overflow bounds are enforced
                  here instead of relying on host-language integer behavior.
* INDETERMINATE - internal failure; correctness cannot be judged.

The HTTP status and stable machine-readable ``code`` travel with the error so
the API layer never invents error semantics.
"""

from __future__ import annotations

from enum import Enum
from typing import Any


class DecisionCategory(str, Enum):
    ACCEPT = "ACCEPT"
    REJECT = "REJECT"
    INDETERMINATE = "INDETERMINATE"


class QuantEngineError(Exception):
    """Base class. Subclasses fix ``http_status``, ``code`` and ``category``."""

    http_status: int = 500
    code: str = "internal_error"
    category: DecisionCategory = DecisionCategory.INDETERMINATE

    def __init__(self, message: str, **context: Any) -> None:
        super().__init__(message)
        self.message = message
        # Only small, non-tensor diagnostic values (shapes, bounds, indices)
        # are allowed in context. Handlers must redact before logging.
        self.context: dict[str, Any] = context


class TensorValidationError(QuantEngineError):
    http_status = 400
    code = "invalid_tensor"
    category = DecisionCategory.REJECT


class QuantizationParameterError(QuantEngineError):
    http_status = 400
    code = "invalid_quantization_parameters"
    category = DecisionCategory.REJECT


class AccumulatorOverflowError(QuantEngineError):
    """Static accumulator width check failed - result would not fit."""

    http_status = 422
    code = "accumulator_overflow"
    category = DecisionCategory.REJECT


class OutOfCalibrationRangeError(QuantEngineError):
    http_status = 422
    code = "input_out_of_calibration_range"
    category = DecisionCategory.REJECT


class InvalidInputError(QuantEngineError):
    http_status = 400
    code = "invalid_input"
    category = DecisionCategory.REJECT


class ModelNotFoundError(QuantEngineError):
    http_status = 404
    code = "model_not_found"
    category = DecisionCategory.REJECT


class ModelVersionError(QuantEngineError):
    http_status = 409
    code = "model_version_mismatch"
    category = DecisionCategory.REJECT


class NumericsValidationError(QuantEngineError):
    http_status = 422
    code = "numerics_validation_failed"
    category = DecisionCategory.REJECT


class GraphExecutionError(QuantEngineError):
    http_status = 500
    code = "graph_execution_failed"
    category = DecisionCategory.INDETERMINATE
