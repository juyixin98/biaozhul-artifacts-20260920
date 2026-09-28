"""Inference service: request validation, graph execution, diagnostics."""

from __future__ import annotations

import uuid
from dataclasses import dataclass
from typing import Any

import numpy as np

from engine.errors import (
    InvalidInputError,
    ModelNotFoundError,
    ModelVersionError,
)
from engine.graph import as_graph_input
from engine.model import QuantizedModelArtifact
from engine.numerics import error_report, float_reference
from engine.tensor_types import QTensor

from .registry import ModelRegistry


@dataclass(frozen=True)
class InferenceResponse:
    request_id: str
    model_id: str
    model_version: str
    output: list[list[float]]
    diagnostics: dict[str, Any]


@dataclass(frozen=True)
class ValidationResponse:
    request_id: str
    model_id: str
    accepted: bool
    reason: str
    error_report: dict[str, Any]
    diagnostics: dict[str, Any]


class InferenceService:
    def __init__(
        self,
        registry: ModelRegistry,
        *,
        max_batch_size: int = 64,
        max_features: int = 4096,
        enforce_calibration_range: bool = True,
    ) -> None:
        self._registry = registry
        self._max_batch_size = max_batch_size
        self._max_features = max_features
        self._enforce_calibration_range = enforce_calibration_range

    @staticmethod
    def new_request_id() -> str:
        return f"req-{uuid.uuid4().hex[:16]}"

    def _get_artifact(self, model_id: str, request_id: str) -> QuantizedModelArtifact:
        try:
            return self._registry.get(model_id)
        except ModelNotFoundError as exc:
            exc.context["request_id"] = request_id
            raise

    def _parse_input(
        self, data: Any, expected_features: int, request_id: str
    ) -> np.ndarray:
        if not isinstance(data, list) or not data:
            raise InvalidInputError(
                "input must be a non-empty 2-D array (list of rows)",
                request_id=request_id,
            )
        if len(data) > self._max_batch_size:
            raise InvalidInputError(
                "batch exceeds configured maximum",
                request_id=request_id,
                batch_size=len(data),
                max_batch_size=self._max_batch_size,
            )
        rows = data
        for i, row in enumerate(rows):
            if not isinstance(row, list) or len(row) != expected_features:
                raise InvalidInputError(
                    "every row must be a list matching the model feature count",
                    request_id=request_id,
                    bad_row=i,
                    row_length=len(row) if isinstance(row, list) else None,
                    expected_features=expected_features,
                )
            if expected_features > self._max_features:
                raise InvalidInputError(
                    "feature count exceeds configured maximum",
                    request_id=request_id,
                    features=expected_features,
                    max_features=self._max_features,
                )
            for j, v in enumerate(row):
                if isinstance(v, bool) or not isinstance(v, (int, float)):
                    raise InvalidInputError(
                        "input elements must be JSON numbers",
                        request_id=request_id,
                        bad_row=i,
                        bad_column=j,
                        value_type=type(v).__name__,
                    )
        x = np.asarray(rows, dtype=np.float64)
        if not np.all(np.isfinite(x)):
            raise InvalidInputError(
                "input contains non-finite values (NaN/Inf are rejected)",
                request_id=request_id,
            )
        return x

    def _run_graph(
        self,
        artifact: QuantizedModelArtifact,
        x: np.ndarray,
        request_id: str,
    ):
        q_input = QTensor(x, artifact.input_params)
        graph = artifact.build_graph()
        result = graph.execute(
            as_graph_input(q_input),
            request_id=request_id,
            enforce_range=self._enforce_calibration_range,
        )
        return result

    def infer(
        self, model_id: str, data: Any, *, request_id: str | None = None
    ) -> InferenceResponse:
        request_id = request_id or self.new_request_id()
        artifact = self._get_artifact(model_id, request_id)
        x = self._parse_input(
            data, artifact.architecture[0], request_id
        )
        result = self._run_graph(artifact, x, request_id)
        return InferenceResponse(
            request_id=request_id,
            model_id=artifact.model_id,
            model_version=artifact.model_version,
            output=result.output.values.tolist(),
            diagnostics=result.to_diagnostics_dict(),
        )

    def validate(
        self,
        model_id: str,
        data: Any,
        *,
        request_id: str | None = None,
    ) -> ValidationResponse:
        """Run inference and compare against the float64 reference.

        Acceptance rule (explicit, reported back with the request id):
          * ACCEPT - executed and every non-saturated element is within the
            analytic quantization bound;
          * REJECT - request invalid or out of calibrated range;
          * INDETERMINATE - executed but bound violated (possible regression).
        """
        request_id = request_id or self.new_request_id()
        artifact = self._get_artifact(model_id, request_id)
        trained = self._registry.require_trained(model_id)
        if artifact.model_version != trained.version:
            from engine.errors import ModelVersionError

            raise ModelVersionError(
                "calibration is bound to a different model version; refuse "
                "to compare against a float state the artifact was not "
                "calibrated from",
                request_id=request_id,
                artifact_model_version=artifact.model_version,
                trained_model_version=trained.version,
            )
        x = self._parse_input(data, artifact.architecture[0], request_id)
        result = self._run_graph(artifact, x, request_id)
        reference = float_reference(trained, x)
        saturated = result.output.saturated
        report = error_report(
            result.output.values,
            reference,
            artifact,
            saturated,
            hidden_saturation=result.hidden_saturated,
        )
        accepted = report.within_analytic_bound
        if accepted:
            reason = "ACCEPT: all non-saturated outputs within analytic bound"
        elif report.hidden_saturation:
            reason = (
                "INDETERMINATE: a hidden activation saturated; its clipping "
                "error is outside the analytic bound, correctness unprovable"
            )
        else:
            reason = (
                "INDETERMINATE: observed error exceeds the analytic bound "
                f"({report.bound_violations} violating elements)"
            )
        return ValidationResponse(
            request_id=request_id,
            model_id=model_id,
            accepted=accepted,
            reason=reason,
            error_report=report.to_dict(),
            diagnostics=result.to_diagnostics_dict(),
        )
