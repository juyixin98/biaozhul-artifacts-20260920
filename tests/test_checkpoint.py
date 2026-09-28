"""Checkpoint integrity tests: atomic commit, reshard load, typed rejections."""

from __future__ import annotations

import json
import os
import shutil

import numpy as np
import pytest

from adam_shard.adam import AdamConfig
from adam_shard.errors import (
    CommitMismatchError,
    CorruptManifestError,
    DigestMismatchError,
    IncompleteManifestError,
    MissingShardError,
    ShapeMismatchError,
)
from adam_shard.layout import ParamLayout
from adam_shard.sharding import ShardPlan
from adam_shard.training_state import load_commit, require_same_commit, save_commit
from adam_shard.tensor_types import json_digest


def _make_commit(root: str, commit_id: str, spec, world_size: int, step: int = 4, seed: int = 0):
    layout = ParamLayout(tuple(spec.parameter_ids()))
    plan = ShardPlan.create(layout.total_numel, world_size)
    rng = np.random.default_rng(seed)
    n = layout.total_numel
    p = rng.standard_normal(n)
    m = rng.standard_normal(n) * 0.1
    v = np.abs(rng.standard_normal(n)) * 0.01
    save_commit(
        root=root, commit_id=commit_id, layout=layout, plan=plan, step=step,
        adam_cfg=AdamConfig(), param_flat=p, m_flat=m, v_flat=v,
    )
    return layout, plan, p, m, v


def _manifest_path(root, commit_id):
    return os.path.join(root, commit_id, "manifest.json")


def _rewrite_manifest_with_valid_digest(path, manifest):
    digest = manifest.pop("manifest_digest")  # noqa: F841
    manifest["manifest_digest"] = json_digest({k: v for k, v in manifest.items() if k != "manifest_digest"})
    with open(path, "w", encoding="utf-8") as fh:
        json.dump(manifest, fh, indent=2, sort_keys=True)


def test_save_and_load_roundtrip_preserves_all_state(tmp_path, spec):
    root = str(tmp_path / "ckpt")
    layout, plan, p, m, v = _make_commit(root, "c1", spec, 2, step=3)
    loaded = load_commit(root, "c1")
    assert loaded.step == 3
    assert loaded.commit_id == "c1"
    np.testing.assert_array_equal(loaded.param_flat, p)
    np.testing.assert_array_equal(loaded.m_flat, m)
    np.testing.assert_array_equal(loaded.v_flat, v)
    # Identity: restored tensors carry stable names+shapes.
    restored_params = loaded.layout.unflatten(loaded.param_flat)
    for tid in spec.parameter_ids():
        assert tid.name in restored_params
        assert restored_params[tid.name].shape == tid.shape


def test_uneven_tail_shard_sizes_recorded(tmp_path, spec):
    root = str(tmp_path / "ckpt")
    _, plan, _, _, _ = _make_commit(root, "c1", spec, 2)
    assert plan.sizes()[-1] == 30 and plan.sizes()[0] == 29


def test_reshard_load_2_to_3_reassembles_identical_globals(tmp_path, spec):
    root = str(tmp_path / "ckpt")
    layout, _, p, m, v = _make_commit(root, "c2", spec, 2, step=5, seed=12)
    loaded = load_commit(root, "c2", target_world_size=3)
    assert loaded.source_plan.world_size == 2
    assert loaded.target_plan.world_size == 3
    assert loaded.target_plan.sizes() == (19, 19, 21)
    np.testing.assert_allclose(loaded.param_flat, p, rtol=1e-13, atol=1e-15)
    np.testing.assert_allclose(loaded.m_flat, m, rtol=1e-13, atol=1e-15)
    np.testing.assert_allclose(loaded.v_flat, v, rtol=1e-13, atol=1e-15)

    # Per-rank slices must exactly partition the global vectors.
    glued_p = np.concatenate([s.param for s in loaded.shards])
    glued_m = np.concatenate([s.m for s in loaded.shards])
    glued_v = np.concatenate([s.v for s in loaded.shards])
    np.testing.assert_array_equal(glued_p, p)
    np.testing.assert_array_equal(glued_m, m)
    np.testing.assert_array_equal(glued_v, v)
    for shard in loaded.shards:
        assert shard.param.shape == shard.m.shape == shard.v.shape
        assert shard.end - shard.start == shard.param.shape[0]


