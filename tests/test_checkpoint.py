"""Tests for checkpoint persistence: atomic commit, corruption, weights-only rejection."""

import json
import os
import pickle

import numpy as np
import pytest

from checkpoint_service.checkpoint import (
    META_NAME,
    PAYLOAD_NAME,
    load_checkpoint,
    save_checkpoint,
)
from checkpoint_service.config import FORMAT_VERSION, TrainConfig
from checkpoint_service.errors import (
    CheckpointCorruptError,
    CheckpointFormatError,
    CheckpointNotFoundError,
    MissingCheckpointFieldError,
    UnsupportedCheckpointVersionError,
)
from checkpoint_service.trainer import Trainer


def _state() -> dict:
    trainer = Trainer.fresh(TrainConfig())
    return trainer.build_state()


@pytest.mark.unit
def test_roundtrip_restores_every_section(tmp_path: str) -> None:
    state = _state()
    save_checkpoint(tmp_path, state)
    loaded = load_checkpoint(tmp_path)
    assert loaded["format_version"] == FORMAT_VERSION
    np.testing.assert_array_equal(loaded["model"]["W"], state["model"]["W"])
    np.testing.assert_array_equal(loaded["optimizer"]["vW"], state["optimizer"]["vW"])
    np.testing.assert_array_equal(
        loaded["cursor"]["permutation"], state["cursor"]["permutation"]
    )
    for scalar_field in ("n_samples", "epoch", "pos", "global_step"):
        assert loaded["cursor"][scalar_field] == state["cursor"][scalar_field]
    assert loaded["rng_state"] == state["rng_state"]
    assert loaded["data_fingerprint"] == state["data_fingerprint"]


@pytest.mark.unit
def test_missing_checkpoint_raises_not_found(tmp_path: str) -> None:
    with pytest.raises(CheckpointNotFoundError):
        load_checkpoint(tmp_path)


@pytest.mark.unit
def test_meta_present_but_payload_missing_is_not_found(tmp_path: str) -> None:
    save_checkpoint(tmp_path, _state())
    os.remove(os.path.join(tmp_path, PAYLOAD_NAME))
    with pytest.raises(CheckpointNotFoundError):
        load_checkpoint(tmp_path)


@pytest.mark.unit
def test_payload_present_but_meta_missing_is_not_found(tmp_path: str) -> None:
    save_checkpoint(tmp_path, _state())
    os.remove(os.path.join(tmp_path, META_NAME))
    with pytest.raises(CheckpointNotFoundError):
        load_checkpoint(tmp_path)


@pytest.mark.unit
def test_bit_flip_in_payload_is_detected_as_corrupt(tmp_path: str) -> None:
    save_checkpoint(tmp_path, _state())
    payload_path = os.path.join(tmp_path, PAYLOAD_NAME)
    blob = bytearray(open(payload_path, "rb").read())
    blob[10] ^= 0xFF  # tamper
    with open(payload_path, "wb") as f:
        f.write(blob)
    with pytest.raises(CheckpointCorruptError, match="checksum"):
        load_checkpoint(tmp_path)


@pytest.mark.unit
def test_truncated_payload_is_detected_as_corrupt(tmp_path: str) -> None:
    save_checkpoint(tmp_path, _state())
    payload_path = os.path.join(tmp_path, PAYLOAD_NAME)
    blob = open(payload_path, "rb").read()[:-20]  # tear/truncate
    with open(payload_path, "wb") as f:
        f.write(blob)
    with pytest.raises(CheckpointCorruptError):
        load_checkpoint(tmp_path)


@pytest.mark.unit
def test_garbage_meta_is_corrupt(tmp_path: str) -> None:
    save_checkpoint(tmp_path, _state())
    with open(os.path.join(tmp_path, META_NAME), "wb") as f:
        f.write(b"{not json")
    with pytest.raises(CheckpointCorruptError):
        load_checkpoint(tmp_path)


@pytest.mark.unit
def test_checksum_field_tampered_is_corrupt(tmp_path: str) -> None:
    save_checkpoint(tmp_path, _state())
    meta_path = os.path.join(tmp_path, META_NAME)
    meta = json.loads(open(meta_path).read())
    meta["sha256"] = "0" * 64
    with open(meta_path, "w") as f:
        json.dump(meta, f)
    with pytest.raises(CheckpointCorruptError):
        load_checkpoint(tmp_path)


def _write_raw_state(tmp_path: str, state: dict) -> None:
    """Bypass validation to plant a structurally bad but well-checksummed blob."""
    blob = pickle.dumps(state, protocol=pickle.HIGHEST_PROTOCOL)
    with open(os.path.join(tmp_path, PAYLOAD_NAME), "wb") as f:
        f.write(blob)
    import hashlib

    with open(os.path.join(tmp_path, META_NAME), "w") as f:
        json.dump(
            {"sha256": hashlib.sha256(blob).hexdigest(), "size": len(blob), "format_version": 1},
            f,
        )


@pytest.mark.unit
def test_weights_only_checkpoint_is_rejected(tmp_path: str) -> None:
    """The acceptance requirement: weights alone must never be accepted."""
    state = _state()
    weights_only = {
        "format_version": FORMAT_VERSION,
        "model": state["model"],
    }
    _write_raw_state(tmp_path, weights_only)
    with pytest.raises(MissingCheckpointFieldError, match="weights-only"):
        load_checkpoint(tmp_path)


@pytest.mark.unit
@pytest.mark.parametrize("dropped", ["optimizer", "rng_state", "cursor"])
def test_checkpoint_missing_one_critical_section_is_rejected(
    tmp_path: str, dropped: str
) -> None:
    state = _state()
    del state[dropped]
    _write_raw_state(tmp_path, state)
    with pytest.raises(CheckpointFormatError):
        load_checkpoint(tmp_path)


@pytest.mark.unit
def test_optimizer_without_velocity_is_rejected(tmp_path: str) -> None:
    state = _state()
    state["optimizer"] = {"lr": state["optimizer"]["lr"], "momentum": 0.9}
    _write_raw_state(tmp_path, state)
    with pytest.raises(CheckpointFormatError, match="velocity"):
        load_checkpoint(tmp_path)


@pytest.mark.unit
def test_unknown_format_version_is_rejected(tmp_path: str) -> None:
    state = _state()
    state["format_version"] = 999
    _write_raw_state(tmp_path, state)
    with pytest.raises(UnsupportedCheckpointVersionError):
        load_checkpoint(tmp_path)


@pytest.mark.unit
def test_cursor_with_bad_permutation_is_rejected(tmp_path: str) -> None:
    state = _state()
    state["cursor"]["permutation"] = np.zeros(state["config"]["n_samples"], dtype=np.int64)
    _write_raw_state(tmp_path, state)
    with pytest.raises(CheckpointFormatError):
        load_checkpoint(tmp_path)


@pytest.mark.unit
def test_failed_validation_does_not_overwrite_good_checkpoint(tmp_path: str) -> None:
    good = _state()
    save_checkpoint(tmp_path, good)
    with pytest.raises(CheckpointFormatError):
        bad = dict(good)
        del bad["optimizer"]
        save_checkpoint(tmp_path, bad)
    loaded = load_checkpoint(tmp_path)
    np.testing.assert_array_equal(loaded["model"]["W"], good["model"]["W"])
