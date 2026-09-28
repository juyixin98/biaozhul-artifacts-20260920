"""Training/calibration state.

A model moves through an explicit lifecycle::

    CALIBRATING  --(freeze with CalibrationBundle)-->  FROZEN

Only ``FROZEN`` models can be registered for inference. Once frozen, nothing
about the numeric parameters can change: there is no API to re-estimate, and
the bound calibration fingerprint is carried with every request diagnosis.
Per-request "calibration" is therefore impossible by construction rather than
by convention.
"""

from __future__ import annotations

from dataclasses import dataclass
from enum import Enum

from .calibration import CalibrationBundle
from .errors import CalibrationError, ModelNotCalibratedError


class LifecycleState(str, Enum):
    CALIBRATING = "CALIBRATING"
    FROZEN = "FROZEN"


@dataclass(frozen=True)
class FrozenModelState:
    """Immutable identity of a deployable model.

    Attributes:
        model_id: stable model identifier.
        model_version: exact version the calibration was produced for.
        calibration_fingerprint: hash over every frozen quant parameter.
        created_at: calibration timestamp (informational).
    """

    model_id: str
    model_version: str
    calibration_fingerprint: str
    created_at: str

    @classmethod
    def freeze(cls, bundle: CalibrationBundle) -> "FrozenModelState":
        if not bundle.layers:
            raise CalibrationError("refusing to freeze an empty calibration bundle")
        return cls(
            model_id=bundle.model_id,
            model_version=bundle.model_version,
            calibration_fingerprint=bundle.fingerprint(),
            created_at=bundle.created_at,
        )

    def matches(self, model_id: str, model_version: str) -> bool:
        return self.model_id == model_id and self.model_version == model_version


class ModelLifecycle:
    """Guards the CALIBRATING -> FROZEN transition (one way, exactly once)."""

    def __init__(self, model_id: str, model_version: str) -> None:
        self._model_id = model_id
        self._model_version = model_version
        self._state = LifecycleState.CALIBRATING
        self._frozen: FrozenModelState | None = None

    @property
    def state(self) -> LifecycleState:
        return self._state

    @property
    def frozen(self) -> FrozenModelState:
        if self._frozen is None:
            raise ModelNotCalibratedError(
                f"model {self._model_id}@{self._model_version} is not frozen"
            )
        return self._frozen

    def freeze(self, bundle: CalibrationBundle) -> FrozenModelState:
        if self._state is LifecycleState.FROZEN:
            raise CalibrationError("model already frozen; calibration cannot change")
        if bundle.model_id != self._model_id or bundle.model_version != self._model_version:
            raise CalibrationError(
                "calibration bundle identity mismatch: "
                f"bundle={bundle.model_id}@{bundle.model_version} vs "
                f"model={self._model_id}@{self._model_version}"
            )
        self._frozen = FrozenModelState.freeze(bundle)
        self._state = LifecycleState.FROZEN
        return self._frozen
