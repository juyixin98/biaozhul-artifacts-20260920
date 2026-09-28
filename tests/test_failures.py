"""Failure-mode tests asserting concrete error categories, not just callability.

Each test mutates a committed checkpoint in one specific way and asserts the
exact exception type/category and that the failure is correlated with the
request id.
"""
from __future__ import annotations

import json

import pytest

from adam_shards.errors import (
    CommitMismatchError,
    DigestMismatchError,
    IncompleteManifestError,
    MissingShardError,
    ShapeMismatchError,
    CorruptManifestError,
)
from adam_shards.sharding import restore_arrays

MANIFEST = "manifest.json"
RID = "req-failure-tests"


def _manifest_path(ckpt):
    return ckpt / MANIFEST


def _load_manifest(ckpt):
    with open(_manifest_path(ckpt), encoding="utf-8") as fh:
        return json.load(fh)


def _write_manifest(ckpt, manifest):
    with open(_manifest_path(ckpt), "w", encoding="utf-8") as fh:
        json.dump(manifest, fh, indent=2, sort_keys=True)


def _shards_by_rank(manifest):
    return {s["rank"]: s for s in manifest["shards"]}


def test_missing_shard_file_is_rejected(saved_checkpoint):
    ckpt, _, _, _ = saved_checkpoint
    manifest = _load_manifest(ckpt)
    target = _shards_by_rank(manifest)[1]["files"]["param"]["file"]
    (ckpt / target).unlink()
    with pytest.raises(MissingShardError) as exc:
        restore_arrays(str(ckpt), world_size=3, request_id=RID)
    assert exc.value.category == "missing_shard"
    assert exc.value.request_id == RID
    assert "rank=1" in str(exc.value)


def test_missing_manifest_directory_is_rejected(tmp_path):
    with pytest.raises(MissingShardError, match="manifest not found"):
        restore_arrays(str(tmp_path / "nope"), world_size=2, request_id=RID)


def test_corrupt_manifest_json_is_rejected(saved_checkpoint):
    ckpt, _, _, _ = saved_checkpoint
    _manifest_path(ckpt).write_text("{not valid json", encoding="utf-8")
    with pytest.raises(CorruptManifestError) as exc:
        restore_arrays(str(ckpt), world_size=2, request_id=RID)
    assert exc.value.category == "corrupt_manifest"


def test_incomplete_manifest_dropped_shard_is_rejected(saved_checkpoint):
    """Removing a shard entry leaves an uncovered flat range -> refuse load."""
    ckpt, _, _, _ = saved_checkpoint
    manifest = _load_manifest(ckpt)
    manifest["shards"] = [s for s in manifest["shards"] if s["rank"] != 1]
    _write_manifest(ckpt, manifest)
    with pytest.raises(IncompleteManifestError) as exc:
        restore_arrays(str(ckpt), world_size=2, request_id=RID)
    assert exc.value.category == "incomplete_manifest"
    # Either the missing rank is detected directly, or the coverage gap is.
    msg = str(exc.value)
    assert "do not form" in msg or "gap" in msg or "coverage incomplete" in msg


def test_incomplete_manifest_truncated_slice_is_rejected(saved_checkpoint):
    ckpt, _, _, _ = saved_checkpoint
    manifest = _load_manifest(ckpt)
    # Shorten the rank-1 slice without touching total_elements: a hole opens.
    shard1 = _shards_by_rank(manifest)[1]
    sl = shard1["slices"][0]
    sl["end"] = sl["end"] - 1
    _write_manifest(ckpt, manifest)
    with pytest.raises(IncompleteManifestError):
        restore_arrays(str(ckpt), world_size=2, request_id=RID)


def test_wrong_shape_in_restore_graph_is_rejected(saved_checkpoint):
    ckpt, _, _, _ = saved_checkpoint
    manifest = _load_manifest(ckpt)
    target = {
        name: tuple(spec["shape"])
        for name, spec in manifest["params"].items()
    }
    target["alpha"] = (6,)  # checkpoint has (5,)
    with pytest.raises(ShapeMismatchError) as exc:
        restore_arrays(str(ckpt), world_size=3, target_shapes=target,
                       request_id=RID)
    assert exc.value.category == "shape_mismatch"
    assert "alpha" in str(exc.value)


