"""Loader tests: checksum/shape/dtype validation and warm-up gating."""

from __future__ import annotations

import hashlib
import json

import numpy as np
import pytest

from model_switch.errors import (
    ArtifactIntegrityError,
    ArtifactNotFoundError,
    WarmupValidationError,
)
from model_switch.loader import ArtifactLoader
from model_switch.manifest import MANIFEST_NAME, canonical_body_bytes

BAD_WARMUP_VERSION = "v-bad-warmup"


def _rehash_and_resign_tensor(registry_root, version: str, name: str) -> None:
    """Update a tensor's sha256/bytes in the manifest and re-sign it.

    Keeps every declared shape/dtype unchanged, so the hash gate passes and
    failure must occur at the later shape/dtype/finiteness gate.
    """
    version_dir = registry_root / version
    data = (version_dir / f"{name}.npy").read_bytes()
    manifest_path = version_dir / MANIFEST_NAME
    body = json.loads(manifest_path.read_text("utf-8"))
    body["tensors"][name]["sha256"] = hashlib.sha256(data).hexdigest()
    body["tensors"][name]["bytes"] = len(data)
    raw = canonical_body_bytes(body)
    manifest_path.write_bytes(raw)
    (version_dir / f"{MANIFEST_NAME}.sha256").write_text(
        hashlib.sha256(raw).hexdigest() + "\n", encoding="ascii"
    )


def test_load_valid_version_runs_warmup(loader):
    loaded, report = loader.load("v1")
    assert loaded.version == "v1"
    assert report.ok is True
    assert report.bytes_verified > 0
    stage_names = [t.name for t in report.stages]
    assert stage_names[0] == "manifest"
    assert stage_names[-1] == "warmup"
    # The manager starts owning one reference.
    assert loaded.refcount == 1
    assert loaded.disposed is False


def test_warmup_failure_raises_and_reports_stage(loader):
    with pytest.raises(WarmupValidationError, match="warm-up output mismatch"):
        loader.load(BAD_WARMUP_VERSION)


def test_warmup_failure_does_not_leak_candidate_buffers(loader):
    # A failed candidate must be disposed rather than half-published.
    with pytest.raises(WarmupValidationError):
        loader.load(BAD_WARMUP_VERSION)
    # The loader is stateless: nothing retained. Load v1 to prove service
    # remains usable afterwards on a fresh, valid artifact.
    loaded, _ = loader.load("v1")
    assert loaded.version == "v1"


def test_missing_tensor_file_is_integrity_error(registry_root):
    (registry_root / "v1" / "b2.npy").unlink()
    with pytest.raises(ArtifactIntegrityError, match="missing tensor file"):
        ArtifactLoader(registry_root).load("v1")


def test_tensor_byte_tamper_is_detected(registry_root):
    path = registry_root / "v1" / "W1.npy"
    raw = bytearray(path.read_bytes())
    # Flip a byte well inside the array payload (skip the .npy header).
    raw[-1] ^= 0xFF
    path.write_bytes(bytes(raw))
    with pytest.raises(ArtifactIntegrityError, match="checksum mismatch"):
        ArtifactLoader(registry_root).load("v1")


def test_tensor_shape_tamper_is_detected(registry_root):
    # Save a different-shaped array under the same filename, then make its
    # hash match so failure occurs strictly at the shape gate (the manifest
    # still declares the old shape).
    wrong = np.zeros((4, 7), dtype=np.float32)
    np.save(registry_root / "v1" / "W1.npy", wrong)
    _rehash_and_resign_tensor(registry_root, "v1", "W1")
    with pytest.raises(ArtifactIntegrityError, match="shape"):
        ArtifactLoader(registry_root).load("v1")


def test_tensor_dtype_tamper_is_detected(registry_root):
    wrong = np.zeros((4, 8), dtype=np.float64)
    np.save(registry_root / "v1" / "W1.npy", wrong)
    _rehash_and_resign_tensor(registry_root, "v1", "W1")
    with pytest.raises(ArtifactIntegrityError, match="dtype"):
        ArtifactLoader(registry_root).load("v1")


def test_non_finite_tensor_is_rejected(registry_root):
    good = np.load(registry_root / "v1" / "W1.npy")
    good[0, 0] = np.inf
    np.save(registry_root / "v1" / "W1.npy", good)
    _rehash_and_resign_tensor(registry_root, "v1", "W1")
    with pytest.raises(ArtifactIntegrityError, match="non-finite"):
        ArtifactLoader(registry_root).load("v1")


def test_load_missing_version(loader):
    with pytest.raises(ArtifactNotFoundError):
        loader.load("nope")


def test_loaded_model_refcount_lifecycle():
    from model_switch.loader import LoadedModel
    from model_switch.model import SimpleMLP, build_version_weights

    model = SimpleMLP(build_version_weights(1), "v1")
    loaded = LoadedModel("v1", model, manifest=None)
    assert loaded.refcount == 1
    loaded.acquire()
    assert loaded.refcount == 2
    assert loaded.release() == 1
    # Cannot free while a reference is outstanding.
    with pytest.raises(RuntimeError, match="refcount"):
        loaded.dispose()
    assert loaded.release() == 0
    loaded.dispose()
    assert loaded.disposed is True
    with pytest.raises(RuntimeError, match="disposed"):
        model.forward(np.zeros(4, dtype=np.float32))


def test_release_underflow_is_an_error():
    from model_switch.loader import LoadedModel
    from model_switch.model import SimpleMLP, build_version_weights

    loaded = LoadedModel("v1", SimpleMLP(build_version_weights(1), "v1"), None)
    loaded.release()
    with pytest.raises(RuntimeError, match="underflow"):
        loaded.release()
