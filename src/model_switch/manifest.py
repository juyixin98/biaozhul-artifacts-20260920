"""On-disk artifact layout and manifest parsing/validation.

Artifact directory layout (one directory per version)::

    <registry_root>/<version>/
        manifest.json
        W1.npy
        b1.npy
        W2.npy
        b2.npy

The manifest records the model kind, expected tensor shapes/dtypes and the
SHA-256 of every ``.npy`` file.  ``manifest.sha256`` separately authenticates
the manifest body itself (the SHA-256 of its *canonical* JSON form), which
guards against a manifest being silently edited to match tampered weights.
"""

from __future__ import annotations

import hashlib
import json
import math
from dataclasses import dataclass
from pathlib import Path

import numpy as np

from .errors import ArtifactIntegrityError, ArtifactNotFoundError, ManifestValidationError
from .model import (
    INPUT_DIM,
    LAYER_NAMES,
    OUTPUT_DIM,
    WEIGHT_DTYPES,
    expected_warmup_logits,
    fixed_warmup_input,
)

MANIFEST_NAME = "manifest.json"
MANIFEST_SIG_SUFFIX = ".sha256"
MODEL_KIND = "simple-mlp/v1"
MANIFEST_VERSION = 1
# Files that must exist but are not model tensors.
_RESERVED_FILES = {MANIFEST_NAME, MANIFEST_NAME + MANIFEST_SIG_SUFFIX}


@dataclass(frozen=True)
class TensorSpec:
    shape: tuple[int, ...]
    dtype: str
    sha256: str
    bytes: int


@dataclass(frozen=True)
class WarmupSpec:
    """Golden warm-up case covered by the manifest signature."""

    input: tuple[float, ...]
    expected_logits: tuple[float, ...]


@dataclass(frozen=True)
class Manifest:
    version: str
    model_kind: str
    tensors: dict[str, TensorSpec]
    warmup: WarmupSpec

    def version_dir(self, root: str | Path) -> Path:
        return Path(root) / self.version


def canonical_body_bytes(obj: dict) -> bytes:
    """Deterministic JSON encoding used both to write and to sign manifests."""
    return json.dumps(obj, sort_keys=True, separators=(",", ":")).encode("utf-8")


def _sha256_file(path: Path) -> tuple[str, int]:
    h = hashlib.sha256()
    size = 0
    with path.open("rb") as fh:
        for chunk in iter(lambda: fh.read(1 << 20), b""):
            h.update(chunk)
            size += len(chunk)
    return h.hexdigest(), size


def write_artifact(root: str | Path, version: str, weights: dict[str, np.ndarray]) -> Path:
    """Create a complete, signed artifact directory for ``weights``.

    Used by the synthetic-data fixture generator and by tests; the loader
    itself never writes artifacts.
    """
    version_dir = Path(root) / version
    version_dir.mkdir(parents=True, exist_ok=False)

    tensors: dict[str, dict] = {}
    for name in LAYER_NAMES:
        arr = np.ascontiguousarray(weights[name], dtype=np.float32)
        path = version_dir / f"{name}.npy"
        np.save(path, arr, allow_pickle=False)
        digest, size = _sha256_file(path)
        tensors[name] = {
            "shape": list(arr.shape),
            "dtype": str(arr.dtype),
            "sha256": digest,
            "bytes": size,
        }

    warmup_x = fixed_warmup_input()
    warmup_y = expected_warmup_logits(weights)
    warmup = {
        "input": [float(v) for v in warmup_x.tolist()],
        "expected_logits": [float(v) for v in warmup_y.tolist()],
    }

    body = {
        "manifest_version": MANIFEST_VERSION,
        "model_kind": MODEL_KIND,
        "version": version,
        "tensors": tensors,
        "warmup": warmup,
    }
    body_path = version_dir / MANIFEST_NAME
    body_path.write_bytes(canonical_body_bytes(body))
    signature = hashlib.sha256(canonical_body_bytes(body)).hexdigest()
    (version_dir / (MANIFEST_NAME + MANIFEST_SIG_SUFFIX)).write_text(signature + "\n")
    return version_dir