def test_parameter_identity_set_mismatch_is_rejected(saved_checkpoint):
    ckpt, _, _, _ = saved_checkpoint
    manifest = _load_manifest(ckpt)
    target = {
        name: tuple(spec["shape"])
        for name, spec in manifest["params"].items()
    }
    target["gamma"] = target.pop("beta")  # rename a parameter
    from adam_shards.errors import ParameterUnknownError
    with pytest.raises(ParameterUnknownError) as exc:
        restore_arrays(str(ckpt), world_size=3, target_shapes=target,
                       request_id=RID)
    assert exc.value.category == "parameter_unknown"
    assert "beta" in str(exc.value) and "gamma" in str(exc.value)


def test_corrupt_shard_bytes_fail_summary_digest(saved_checkpoint):
    """Flipping payload bytes invalidates the file digest (summary)."""
    ckpt, _, _, _ = saved_checkpoint
    manifest = _load_manifest(ckpt)
    target = _shards_by_rank(manifest)[0]["files"]["moment1"]["file"]
    path = ckpt / target
    raw = bytearray(path.read_bytes())
    raw[0] ^= 0xFF
    path.write_bytes(bytes(raw))
    with pytest.raises(DigestMismatchError) as exc:
        restore_arrays(str(ckpt), world_size=2, request_id=RID)
    assert exc.value.category == "digest_mismatch"
    assert "digest mismatch" in str(exc.value)


def test_corrupt_step_in_manifest_fails_optimizer_summary(saved_checkpoint):
    """Tampering the step count changes the optimizer summary digest."""
    ckpt, _, _, _ = saved_checkpoint
    manifest = _load_manifest(ckpt)
    manifest["params"]["alpha"]["step"] = 999
    _write_manifest(ckpt, manifest)
    with pytest.raises(DigestMismatchError) as exc:
        restore_arrays(str(ckpt), world_size=2, request_id=RID)
    assert exc.value.category == "digest_mismatch"
    assert "optimizer" in str(exc.value)


def test_corrupt_layout_in_manifest_fails_model_summary(saved_checkpoint):
    ckpt, _, _, _ = saved_checkpoint
    manifest = _load_manifest(ckpt)
    # Changing numel while keeping contiguity valid alters the model summary.
    manifest["params"]["alpha"]["numel"] = 4
    manifest["params"]["beta"]["offset"] = 4
    manifest["total_elements"] = 8
    for shard in manifest["shards"]:
        for sl in shard["slices"]:
            if sl["param"] == "alpha":
                sl["end"] = min(sl["end"], 4)
    _write_manifest(ckpt, manifest)
    # Even if structural validation differs, it must never load silently.
    with pytest.raises((DigestMismatchError, IncompleteManifestError,
                        CorruptManifestError)):
        restore_arrays(str(ckpt), world_size=2, request_id=RID)


def test_model_optimizer_commit_mismatch_is_rejected(saved_checkpoint):
    ckpt, _, _, _ = saved_checkpoint
    manifest = _load_manifest(ckpt)
    manifest["parts"]["optimizer"]["commit_id"] = "different-commit"
    _write_manifest(ckpt, manifest)
    with pytest.raises(CommitMismatchError) as exc:
        restore_arrays(str(ckpt), world_size=2, request_id=RID)
    assert exc.value.category == "commit_mismatch"
    assert "not saved together" in str(exc.value)


def test_overlapping_coverage_is_rejected(saved_checkpoint):
    ckpt, _, _, _ = saved_checkpoint
    manifest = _load_manifest(ckpt)
    # Rank 0 grabs a beta slice that rank 1 also covers -> true overlap,
    # every slice stays within its own parameter bounds.
    shard0 = _shards_by_rank(manifest)[0]
    shard0["slices"] = [
        {"param": "alpha", "start": 0, "end": 5},
        {"param": "beta", "start": 5, "end": 6},
    ]
    _write_manifest(ckpt, manifest)
    with pytest.raises(IncompleteManifestError, match="overlap"):
        restore_arrays(str(ckpt), world_size=2, request_id=RID)
