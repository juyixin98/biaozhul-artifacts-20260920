"""Tests for malformed-but-signed manifests and missing files.

These exercise every validation branch of the manifest parser: the manifest
signature is re-generated after mutation, so failures come from structural/
semantic validation rather than the checksum gate.
"""

from __future__ import annotations

import hashlib
import json

import pytest

from model_switch.errors import (
    ArtifactIntegrityError,
    ArtifactNotFoundError,
    ManifestValidationError,
)
from model_switch.manifest import (
    canonical_body_bytes,
    load_manifest,
)


def _rewrite_manifest(registry_root, version: str, mutate) -> None:
    """Load a manifest, mutate its body dict, re-serialize and re-sign it."""
    vdir = registry_root / version
    path = vdir / "manifest.json"
    body = json.loads(path.read_text("utf-8"))
    mutate(body)
    raw = canonical_body_bytes(body)
    path.write_bytes(raw)
    (vdir / "manifest.json.sha256").write_text(
        hashlib.sha256(raw).hexdigest() + "\n", encoding="ascii"
    )


def test_tensors_not_an_object(registry_root):
    _rewrite_manifest(registry_root, "v1", lambda b: b.update(tensors=[]))
    with pytest.raises(ManifestValidationError, match="'tensors' must be an object"):
        load_manifest(registry_root, "v1")


def test_missing_tensor_entry(registry_root):
    def mutate(b):
        del b["tensors"]["b2"]
    _rewrite_manifest(registry_root, "v1", mutate)
    with pytest.raises(ManifestValidationError, match="missing tensors"):
        load_manifest(registry_root, "v1")


def test_extra_tensor_entry(registry_root):
    def mutate(b):
        b["tensors"]["W3"] = b["tensors"]["W1"]
    _rewrite_manifest(registry_root, "v1", mutate)
    with pytest.raises(ManifestValidationError, match="unknown tensors"):
        load_manifest(registry_root, "v1")


def test_tensor_entry_not_an_object(registry_root):
    _rewrite_manifest(registry_root, "v1", lambda b: b["tensors"].update(b1=None))
    with pytest.raises(ManifestValidationError, match="entry must be an object"):
        load_manifest(registry_root, "v1")


def test_tensor_entry_missing_field(registry_root):
    def mutate(b):
        del b["tensors"]["W1"]["dtype"]
    _rewrite_manifest(registry_root, "v1", mutate)
    with pytest.raises(ManifestValidationError, match="entry malformed"):
        load_manifest(registry_root, "v1")


def test_tensor_shape_field_not_numeric(registry_root):
    def mutate(b):
        b["tensors"]["W1"]["shape"] = ["four", 8]
    _rewrite_manifest(registry_root, "v1", mutate)
    with pytest.raises(ManifestValidationError, match="entry malformed"):
        load_manifest(registry_root, "v1")


def test_manifest_dtype_mismatch_is_validation_error(registry_root):
    def mutate(b):
        b["tensors"]["W1"]["dtype"] = "float64"
    _rewrite_manifest(registry_root, "v1", mutate)
    with pytest.raises(ManifestValidationError, match="dtype"):
        load_manifest(registry_root, "v1")


def test_bad_sha256_length(registry_root):
    def mutate(b):
        b["tensors"]["W1"]["sha256"] = "abc"
    _rewrite_manifest(registry_root, "v1", mutate)
    with pytest.raises(ManifestValidationError, match="64 hex"):
        load_manifest(registry_root, "v1")


def test_sha256_not_hex(registry_root):
    def mutate(b):
        b["tensors"]["W1"]["sha256"] = "z" + "0" * 63
    _rewrite_manifest(registry_root, "v1", mutate)
    with pytest.raises(ManifestValidationError, match="entry malformed"):
        load_manifest(registry_root, "v1")


def test_negative_byte_count(registry_root):
    def mutate(b):
        b["tensors"]["W1"]["bytes"] = -10
    _rewrite_manifest(registry_root, "v1", mutate)
    with pytest.raises(ManifestValidationError, match="negative byte"):
        load_manifest(registry_root, "v1")


def test_warmup_not_an_object(registry_root):
    _rewrite_manifest(registry_root, "v1", lambda b: b.update(warmup=[]))
    with pytest.raises(ManifestValidationError, match="'warmup' must be an object"):
        load_manifest(registry_root, "v1")


def test_warmup_missing_field(registry_root):
    def mutate(b):
        del b["warmup"]["input"]
    _rewrite_manifest(registry_root, "v1", mutate)
    with pytest.raises(ManifestValidationError, match="warmup case malformed"):
        load_manifest(registry_root, "v1")


def test_warmup_wrong_input_length(registry_root):
    def mutate(b):
        b["warmup"]["input"] = [1.0, 2.0]
    _rewrite_manifest(registry_root, "v1", mutate)
    with pytest.raises(ManifestValidationError, match="warmup input length"):
        load_manifest(registry_root, "v1")


def test_warmup_wrong_output_length(registry_root):
    def mutate(b):
        b["warmup"]["expected_logits"] = [1.0]
    _rewrite_manifest(registry_root, "v1", mutate)
    with pytest.raises(ManifestValidationError, match="expected_logits length"):
        load_manifest(registry_root, "v1")


def test_warmup_non_finite(registry_root):
    def mutate(b):
        b["warmup"]["input"][0] = "Infinity"
    _rewrite_manifest(registry_root, "v1", mutate)
    # "Infinity" parses as float('inf') -> rejected at the finiteness gate.
    with pytest.raises(ManifestValidationError, match="non-finite"):
        load_manifest(registry_root, "v1")


def test_unsupported_manifest_version(registry_root):
    _rewrite_manifest(registry_root, "v1", lambda b: b.update(manifest_version=999))
    with pytest.raises(ManifestValidationError, match="manifest_version"):
        load_manifest(registry_root, "v1")


def test_declared_version_mismatch(registry_root):
    _rewrite_manifest(registry_root, "v1", lambda b: b.update(version="v2"))
    with pytest.raises(ManifestValidationError, match="declares version"):
        load_manifest(registry_root, "v1")


def test_missing_signature_file(registry_root):
    (registry_root / "v1" / "manifest.json.sha256").unlink()
    with pytest.raises(ManifestValidationError, match="missing manifest signature"):
        load_manifest(registry_root, "v1")


def test_manifest_not_json(registry_root):
    path = registry_root / "v1" / "manifest.json"
    path.write_bytes(b"{this is not json")
    # Unparsable bytes also fail the signature gate if the .sha256 was made
    # for valid JSON; here it remains the old signature, so integrity fires.
    with pytest.raises((ManifestValidationError, ArtifactIntegrityError)):
        load_manifest(registry_root, "v1")


def test_manifest_root_not_object_after_resign(registry_root):
    raw = canonical_body_bytes([1, 2, 3])
    path = registry_root / "v1" / "manifest.json"
    path.write_bytes(raw)
    (registry_root / "v1" / "manifest.json.sha256").write_text(
        hashlib.sha256(raw).hexdigest() + "\n", encoding="ascii"
    )
    with pytest.raises(ManifestValidationError, match="root must be an object"):
        load_manifest(registry_root, "v1")


def test_missing_manifest_file(registry_root):
    (registry_root / "v1" / "manifest.json").unlink()
    with pytest.raises(ArtifactNotFoundError, match="manifest not found"):
        load_manifest(registry_root, "v1")
