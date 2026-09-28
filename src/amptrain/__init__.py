"""amptrain: mixed-precision trainer for small synthetic networks.

Layering:
- ``tensors``   : tensor-type layer (master weights / low-precision params /
                  optimizer state / gradient accumulators).
- ``graph``     : compute graph (low-precision forward/backward, overflow checks).
- ``trainer``   : training state (dynamic loss scaling, gradient accumulation,
                  step semantics, checkpoints).
- ``reference`` : numerical validation (independent full-precision oracle).
- ``api``       : FastAPI service layer on top of the trainer.
"""

from .config import (
    AccumulationConfig,
    DataConfig,
    ModelConfig,
    OptimizerConfig,
    PrecisionConfig,
    RunConfig,
    ScalerConfig,
)
from .errors import AmpTrainError, ErrorCode, NonFiniteTensorError
from .version import VERSION

__all__ = [
    "AccumulationConfig",
    "AmpTrainError",
    "DataConfig",
    "ErrorCode",
    "ModelConfig",
    "NonFiniteTensorError",
    "OptimizerConfig",
    "PrecisionConfig",
    "RunConfig",
    "ScalerConfig",
    "VERSION",
]
