"""Integration test: 2-process save -> 3-process restore, reordered identity,
one more update must equal the unsharded independent reference exactly.
"""
from __future__ import annotations

import numpy as np
import pytest

from adam_shards.adam import AdamConfig
from adam_shards.pipeline import run_pipeline
from adam_shards.verification import reference_adam_run
from adam_shards.logging_utils import get_logger

DIMS = (3, 4, 2)
RID = "req-integration-2to3"


def _run(tmp_path):
    return run_pipeline(
        dims=DIMS, seed=20260927, n_samples=40, batch_size=8, train_steps=4,
        adam_cfg=AdamConfig(lr=0.05, beta1=0.9, beta2=0.999, eps=1e-8),
        save_world_size=2, restore_world_size=3,
        ckpt_dir=str(tmp_path / "ckpt"), request_id=RID,
        tol=1e-12, logger=get_logger(RID, "integration"),
    )


def test_full_pipeline_2_to_3_matches_unsharded_reference(tmp_path):
    result = _run(tmp_path)
    payload = result.to_dict()

    # The whole point: restored one-step update is bit-for-bit equivalent.
    assert payload["passed"] is True, payload
    assert payload["save_world_size"] == 2
    assert payload["restore_world_size"] == 3
    assert payload["step_after_restore"] == 5  # 4 trained + 1 after restore

    for section in ("parameter_comparison", "moment1_comparison",
                    "moment2_comparison"):
        rep = payload[section]
        assert rep["failures"] == []
        assert rep["max_abs_diff"] < 1e-12, (section, rep["max_abs_diff"])
        # Every individual parameter checked, with concrete numeric bounds.
        assert len(rep["checks"]) == 4  # 2 layers x (weight, bias)
        assert all(c["passed"] for c in rep["checks"])


def test_finite_difference_oracle_validates_core_gradients(tmp_path):
    result = _run(tmp_path)
    # Independent numeric oracle agrees with analytic gradients.
    assert result.fd_max_rel_error < 1e-5


def test_reference_oracle_matches_hand_derived_scalar_adam_step():
    """The independent oracle is anchored to a fully hand-computed answer.

    1 linear layer, 2 classes, x=[1], target y=1, zero init:
        logits=[0,0] -> probs=[0.5,0.5] -> grad=[0.5,-0.5]
    With lr=0.1,b1=0.9,b2=0.999 the first bias-corrected Adam update is
        w_new=b_new=[-0.1, +0.1].
    """
    init = {
        "layers.0.weight": np.zeros((2, 1)),
        "layers.0.bias": np.zeros(2),
    }
    x = np.array([[1.0]])
    y = np.array([1])
    out = reference_adam_run(
        init, [(x, y)], n_layers=1,
        lr=0.1, beta1=0.9, beta2=0.999, eps=1e-8,
    )
    # Honest first update including the epsilon floor: step = -lr*g/(|g|+eps).
    g, lr, eps = 0.5, 0.1, 1e-8
    step0 = -lr * g / (abs(g) + eps)
    expected_bias = np.array([step0, -step0])
    np.testing.assert_allclose(out["params"]["layers.0.bias"],
                               expected_bias, rtol=0, atol=1e-15)
    np.testing.assert_allclose(out["params"]["layers.0.weight"],
                               expected_bias.reshape(2, 1), rtol=0, atol=1e-15)
    # Sanity: epsilon moves the answer by the expected ~2e-9 from 0.1.
    assert abs(expected_bias[0] + 0.1) == pytest.approx(2e-9, abs=1e-12)
    m1 = out["moments"]["layers.0.bias"][0]
    np.testing.assert_allclose(m1, np.array([0.05, -0.05]),
                               rtol=0, atol=1e-15)
    assert out["moments"]["layers.0.bias"][2] == 1
