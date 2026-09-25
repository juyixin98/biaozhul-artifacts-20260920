"""Artifact loading: checksum verification, tensor validation, warm-up.

The loader turns an on-disk artifact into a fully ready
:class:`LoadedModel`.  Critically, a candidate model is built and warmed up
in a *private* object before the manager publishes it — a warm-up failure
therefore never mutates any currently serving version.
"""

from __future__ import annotations

import time
from dataclasses import dataclass, field
from pathlib import Path

import numpy as np

from .errors import ArtifactIntegrityError, WarmupValidationError
from .manifest import Manifest, TensorSpec, _sha256_file, load_manifest
from .model import LAYER_NAMES, SimpleMLP

WARMUP_RTOL = 1e-5
WARMUP_ATOL = 1e-6


@dataclass(frozen=True)
class StageTiming:
    name: str
    elapsed_ms: float


@dataclass
class LoadReport:
    """Observability record for one load attempt (successful or not)."""

    version: str
    started_at: float
    stages: list[StageTiming] = field(default_factory=list)
    bytes_verified: int = 0
    ok: bool = False
    error: str | None = None

    def elapsed_ms(self) -> float:
        return sum(t.elapsed_ms for t in self.stages)


class LoadedModel:
    """A ready-to-serve model with a thread-safe reference count.

    The model starts with refcount 1, owned by the manager's active slot.
    Every request lease adds one reference; the model's NumPy buffers are
    released only when the count returns to zero.
    """

    def __init__(self, version: str, model: SimpleMLP, manifest: Manifest):
        self.version = version
        self.model = model
        self.manifest = manifest
        self._refcount = 1
        self._disposed = False

    def acquire(self) -> None:
        if self._disposed:
            raise RuntimeError(f"model {self.version!r} already disposed")
        self._refcount += 1

    def release(self) -> int:
        """Drop one reference; returns the remaining count."""
        if self._refcount <= 0:
            raise RuntimeError(f"model {self.version!r}: release underflow")
        self._refcount -= 1
        return self._refcount

    @property
    def refcount(self) -> int:
        return self._refcount

    @property
    def disposed(self) -> bool:
        return self._disposed

    def dispose(self) -> None:
        """Free the underlying NumPy buffers. Idempotent."""
        if self._refcount != 0:
            raise RuntimeError(
                f"refusing to dispose model {self.version!r} with refcount {self._refcount}"
            )
        if not self._disposed:
            self.model.dispose()
            self._disposed = True


class ArtifactLoader:
    """Loads artifacts from a registry root. Stateless and thread-safe."""

    def __init__(self, root: str | Path, *, warmup: bool = True):
        self.root = Path(root)
        self.warmup = warmup

    def load(self, version: str) -> tuple[LoadedModel, LoadReport]:
        """Verify and fully warm up ``version``.

        Raises on any failure; nothing is returned on failure and the
        partially built candidate is discarded (and its buffers freed).
        """
        report = LoadReport(version=version, started_at=time.monotonic())
        candidate: SimpleMLP | None = None
        try:
            t0 = time.monotonic()
            manifest = load_manifest(self.root, version)
            report.stages.append(StageTiming("manifest", (time.monotonic() - t0) * 1000))

            weights = self._load_tensors(manifest, report)

            t0 = time.monotonic()
            candidate = SimpleMLP(weights, version)
            report.stages.append(StageTiming("construct", (time.monotonic() - t0) * 1000))

            if self.warmup:
                self._warmup(candidate, manifest, report)

            report.ok = True
            return LoadedModel(version, candidate, manifest), report
        except Exception:
            # A candidate that failed warm-up must not leak buffers.
            if candidate is not None:
                candidate.dispose()
            raise

    def _load_tensors(self, manifest: Manifest, report: LoadReport) -> dict[str, np.ndarray]:
        version_dir = manifest.version_dir(self.root)
        weights: dict[str, np.ndarray] = {}
        for name in LAYER_NAMES:
            spec = manifest.tensors[name]
            path = version_dir / f"{name}.npy"
            t0 = time.monotonic()
            digest, size = _sha256_file(path) if path.is_file() else ("", -1)
            if size < 0:
                raise ArtifactIntegrityError(f"[{manifest.version}] missing tensor file {path.name}")
            if digest != spec.sha256:
                raise ArtifactIntegrityError(
                    f"[{manifest.version}] tensor {name!r} checksum mismatch "
                    f"(expected {spec.sha256[:12]}..., got {digest[:12]}...)"
                )
            if size != spec.bytes:
                raise ArtifactIntegrityError(
                    f"[{manifest.version}] tensor {name!r}: size {size} != manifest {spec.bytes}"
                )
            arr = np.load(path, allow_pickle=False)
            self._validate_array(manifest.version, name, arr, spec)
            weights[name] = arr
            report.bytes_verified += size
            report.stages.append(
                StageTiming(f"tensor:{name}", (time.monotonic() - t0) * 1000)
            )
        return weights

    @staticmethod
    def _validate_array(version: str, name: str, arr: np.ndarray, spec: TensorSpec) -> None:
        if tuple(arr.shape) != spec.shape:
            raise ArtifactIntegrityError(
                f"[{version}] tensor {name!r}: shape {tuple(arr.shape)} != manifest {spec.shape}"
            )
        if str(arr.dtype) != spec.dtype:
            raise ArtifactIntegrityError(
                f"[{version}] tensor {name!r}: dtype {arr.dtype} != manifest {spec.dtype}"
            )
        if not np.isfinite(arr).all():
            raise ArtifactIntegrityError(
                f"[{version}] tensor {name!r} contains non-finite values"
            )
        if not arr.flags["C_CONTIGUOUS"]:
            raise ArtifactIntegrityError(f"[{version}] tensor {name!r} is not C-contiguous")

    @staticmethod
    def _warmup(model: SimpleMLP, manifest: Manifest, report: LoadReport) -> None:
        t0 = time.monotonic()
        x = np.asarray(manifest.warmup.input, dtype=np.float32)
        logits = model.forward(x)
        if not np.isfinite(logits).all():
            raise WarmupValidationError(
                f"[{manifest.version}] warm-up produced non-finite logits: {logits!r}"
            )
        expected = np.asarray(manifest.warmup.expected_logits, dtype=np.float32)
        if not np.allclose(logits, expected, rtol=WARMUP_RTOL, atol=WARMUP_ATOL):
            raise WarmupValidationError(
                f"[{manifest.version}] warm-up output mismatch: "
                f"got {logits!r}, expected {expected!r}"
            )
        report.stages.append(
            StageTiming("warmup", (time.monotonic() - t0) * 1000)
        )
