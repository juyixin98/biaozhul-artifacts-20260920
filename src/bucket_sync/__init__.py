"""Local multi-process teaching runtime for gradient bucketing and reduction.

Module responsibilities (import surface):

- :mod:`bucket_sync.tensor_types` - tensor value objects, dtypes, boundary checks
- :mod:`bucket_sync.graph`         - ordered parameter graph
- :mod:`bucket_sync.bucketing`     - fixed bucket layout, pack/unpack
- :mod:`bucket_sync.training`      - model state, local gradient computation
- :mod:`bucket_sync.reference`     - independent single-process joint-batch oracle
- :mod:`bucket_sync.round_types`   - failure categories, round value objects
- :mod:`bucket_sync.reduction`     - pure weighted bucket reduction
- :mod:`bucket_sync.coordinator`   - round state machine, commit barrier
- :mod:`bucket_sync.diagnostics`   - structured accept/reject evidence with redaction
- :mod:`bucket_sync.worker`        - worker subprocess loop
- :mod:`bucket_sync.runtime`       - local multi-process orchestration
- :mod:`bucket_sync.api`           - FastAPI control-plane shell over the coordinator
"""

from bucket_sync.tensor_types import TensorSpec
from bucket_sync.graph import ParameterGraph
from bucket_sync.bucketing import BucketLayout, plan_buckets
from bucket_sync.training import ModelState, linear_mse_gradients
from bucket_sync.reference import (
    joint_linear_mse,
    reference_weighted_mean,
    reference_sgd_step,
)
from bucket_sync.coordinator import Coordinator, RejectReason
from bucket_sync.diagnostics import Diagnostics, DiagnosticEvent

__all__ = [
    "TensorSpec",
    "ParameterGraph",
    "BucketLayout",
    "plan_buckets",
    "ModelState",
    "linear_mse_gradients",
    "joint_linear_mse",
    "reference_weighted_mean",
    "reference_sgd_step",
    "Coordinator",
    "RejectReason",
    "Diagnostics",
    "DiagnosticEvent",
]
