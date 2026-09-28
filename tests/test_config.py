"""Independent configuration layer: validation, closed enums, round-trip."""

from __future__ import annotations

import json

import pytest

from sparse_embedding.config import (
    ClipConfig,
    ClipMode,
    OptimizerConfig,
    OptimizerName,
    ServiceConfig,
    TableSpec,
)


def test_table_spec_rejects_non_positive_dims():
    with pytest.raises(ValueError):
        TableSpec(num_rows=0, dim=4)
    with pytest.raises(ValueError):
        TableSpec(num_rows=4, dim=0)


def test_clip_requires_positive_max_norm_when_enabled():
    with pytest.raises(ValueError):
        ClipConfig(mode=ClipMode.GLOBAL, max_norm=None)
    with pytest.raises(ValueError):
        ClipConfig(mode=ClipMode.ROW, max_norm=0.0)
    # None mode imposes no requirement.
    assert ClipConfig(mode=ClipMode.NONE).max_norm is None


def test_clip_modes_are_closed_and_distinct():
    assert {m.value for m in ClipMode} == {"none", "global", "row"}


def test_optimizer_validates_hyperparameters():
    with pytest.raises(ValueError):
        OptimizerConfig(lr=0.0)
    with pytest.raises(ValueError):
        OptimizerConfig(momentum=1.0)
    with pytest.raises(ValueError):
        OptimizerConfig(weight_decay=-0.1)


def test_plain_sgd_forbids_momentum():
    with pytest.raises(ValueError):
        OptimizerConfig(name=OptimizerName.SGD, momentum=0.9)


def test_step_zero_rows_cannot_be_enabled():
    # The explicit rule: zero-gradient rows never step; enabling it is rejected.
    with pytest.raises(ValueError) as exc:
        OptimizerConfig(step_zero_rows=True)
    assert "zero" in str(exc.value).lower()


def test_config_round_trips_through_json(tmp_path):
    cfg = ServiceConfig(
        table=TableSpec(num_rows=12, dim=5),
        optimizer=OptimizerConfig(name=OptimizerName.SGD_MOMENTUM, lr=0.05, momentum=0.8),
        clip=ClipConfig(mode=ClipMode.ROW, max_norm=3.0),
        state_dir=str(tmp_path / "state"),
        seed=42,
    )
    p = tmp_path / "cfg.json"
    cfg.dump(str(p))
    loaded = ServiceConfig.load(str(p))
    assert loaded.table.num_rows == 12 and loaded.table.dim == 5
    assert loaded.optimizer.lr == 0.05 and loaded.optimizer.momentum == 0.8
    assert loaded.clip.mode is ClipMode.ROW and loaded.clip.max_norm == 3.0
    assert loaded.seed == 42


def test_config_rejects_wrong_optimizer_enum_string(tmp_path):
    p = tmp_path / "bad.json"
    p.write_text(json.dumps({
        "table": {"num_rows": 4, "dim": 2},
        "optimizer": {"name": "adam"},
    }))
    with pytest.raises(ValueError):
        ServiceConfig.load(str(p))