def _parse_tensor_specs(raw: object, version: str) -> dict[str, TensorSpec]:
    if not isinstance(raw, dict):
        raise ManifestValidationError(f"[{version}] 'tensors' must be an object")
    missing = [n for n in LAYER_NAMES if n not in raw]
    if missing:
        raise ManifestValidationError(f"[{version}] manifest missing tensors: {missing}")
    extra = [n for n in raw if n not in LAYER_NAMES]
    if extra:
        raise ManifestValidationError(f"[{version}] manifest has unknown tensors: {extra}")

    specs: dict[str, TensorSpec] = {}
    for name in LAYER_NAMES:
        entry = raw[name]
        if not isinstance(entry, dict):
            raise ManifestValidationError(f"[{version}] tensor {name!r} entry must be an object")
        try:
            shape = tuple(int(d) for d in entry["shape"])
            dtype = str(entry["dtype"])
            digest = str(entry["sha256"])
            nbytes = int(entry["bytes"])
            int(digest, 16)
        except (KeyError, TypeError, ValueError) as exc:
            raise ManifestValidationError(
                f"[{version}] tensor {name!r} entry malformed: {exc}"
            ) from exc

        expected_dtype, expected_shape = WEIGHT_DTYPES[name]
        if shape != expected_shape:
            raise ManifestValidationError(
                f"[{version}] tensor {name!r}: manifest shape {shape} "
                f"!= expected {expected_shape}"
            )
        if dtype != np.dtype(expected_dtype).name:
            raise ManifestValidationError(
                f"[{version}] tensor {name!r}: manifest dtype {dtype!r} "
                f"!= expected {np.dtype(expected_dtype).name!r}"
            )
        if len(digest) != 64:
            raise ManifestValidationError(f"[{version}] tensor {name!r}: sha256 must be 64 hex chars")
        if nbytes < 0:
            raise ManifestValidationError(f"[{version}] tensor {name!r}: negative byte count")
        specs[name] = TensorSpec(shape, dtype, digest, nbytes)
    return specs


def _parse_warmup(raw: object, version: str) -> WarmupSpec:
    if not isinstance(raw, dict):
        raise ManifestValidationError(f"[{version}] 'warmup' must be an object")
    try:
        x = [float(v) for v in raw["input"]]
        y = [float(v) for v in raw["expected_logits"]]
    except (KeyError, TypeError, ValueError) as exc:
        raise ManifestValidationError(f"[{version}] warmup case malformed: {exc}") from exc
    if len(x) != INPUT_DIM:
        raise ManifestValidationError(
            f"[{version}] warmup input length {len(x)} != {INPUT_DIM}"
        )
    if len(y) != OUTPUT_DIM:
        raise ManifestValidationError(
            f"[{version}] warmup expected_logits length {len(y)} != {OUTPUT_DIM}"
        )
    if not all(math.isfinite(v) for v in x + y):
        raise ManifestValidationError(f"[{version}] warmup case contains non-finite values")
    return WarmupSpec(input=tuple(x), expected_logits=tuple(y))


def load_manifest(root: str | Path, version: str) -> Manifest:
    """Read and verify a manifest and its detached SHA-256 signature."""
    version_dir = Path(root) / version
    manifest_path = version_dir / MANIFEST_NAME
    sig_path = version_dir / (MANIFEST_NAME + MANIFEST_SIG_SUFFIX)
    if not version_dir.is_dir():
        raise ArtifactNotFoundError(f"artifact directory not found: {version_dir}")
    if not manifest_path.is_file():
        raise ArtifactNotFoundError(f"manifest not found: {manifest_path}")
    if not sig_path.is_file():
        raise ManifestValidationError(f"[{version}] missing manifest signature {sig_path.name}")

    raw_bytes = manifest_path.read_bytes()
    try:
        obj = json.loads(raw_bytes.decode("utf-8"))
    except (UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise ManifestValidationError(f"[{version}] manifest is not valid JSON: {exc}") from exc
    if not isinstance(obj, dict):
        raise ManifestValidationError(f"[{version}] manifest root must be an object")

    # Authenticate the bytes FIRST. Any semantic-field tampering without the
    # signing key must fail here as an integrity error, not masquerade as a
    # benign validation error from a field the tamper happened to touch.
    actual_sig = hashlib.sha256(raw_bytes).hexdigest()
    expected_sig = sig_path.read_text(encoding="ascii").strip()
    if actual_sig != expected_sig:
        raise ArtifactIntegrityError(
            f"[{version}] manifest checksum mismatch "
            f"(expected {expected_sig[:12]}..., got {actual_sig[:12]}...)"
        )

    # Everything below is covered by the signature verified above.
    if obj.get("manifest_version") != MANIFEST_VERSION:
        raise ManifestValidationError(
            f"[{version}] unsupported manifest_version={obj.get('manifest_version')!r}"
        )
    if obj.get("model_kind") != MODEL_KIND:
        raise ManifestValidationError(
            f"[{version}] model_kind {obj.get('model_kind')!r} != {MODEL_KIND!r}"
        )
    if obj.get("version") != version:
        raise ManifestValidationError(
            f"[{version}] manifest declares version {obj.get('version')!r}"
        )
    if not isinstance(version, str) or not version:
        raise ManifestValidationError("version must be a non-empty string")

    specs = _parse_tensor_specs(obj.get("tensors"), version)
    warmup = _parse_warmup(obj.get("warmup"), version)
    return Manifest(version=version, model_kind=MODEL_KIND, tensors=specs, warmup=warmup)


def list_versions(root: str | Path) -> list[str]:
    """Return the sorted version names that contain a manifest file."""
    root = Path(root)
    if not root.is_dir():
        return []
    return sorted(
        p.parent.name
        for p in root.glob(f"*/{MANIFEST_NAME}")
        if p.is_file()
    )
