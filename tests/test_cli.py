"""Command-line interface tests: real commands over a tmp config/storage."""

from __future__ import annotations

import json

import pytest

from adam_shard.__main__ import main

pytestmark = pytest.mark.unit


def _write_config(path, storage_root):
    cfg = {
        "storage_root": storage_root,
        "seed": 20260927,
        "dtype": "float64",
        "model": {"name": "mlp-tanh-cli", "dims": [3, 5, 4, 3]},
        "data": {"n_samples": 12},
        "adam": {"lr": 0.01, "beta1": 0.9, "beta2": 0.999, "eps": 1e-8},
        "train_steps": 2,
        "api": {"host": "127.0.0.1", "port": 8000},
    }
    with open(path, "w", encoding="utf-8") as fh:
        json.dump(cfg, fh)


def test_cli_init_train_list_validate_parity(tmp_path, capsys):
    cfg_path = tmp_path / "cfg.json"
    _write_config(str(cfg_path), str(tmp_path / "ckpt"))

    assert main(["--config", str(cfg_path), "init-data"]) == 0
    assert "wrote" in capsys.readouterr().out

    assert main(["--config", str(cfg_path), "train", "--world-size", "2", "--steps", "2"]) == 0
    train_out = json.loads(capsys.readouterr().out)
    commit = train_out["commit_id"]
    assert train_out["step"] == 2 and train_out["world_size"] == 2

    assert main(["--config", str(cfg_path), "list"]) == 0
    listing = json.loads(capsys.readouterr().out)
    assert any(c["commit_id"] == commit and c["loadable"] for c in listing)

    assert (
        main(["--config", str(cfg_path), "validate", commit, "--target-world-size", "3"])
        == 0
    )
    validated = json.loads(capsys.readouterr().out)
    assert validated["target_shard_sizes"] == [19, 19, 21]

    # Restore at 3 processes and compare one step with the independent oracle.
    rc = main(
        ["--config", str(cfg_path), "verify-parity", commit, "--target-world-size", "3"]
    )
    report = json.loads(capsys.readouterr().out)
    assert report["status"] == "pass"
    assert report["failures"] == []
    assert rc == 0


def test_cli_validate_unknown_commit_is_nonzero(tmp_path):
    cfg_path = tmp_path / "cfg.json"
    _write_config(str(cfg_path), str(tmp_path / "ckpt"))
    with pytest.raises(Exception):
        main(["--config", str(cfg_path), "validate", "ghost", "--target-world-size", "2"])