def test_reshard_3_to_2_and_back_matches(tmp_path, spec):
    root = str(tmp_path / "ckpt")
    _, _, p, m, v = _make_commit(root, "c3", spec, 3, step=2, seed=4)
    loaded2 = load_commit(root, "c3", target_world_size=2)
    np.testing.assert_array_equal(np.concatenate([s.param for s in loaded2.shards]), p)
    loaded3 = load_commit(root, "c3", target_world_size=3)
    np.testing.assert_array_equal(loaded3.param_flat, p)
    np.testing.assert_array_equal(loaded3.m_flat, m)
    np.testing.assert_array_equal(loaded3.v_flat, v)


def test_missing_shard_file_is_rejected_with_category(tmp_path, spec):
    root = str(tmp_path / "ckpt")
    _make_commit(root, "c", spec, 2)
    os.remove(os.path.join(root, "c", "model-r1.npy"))
    with pytest.raises(MissingShardError) as exc:
        load_commit(root, "c")
    assert exc.value.category == "missing_shard"
    assert exc.value.detail["rank"] == 1


def test_wrong_shard_shape_is_rejected(tmp_path, spec):
    root = str(tmp_path / "ckpt")
    _make_commit(root, "c", spec, 2)
    # Overwrite one shard with a different element count.
    np.save(os.path.join(root, "c", "model-r0.npy"), np.zeros(3, dtype=np.float64))
    with pytest.raises(ShapeMismatchError) as exc:
        load_commit(root, "c")
    assert exc.value.category == "shape_mismatch"
    assert exc.value.detail["rank"] == 0


def test_corrupt_content_fails_digest_check(tmp_path, spec):
    root = str(tmp_path / "ckpt")
    _make_commit(root, "c", spec, 2)
    # Same length, altered values: shape passes, digest must catch it.
    tampered = np.load(os.path.join(root, "c", "model-r1.npy"))
    tampered[0] += 123.0
    np.save(os.path.join(root, "c", "model-r1.npy"), tampered)
    with pytest.raises(DigestMismatchError) as exc:
        load_commit(root, "c")
    assert exc.value.category == "digest_mismatch"
    assert exc.value.detail["rank"] == 1


def test_swapped_m_and_v_detected_via_digest(tmp_path, spec):
    root = str(tmp_path / "ckpt")
    _make_commit(root, "c", spec, 2)
    path = os.path.join(root, "c", "optim-r0.npz")
    with np.load(path) as zf:
        m, v = zf["m"].copy(), zf["v"].copy()
    np.savez(path, m=v, v=m)  # moments exchanged
    with pytest.raises(DigestMismatchError):
        load_commit(root, "c")


def test_corrupt_manifest_summary_rejected(tmp_path, spec):
    root = str(tmp_path / "ckpt")
    _make_commit(root, "c", spec, 2)
    path = _manifest_path(root, "c")
    with open(path, encoding="utf-8") as fh:
        manifest = json.load(fh)
    manifest["step"] = 999  # alter without recomputing the summary digest
    with open(path, "w", encoding="utf-8") as fh:
        json.dump(manifest, fh)
    with pytest.raises(CorruptManifestError) as exc:
        load_commit(root, "c")
    assert exc.value.category == "corrupt_manifest"


def test_incomplete_manifest_missing_rank_rejected(tmp_path, spec):
    root = str(tmp_path / "ckpt")
    _make_commit(root, "c", spec, 3)
    path = _manifest_path(root, "c")
    with open(path, encoding="utf-8") as fh:
        manifest = json.load(fh)
    manifest["model_shards"] = [e for e in manifest["model_shards"] if e["rank"] != 2]
    _rewrite_manifest_with_valid_digest(path, manifest)
    with pytest.raises(IncompleteManifestError) as exc:
        load_commit(root, "c")
    assert exc.value.category == "incomplete_manifest"


def test_directory_manifest_commit_id_mismatch_rejected(tmp_path, spec):
    root = str(tmp_path / "ckpt")
    _make_commit(root, "real-commit", spec, 2)
    shutil.copytree(os.path.join(root, "real-commit"), os.path.join(root, "impostor"))
    with pytest.raises(CommitMismatchError):
        load_commit(root, "impostor")


def test_require_same_commit_enforces_pairing():
    with pytest.raises(CommitMismatchError):
        require_same_commit("model-c1", "optim-c2")
    require_same_commit("c1", "c1")  # no raise


def test_latest_pointer_roundtrip(tmp_path, spec):
    root = str(tmp_path / "ckpt")
    _make_commit(root, "first", spec, 2, step=1)
    _make_commit(root, "second", spec, 2, step=2)
    loaded = load_commit(root)
    assert loaded.commit_id == "second"


def test_target_world_larger_than_numel_rejected(tmp_path, spec):
    root = str(tmp_path / "ckpt")
    _make_commit(root, "c", spec, 2)
    with pytest.raises(ValueError):
        load_commit(root, "c", target_world_size=10_000)
