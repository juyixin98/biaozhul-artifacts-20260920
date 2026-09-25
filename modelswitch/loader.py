"""Staged artifact loading: read -> verify -> validate -> build -> warm up.

`load_candidate` runs entirely off to the side and returns a fully
constructed, immutable `ModelVersion`. Nothing is published anywhere during
loading, so a request can never observe half-loaded weights: the registry
only ever sees the object after the final stage succeeds.

Failure taxonomy:
    ArtifactError    base class
    ChecksumError    weights.npz bytes do not match manifest sha256
    ValidationError  structural problems (missing files, bad shapes/dtypes)
    WarmupError      model built but warm-up inferences misbehaved

Structural validation deliberately does NOT reject non-finite weights: an
inf/NaN is a structurally valid float64. Whether the weights *behave* is a
runtime question, and that is exactly what warm-up answers.
"""
from __future__ import annotations

import enum
import json
import threading
from pathlib import Path
from typing import Callable

import numpy as np

from .artifacts import MANIFEST_NAME, WEIGHTS_NAME, sha256_file
from .model import LinearModel


class ArtifactError(Exception):
    """Base class for all artifact loading failures."""


class ChecksumError(ArtifactError):
    """weights.npz does not match the sha256 recorded in the manifest."""


class ValidationError(ArtifactError):
    """Artifact is structurally invalid (missing/corrupt/mismatched)."""


class WarmupError(ArtifactError):
    """Model built successfully but failed warm-up inference checks."""


class LoadStage(enum.Enum):
    READ_MANIFEST = "read_manifest"
    VERIFY_CHECKSUM = "verify_checksum"
    LOAD_WEIGHTS = "load_weights"
    VALIDATE = "validate"
    BUILD_MODEL = "build_model"
    WARMUP = "warmup"
    READY = "ready"


# Number of synthetic inference batches run during warm-up.
DEFAULT_WARMUP_ITERATIONS = 8
# Warm-up batch size.
WARMUP_BATCH = 4
# Tolerance for the "probabilities sum to 1" warm-up check.
PROB_SUM_TOL = 1e-6


class ModelVersion:
    """An immutable, fully loaded and warmed-up model, ready to serve.

    Instances are created only by `load_candidate` after every stage passed.
    `close()` releases the underlying arrays; it is called by the registry
    exactly once, after the last in-flight request referencing this version
    has finished. Calling `predict` after `close` raises RuntimeError.
    """

    def __init__(self, *, version: str, model: LinearModel, source: str) -> None:
        self._version = version
        self._model: LinearModel | None = model
        self._source = source
        self._closed = False
        self._close_lock = threading.Lock()

    @property
    def version(self) -> str:
        return self._version

    @property
    def source(self) -> str:
        return self._source

    @property
    def closed(self) -> bool:
        return self._closed

    @property
    def input_dim(self) -> int:
        return self._require_model().input_dim

    @property
    def output_dim(self) -> int:
        return self._require_model().output_dim

    def predict(self, x: np.ndarray) -> np.ndarray:
        return self._require_model().predict(x)

    def close(self) -> None:
        with self._close_lock:
            self._model = None
            self._closed = True

    def _require_model(self) -> LinearModel:
        model = self._model
        if model is None:
            raise RuntimeError(
                f"model version {self._version!r} has been released; "
                "requests must hold a registry lease"
            )
        return model

    def __repr__(self) -> str:  # pragma: no cover - debugging aid
        state = "closed" if self._closed else "open"
        return f"<ModelVersion {self._version!r} ({state}) from {self._source!r}>"


def load_candidate(
    artifact_dir: str | Path,
    *,
    warmup_iterations: int = DEFAULT_WARMUP_ITERATIONS,
    on_stage: Callable[[LoadStage], None] | None = None,
) -> ModelVersion:
    """Load, validate and warm up an artifact; return a ready ModelVersion.

    Raises ArtifactError (or a subclass) on any failure; nothing is
    published and no global state is touched on failure.

    `on_stage` is an optional hook invoked before each stage; tests use it to
    inject delays and prove that readers never observe a candidate mid-load.
    """
    artifact_dir = Path(artifact_dir)

    def stage(name: LoadStage) -> None:
        if on_stage is not None:
            on_stage(name)

    stage(LoadStage.READ_MANIFEST)
    manifest = _read_manifest(artifact_dir)

    stage(LoadStage.VERIFY_CHECKSUM)
    _verify_checksum(artifact_dir, manifest)

    stage(LoadStage.LOAD_WEIGHTS)
    arrays = _read_weights(artifact_dir)

    stage(LoadStage.VALIDATE)
    weights, bias = _validate(manifest, arrays)

    stage(LoadStage.BUILD_MODEL)
    model = LinearModel(weights, bias)

    stage(LoadStage.WARMUP)
    _warmup(model, manifest, warmup_iterations)

    stage(LoadStage.READY)
    return ModelVersion(
        version=str(manifest["version"]),
        model=model,
        source=str(artifact_dir),
    )


