"""Manifest parsing, signing and validation tests."""

from __future__ import annotations

import hashlib
import json

import pytest

from model_switch.manifest import (
    canonical_body_bytes,
    list_versions,
    load_manifest,
    write_artifact,
)
from model_switch.errors import (
    ArtifactIntegrityError,
    ArtifactNotFoundError,
    ManifestValidationError,
)
from model_switch.model import build_version_weights


def test_load_valid_manifest(registry_root):
    m = load_manifest(registry_root, "v1")
    assert m.version == "v1"
    assert set(m.tensors) == {"W1", "b1", "W2", "b2"}
    assert m.tensors["W1"].shape == (4, 8)
    assert m.tensors["b2"].shape == (2,)
    assert len(m.warmup.input) == 4
    assert len(m.warmup.expected_logits) == 2


def test_missing_artifact_directory(registry_root):
    with pytest.raises(ArtifactNotFoundError):
        load_manifest(registry_root, "does-not-exist")


def test_manifest_body_tamper_without_resign_is_detected(registry_root):
    p = registry_root / "v1" / "manifest.json"
    body = json.loads(p.read_text("utf-8"))
    body["model_kind"] = "attacker/v9"
    p.write_bytes(canonical_body_bytes(body))
    with pytest.raises(ArtifactIntegrityError, match="checksum"):
        load_manifest(registry_root, "v1")


def test_manifest_signature_file_tamper_is_detected(registry_root):
    sig = registry_root / "v1" / "manifest.json.sha256"
    sig.write_text("0" * 64 + "\n", encoding="ascii")
    with pytest.raises(ArtifactIntegrityError):
        load_manifest(registry_root, "v1")


def test_resigned_manifest_with_bad_shape_is_rejected(registry_root):
    """An attacker who can re-sign still cannot bypass structural rules."""
    p = registry_root / "v1" / "manifest.json"
    body = json.loads(p.read_text("utf-8"))
    body["tensors"]["b2"]["shape"] = [7]
    raw = canonical_body_bytes(body)
    p.write_bytes(raw)
    (registry_root / "v1" / "manifest.json.sha256").write_text(
        hashlib.sha256(raw).hexdigest() + "\n", encoding="ascii"
    )
    with pytest.raises(ManifestValidationError, match="shape"):
        load_manifest(registry_root, "v1")


def test_wrong_model_kind_is_rejected(registry_root):
    p = registry_root / "v1" / "manifest.json"
    body = json.loads(p.read_text("utf-8"))
    body["model_kind"] = "other/v1"
    raw = canonical_body_bytes(body)
    p.write_bytes(raw)
    (registry_root / "v1" / "manifest.json.sha256").write_text(
        hashlib.sha256(raw).hexdigest() + "\n", encoding="ascii"
    )
    with pytest.raises(ManifestValidationError, match="model_kind"):
        load_manifest(registry_root, "v1")


def test_list_versions(registry_root):
    assert list_versions(registry_root)[0] == "v-bad-warmup"
    assert set(list_versions(registry_root)) >= {"v1", "v2", "v3"}
    (registry_root / "no-manifest-dir").mkdir()
    versions = list_versions(registry_root)
    assert "no-manifest-dir" not in versions


def test_artifact_writing_is_bit_reproducible(tmp_path):
    weights = build_version_weights(3)
    a = write_artifact(tmp_path / "r1", "v3", weights)
    b = write_artifact(tmp_path / "r2", "v3", weights)
    for name in ("manifest.json", "W1.npy", "b1.npy", "W2.npy", "b2.npy"):
        assert (a / name).read_bytes() == (b / name).read_bytes()


def test_write_artifact_refuses_to_overwrite(registry_root):
    with pytest.raises(FileExistsError):
        write_artifact(registry_root, "v1", build_version_weights(1))
