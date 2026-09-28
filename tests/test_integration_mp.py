"""End-to-end multi-process integration tests.

Scenario required by the project brief:

* train with 2 processes for several steps and commit;
* restore the SAME commit with 3 processes (uneven tail) and take one step;
* the result must equal a single-process independent reference that never
  touched the sharding core;
* parameters presented in reordered dict traversal still map by identity;
* corrupted/incomplete checkpoints are refused with concrete categories.
"""

from __future__ import annotations

import json
import os

import numpy as np
import pytest

from adam_shard.fixtures import load_sample_dataset
from adam_shard.graph import MLPModule, mse_loss_and_grad
from adam_shard.reference_adam import ReferenceAdam
from adam_shard.service import describe_commits, train, validate_commit, verify_restore_parity
from adam_shard.training_state import load_commit

pytestmark = pytest.mark.integration


def test_two_process_train_then_three_process_restore_matches_reference(app_config):
    cfg = app_config

    # Phase 1: train with 2 processes, 3 steps.
    run2 = train(cfg, world_size=2, steps=3, request_id="req-int-2p")
    assert run2.step == 3
    assert run2.restored_from is None
    assert run2.world_size == 2
    assert len(run2.losses) == 2

    # Commit on disk is loadable and records the uneven split 29/30.
    loaded = load_commit(cfg.storage_root, run2.commit_id)
    assert loaded.source_plan.sizes() == (29, 30)

    # Phase 2: independent single-process oracle takes ONE more step.
    data = load_sample_dataset(cfg)
    ref_model = MLPModule(cfg.spec, dtype=cfg.dtype, seed=cfg.seed)
    params = loaded.layout.unflatten(loaded.param_flat)
    m_by = loaded.layout.unflatten(loaded.m_flat)
    v_by = loaded.layout.unflatten(loaded.v_flat)
    ref_model.install(params)
    ref_opt = ReferenceAdam(
        cfg=loaded.adam_cfg,
        moments={n: (m_by[n], v_by[n]) for n in m_by},
        t=loaded.step,
    )
    pred = ref_model.forward(data["x"])
    _, grad_out = mse_loss_and_grad(pred, data["y"])
    grads = ref_model.backward(data["x"], grad_out)
    expected_params = ref_opt.step(params, grads)
    expected_m = {n: mv[0] for n, mv in ref_opt.moments.items()}
    expected_v = {n: mv[1] for n, mv in ref_opt.moments.items()}

    # Phase 3: restore with 3 processes and take that same one step (4 total).
    run3 = train(
        cfg, world_size=3, steps=4, request_id="req-int-3p",
        model_commit=run2.commit_id, optim_commit=run2.commit_id,
    )
    assert run3.restored_from == run2.commit_id
    assert run3.world_size == 3
    assert run3.step == 4

    loaded3 = load_commit(cfg.storage_root, run3.commit_id)
    assert loaded3.source_plan.sizes() == (19, 19, 21)
    actual_params = loaded3.layout.unflatten(loaded3.param_flat)
    actual_m = loaded3.layout.unflatten(loaded3.m_flat)
    actual_v = loaded3.layout.unflatten(loaded3.v_flat)

    # The headline assertion: one restored update under 3 processes equals the
    # unsharded reference element-for-element.
    for name in sorted(expected_params):
        np.testing.assert_allclose(
            actual_params[name], expected_params[name], rtol=1e-11, atol=1e-13,
            err_msg=f"param {name} diverged after 2->3 restore+step",
        )
        np.testing.assert_allclose(actual_m[name], expected_m[name], rtol=1e-11, atol=1e-13)
        np.testing.assert_allclose(actual_v[name], expected_v[name], rtol=1e-11, atol=1e-13)

    # Every rank is a full replica on identical data, so all rank losses agree.
    assert len(run3.losses) == 3
    assert all(abs(x - run3.losses[0]) < 1e-12 for x in run3.losses)


def test_parameter_reorder_does_not_alias_state(app_config):
    """Restored state maps by (name, shape) regardless of dict traversal order."""

    cfg = app_config
    run2 = train(cfg, world_size=2, steps=2, request_id="req-reorder-2")
    loaded = load_commit(cfg.storage_root, run2.commit_id)
    restored = loaded.layout.unflatten(loaded.param_flat)

    # Feed the restored tensors in reversed traversal order into a fresh model.
    reversed_params = dict(sorted(restored.items(), key=lambda kv: kv[0], reverse=True))
    model_a = MLPModule(cfg.spec, dtype=cfg.dtype, seed=cfg.seed)
    model_a.install(reversed_params)
    model_b = MLPModule(cfg.spec, dtype=cfg.dtype, seed=cfg.seed)
    model_b.install(restored)

    data = load_sample_dataset(cfg)
    np.testing.assert_array_equal(model_a.forward(data["x"]), model_b.forward(data["x"]))

    # Identity trap: values installed under the wrong stable name/shape are
    # rejected, so state can never silently alias another parameter by ordinal.
    swapped = dict(restored)
    swapped["layers.0.weight"] = restored["layers.1.weight"]  # (4,5) under a (5,3) name
    with pytest.raises(ValueError):
        MLPModule(cfg.spec, dtype=cfg.dtype, seed=cfg.seed).install(swapped)


