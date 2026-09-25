"""Tests for the service layer."""

import pytest

from checkpoint_service.config import TrainConfig
from checkpoint_service.errors import (
    InvalidRequestError,
    RunExistsError,
    RunNotFoundError,
)
from checkpoint_service.service import TrainingService


@pytest.mark.unit
def test_create_and_resume_training_lifecycle(tmp_path: str) -> None:
    svc = TrainingService(str(tmp_path))
    created = svc.create_run("r1", TrainConfig(n_epochs=1))
    assert created["state"]["global_step"] == 0

    part = svc.train("r1", stop_after=3)
    assert part["global_step"] == 3
    assert not part["finished"]

    again = svc.train("r1", stop_after=3)
    assert again["global_step"] == 6

    full = svc.train("r1")
    assert full["finished"]
    assert full["global_step"] == TrainConfig(n_epochs=1).total_steps

    status = svc.status("r1")
    assert status["history_len"] == status["total_steps"]


@pytest.mark.unit
def test_duplicate_run_is_rejected(tmp_path: str) -> None:
    svc = TrainingService(str(tmp_path))
    svc.create_run("r", TrainConfig())
    with pytest.raises(RunExistsError):
        svc.create_run("r", TrainConfig())


@pytest.mark.unit
def test_unknown_run_raises(tmp_path: str) -> None:
    svc = TrainingService(str(tmp_path))
    with pytest.raises(RunNotFoundError):
        svc.status("ghost")
    with pytest.raises(RunNotFoundError):
        svc.train("ghost")


@pytest.mark.unit
def test_run_id_validation(tmp_path: str) -> None:
    svc = TrainingService(str(tmp_path))
    for bad in ["", "../escape", "a/b", "x y"]:
        with pytest.raises(InvalidRequestError):
            svc.create_run(bad, TrainConfig())


@pytest.mark.unit
def test_list_runs_reports_corruption(tmp_path: str) -> None:
    svc = TrainingService(str(tmp_path))
    svc.create_run("good", TrainConfig())
    svc.create_run("bad", TrainConfig())
    import os

    payload = os.path.join(str(tmp_path), "bad", "checkpoint.payload")
    blob = bytearray(open(payload, "rb").read())
    blob[5] ^= 0xFF
    open(payload, "wb").write(blob)

    runs = {r["run_id"]: r for r in svc.list_runs()}
    assert runs["good"]["committed"]
    assert "corrupt" in runs["bad"]["status"]


@pytest.mark.unit
def test_inspect_checkpoint_lists_all_required_sections(tmp_path: str) -> None:
    svc = TrainingService(str(tmp_path))
    svc.create_run("r", TrainConfig())
    info = svc.inspect_checkpoint("r")
    assert info["has_model"]
    assert info["has_optimizer_state"]
    assert info["has_rng_state"]
    assert info["has_cursor"]
