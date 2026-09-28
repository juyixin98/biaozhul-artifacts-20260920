"""Model registry.

Holds frozen artifacts (and optionally the float trained state used only for
the independent validation comparison). Artifacts are loaded once at startup
and shared read-only across requests - no per-request calibration or
parameter estimation ever happens here.
"""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path

from engine.errors import ModelNotFoundError
from engine.model import (
    QuantizedModelArtifact,
    TrainedModel,
    load_artifact,
)


@dataclass(frozen=True)
class RegisteredModel:
    artifact: QuantizedModelArtifact
    trained: TrainedModel | None


class ModelRegistry:
    def __init__(self) -> None:
        self._models: dict[str, RegisteredModel] = {}

    def register(
        self,
        artifact: QuantizedModelArtifact,
        trained: TrainedModel | None = None,
    ) -> None:
        self._models[artifact.model_id] = RegisteredModel(
            artifact=artifact, trained=trained
        )

    def get(self, model_id: str) -> QuantizedModelArtifact:
        return self._require(model_id).artifact

    def require_trained(self, model_id: str) -> TrainedModel:
        registered = self._require(model_id)
        if registered.trained is None:
            raise ModelNotFoundError(
                "float reference state is not registered for this model; "
                "validation is unavailable",
                model_id=model_id,
            )
        return registered.trained

    def _require(self, model_id: str) -> RegisteredModel:
        try:
            return self._models[model_id]
        except KeyError:
            raise ModelNotFoundError(
                "model is not registered; build/load it before inference",
                model_id=model_id,
                available=sorted(self._models),
            )

    def ids(self) -> list[str]:
        return sorted(self._models)

    def load_directory(self, directory: str | Path) -> list[str]:
        directory = Path(directory)
        loaded: list[str] = []
        if not directory.exists():
            return loaded
        for path in sorted(directory.glob("*.json")):
            artifact = load_artifact(path)
            self.register(artifact)
            loaded.append(artifact.model_id)
        return loaded