def _read_manifest(artifact_dir: Path) -> dict:
    path = artifact_dir / MANIFEST_NAME
    if not path.is_file():
        raise ValidationError(f"missing manifest: {path}")
    try:
        manifest = json.loads(path.read_text())
    except json.JSONDecodeError as exc:
        raise ValidationError(f"manifest is not valid JSON: {exc}") from exc
    for key in ("version", "input_dim", "output_dim", "sha256"):
        if key not in manifest:
            raise ValidationError(f"manifest missing required key {key!r}")
    return manifest


def _verify_checksum(artifact_dir: Path, manifest: dict) -> None:
    weights_path = artifact_dir / WEIGHTS_NAME
    if not weights_path.is_file():
        raise ValidationError(f"missing weights file: {weights_path}")
    actual = sha256_file(weights_path)
    expected = manifest["sha256"]
    if actual != expected:
        raise ChecksumError(
            f"checksum mismatch for {weights_path}: "
            f"manifest says {expected[:12]}..., file hashes to {actual[:12]}..."
        )


def _read_weights(artifact_dir: Path) -> dict:
    try:
        with np.load(artifact_dir / WEIGHTS_NAME) as npz:
            return {name: npz[name] for name in npz.files}
    except ValidationError:
        raise
    except Exception as exc:  # corrupt zip, bad pickle, etc.
        raise ValidationError(f"cannot read weights file: {exc}") from exc


def _validate(manifest: dict, arrays: dict) -> tuple[np.ndarray, np.ndarray]:
    for name in ("W", "b"):
        if name not in arrays:
            raise ValidationError(f"weights file missing array {name!r}")
    weights, bias = arrays["W"], arrays["b"]
    if not np.issubdtype(weights.dtype, np.floating):
        raise ValidationError(f"W must be floating point, got {weights.dtype}")
    if not np.issubdtype(bias.dtype, np.floating):
        raise ValidationError(f"b must be floating point, got {bias.dtype}")
    expected_w = (int(manifest["input_dim"]), int(manifest["output_dim"]))
    if tuple(weights.shape) != expected_w:
        raise ValidationError(
            f"W shape {tuple(weights.shape)} does not match manifest {expected_w}"
        )
    if bias.shape != (expected_w[1],):
        raise ValidationError(
            f"b shape {bias.shape} does not match manifest output_dim {expected_w[1]}"
        )
    return weights, bias


def _warmup(model: LinearModel, manifest: dict, iterations: int) -> None:
    """Run synthetic inferences; any non-finite or non-probability output fails."""
    if iterations < 1:
        raise WarmupError("warmup_iterations must be >= 1")
    rng = np.random.default_rng(int(manifest.get("seed", 0)))
    input_dim = int(manifest["input_dim"])
    output_dim = int(manifest["output_dim"])
    for i in range(iterations):
        batch = rng.normal(size=(WARMUP_BATCH, input_dim))
        try:
            out = model.predict(batch)
        except Exception as exc:
            raise WarmupError(f"warm-up inference {i} raised: {exc}") from exc
        if out.shape != (WARMUP_BATCH, output_dim):
            raise WarmupError(
                f"warm-up inference {i} produced shape {out.shape}, "
                f"expected ({WARMUP_BATCH}, {output_dim})"
            )
        if not np.all(np.isfinite(out)):
            raise WarmupError(
                f"warm-up inference {i} produced non-finite outputs "
                "(weights are numerically unusable)"
            )
        row_sums = out.sum(axis=1)
        if not np.all(np.abs(row_sums - 1.0) <= PROB_SUM_TOL):
            raise WarmupError(
                f"warm-up inference {i} outputs are not probabilities "
                f"(row sums {row_sums})"
            )
