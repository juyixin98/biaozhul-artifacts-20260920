"""Error taxonomy for amptrain.

Design rule: gradient overflow during training is a *domain status*, not an
exception -- it is reported through ``WindowOutcome`` and the event log.
Exceptions are reserved for contract violations (bad config, bad input,
corrupt checkpoints) and for non-finite values detected *outside* the
checked low-precision compute path (which always indicates a bug).
"""

from __future__ import annotations

from enum import Enum


class ErrorCode(str, Enum):
    """Stable machine-readable error codes (also used in API envelopes)."""

    CONFIG_INVALID = "CONFIG_INVALID"
    INPUT_INVALID = "INPUT_INVALID"
    RUN_NOT_FOUND = "RUN_NOT_FOUND"
    CHECKPOINT_NOT_FOUND = "CHECKPOINT_NOT_FOUND"
    CHECKPOINT_CORRUPT = "CHECKPOINT_CORRUPT"
    NONFINITE_UNEXPECTED = "NONFINITE_UNEXPECTED"
    INTERNAL_ERROR = "INTERNAL_ERROR"


class AmpTrainError(Exception):
    """Base exception carrying a stable :class:`ErrorCode`."""

    def __init__(self, code: ErrorCode, message: str, *, detail: dict | None = None):
        super().__init__(message)
        self.code = code
        self.message = message
        self.detail = detail or {}

    def to_dict(self) -> dict:
        return {"code": self.code.value, "message": self.message, "detail": self.detail}


class NonFiniteTensorError(AmpTrainError):
    """A non-finite value appeared where the checked compute path forbids it.

    Raised by the compute graph when a forward/backward stage produces
    inf/NaN, and by the trainer if an *unscaled fp32 accumulator* is ever
    non-finite (that path must be unreachable given checked gradients).
    """

    def __init__(self, stage: str, tensor_names: list[str]):
        super().__init__(
            ErrorCode.NONFINITE_UNEXPECTED,
            f"non-finite value(s) at stage '{stage}': {', '.join(tensor_names)}",
            detail={"stage": stage, "tensors": list(tensor_names)},
        )
        self.stage = stage
        self.tensor_names = list(tensor_names)