def test_verify_parity_report_is_interpretable_and_passes(app_config):
    cfg = app_config
    run2 = train(cfg, world_size=2, steps=2, request_id="req-report")
    report = verify_restore_parity(cfg, run2.commit_id, target_world_size=3, request_id="req-report-v")
    payload = report.to_dict()

    assert payload["request_id"] == "req-report-v"
    assert payload["stage"] == "restore+one_step"
    assert payload["version"] == "1.0.0"
    assert cfg.storage_root in payload["location"] and run2.commit_id in payload["location"]
    assert payload["status"] == "pass"
    assert payload["failures"] == []
    assert payload["uncertainties"] == []
    names = {c["name"] for c in payload["checks"]}
    assert {
        "one_step_params",
        "one_step_moments",
        "step_counter_correspondence",
        "finite_difference_gradient",
        "reshard_uneven_tail",
    } <= names
    # Concrete recorded values, not just "interface callable".
    params_check = next(c for c in payload["checks"] if c["name"] == "one_step_params")
    assert params_check["detail"]["max_abs_err"] < 1e-11


def test_validate_reports_reshard_plan(app_config):
    cfg = app_config
    run2 = train(cfg, world_size=2, steps=1, request_id="req-val")
    result = validate_commit(cfg, run2.commit_id, target_world_size=3)
    assert result["ok"] is True
    assert result["source_world_size"] == 2
    assert result["target_shard_sizes"] == [19, 19, 21]
    assert {p["name"] for p in result["parameters"]} == {
        "layers.0.bias", "layers.0.weight",
        "layers.1.bias", "layers.1.weight",
        "layers.2.bias", "layers.2.weight",
    }


def test_list_commits_reflects_both_runs(app_config):
    cfg = app_config
    a = train(cfg, world_size=2, steps=1, request_id="req-list-a")
    b = train(cfg, world_size=3, steps=1, request_id="req-list-b")
    commits = {c["commit_id"]: c for c in describe_commits(cfg)}
    assert commits[a.commit_id]["shard_sizes"] == [29, 30]
    assert commits[b.commit_id]["source_world_size"] == 3


def test_corrupt_commit_before_restore_is_refused_with_category(app_config):
    cfg = app_config
    run2 = train(cfg, world_size=2, steps=2, request_id="req-badbase")
    cdir = os.path.join(cfg.storage_root, run2.commit_id)

    # Tamper a shard after the commit was published.
    path = os.path.join(cdir, "model-r0.npy")
    bad = np.load(path)
    bad[0] += 999.0
    np.save(path, bad)

    from adam_shard.errors import DigestMismatchError

    with pytest.raises(DigestMismatchError) as exc:
        train(
            cfg, world_size=3, steps=3, request_id="req-badrestore",
            model_commit=run2.commit_id, optim_commit=run2.commit_id,
        )
    assert exc.value.category == "digest_mismatch"


def test_missing_shard_before_restore_refused_with_category(app_config):
    cfg = app_config
    run2 = train(cfg, world_size=2, steps=1, request_id="req-missbase")
    os.remove(os.path.join(cfg.storage_root, run2.commit_id, "optim-r1.npz"))

    from adam_shard.errors import MissingShardError

    with pytest.raises(MissingShardError) as exc:
        train(
            cfg, world_size=3, steps=2, request_id="req-missrestore",
            model_commit=run2.commit_id, optim_commit=run2.commit_id,
        )
    assert exc.value.category == "missing_shard"


def test_incomplete_manifest_refused_with_category(app_config):
    cfg = app_config
    run2 = train(cfg, world_size=2, steps=1, request_id="req-incbase")
    path = os.path.join(cfg.storage_root, run2.commit_id, "manifest.json")
    with open(path, encoding="utf-8") as fh:
        manifest = json.load(fh)
    manifest["optim_shards"] = [e for e in manifest["optim_shards"] if e["rank"] != 0]
    from adam_shard.tensor_types import json_digest

    manifest["manifest_digest"] = json_digest(
        {k: v for k, v in manifest.items() if k != "manifest_digest"}
    )
    with open(path, "w", encoding="utf-8") as fh:
        json.dump(manifest, fh)

    from adam_shard.errors import IncompleteManifestError

    with pytest.raises(IncompleteManifestError) as exc:
        train(
            cfg, world_size=3, steps=2, request_id="req-increstore",
            model_commit=run2.commit_id, optim_commit=run2.commit_id,
        )
    assert exc.value.category == "incomplete_manifest"


def test_cross_commit_model_optimizer_pairing_refused(app_config):
    cfg = app_config
    a = train(cfg, world_size=2, steps=1, request_id="req-pair-a")
    b = train(cfg, world_size=2, steps=1, request_id="req-pair-b")

    from adam_shard.errors import CommitMismatchError

    with pytest.raises(CommitMismatchError) as exc:
        train(
            cfg, world_size=2, steps=2, request_id="req-pair-mix",
            model_commit=a.commit_id, optim_commit=b.commit_id,
        )
    assert exc.value.category == "commit_mismatch"
