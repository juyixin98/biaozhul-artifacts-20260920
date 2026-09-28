"""Layer 5: service facade + HTTP boundary, including explicit error mapping."""

from __future__ import annotations

import json

import numpy as np
import pytest
from fastapi.testclient import TestClient

from sparse_embedding.app import create_app
from sparse_embedding.errors import EmptyBatchError, ValidationBatchRejectedError


def make_app(tmp_path, **overrides):
    from conftest import make_config

    cfg = make_config(state_dir=str(tmp_path / "state"), **overrides)
    return create_app(cfg)


# --------------------------------------------------------------- facade


def test_service_apply_repeated_ids_and_counters(make_service):
    svc = make_service()
    report = svc.apply([0, 0, 2], [[1, 0, 0], [2, 0, 0], [0, 1, 0]],
                       run_id="svc-1", batch_id="b1")
    assert report.run_id == "svc-1"
    assert report.active_indices.tolist() == [0, 2]
    assert report.global_step_after == 1


def test_service_empty_batch_is_explicit_category(make_service):
    svc = make_service()
    with pytest.raises(EmptyBatchError) as exc:
        svc.apply([], [], run_id="svc-empty")
    assert exc.value.code == "empty_batch"
    assert svc.state.global_step == 0


def test_service_out_of_range_raises_and_is_rejected(make_service):
    svc = make_service()
    with pytest.raises(ValidationBatchRejectedError) as exc:
        svc.apply([0, 42], [[1, 1, 1], [1, 1, 1]], run_id="svc-bad")
    assert exc.value.code == "batch_rejected"
    assert svc.state.global_step == 0


# ------------------------------------------------------------------- HTTP


def test_http_apply_success(tmp_path):
    app = make_app(tmp_path)
    client = TestClient(app)
    resp = client.post("/apply", json={
        "run_id": "http-1", "batch_id": "bb",
        "indices": [0, 2, 0],
        "values": [[1, 0, 0], [0, 1, 0], [1, 0, 0]],
    })
    assert resp.status_code == 200
    body = resp.json()
    assert body["verdict"] == "applied"
    assert body["active_indices"] == [0, 2]
    assert body["n_active"] == 2
    assert body["global_step_after"] == 1


def test_http_out_of_range_returns_400_with_error_code(tmp_path):
    app = make_app(tmp_path)
    client = TestClient(app)
    resp = client.post("/apply", json={
        "run_id": "http-bad",
        "indices": [0, 8],
        "values": [[1, 1, 1], [1, 1, 1]],
    })
    assert resp.status_code == 400
    body = resp.json()
    assert body["error_code"] == "batch_rejected"
    assert body["run_id"] == "http-bad"


def test_http_width_mismatch_returns_400(tmp_path):
    app = make_app(tmp_path)
    client = TestClient(app)
    resp = client.post("/apply", json={
        "run_id": "http-width", "indices": [0], "values": [[1, 2, 3, 4]]
    })
    assert resp.status_code == 400
    assert resp.json()["error_code"] == "batch_rejected"


def test_http_empty_batch_reports_empty_verdict_not_applied(tmp_path):
    app = make_app(tmp_path)
    client = TestClient(app)
    resp = client.post("/apply", json={"run_id": "http-empty", "indices": [], "values": []})
    assert resp.status_code == 200
    body = resp.json()
    assert body["verdict"] == "empty"
    assert body["error_code"] == "empty_batch"
    assert body["global_step_after"] == body["global_step_before"] == 0


def test_http_malformed_json_shape_is_422(tmp_path):
    app = make_app(tmp_path)
    client = TestClient(app)
    # indices present but values missing -> request schema validation.
    resp = client.post("/apply", json={"run_id": "x", "indices": [0]})
    assert resp.status_code == 422


def test_http_row_and_summary_views(tmp_path):
    app = make_app(tmp_path)
    client = TestClient(app)
    client.post("/apply", json={"run_id": "v", "indices": [3], "values": [[1, 1, 1]]})
    row = client.get("/state/row/3").json()
    assert row["index"] == 3 and row["touched"] is True and row["row_steps"] == 1
    untouched = client.get("/state/row/4").json()
    assert untouched["touched"] is False and untouched["row_steps"] == 0
    summary = client.get("/state/summary").json()
    assert summary["n_touched"] == 1 and summary["global_step"] == 1
    assert client.get("/state/row/999").status_code == 404


def test_http_checkpoint_round_trip_across_app_instances(tmp_path):
    app = make_app(tmp_path)
    client = TestClient(app)
    client.post("/apply", json={"run_id": "p1", "indices": [1, 1],
                                 "values": [[2, 2, 2], [3, 3, 3]]})
    saved_row = client.get("/state/row/1").json()
    ckpt = client.post("/checkpoint")
    assert ckpt.status_code == 200
    assert ckpt.json()["verdict"] == "committed"

    # A brand new app instance on the same state_dir restores state.
    app2 = make_app(tmp_path)
    client2 = TestClient(app2)
    loaded_row = client2.get("/state/row/1").json()
    assert loaded_row["weight"] == saved_row["weight"]
    assert loaded_row["momentum"] == saved_row["momentum"]
    assert loaded_row["row_steps"] == 1


# --------------------------------------------------------------- journal


def test_journal_correlates_run_id_versions_and_verdict(make_service, tmp_path):
    log = tmp_path / "journal.jsonl"
    from sparse_embedding.journal import RunJournal

    svc = make_service()
    svc.journal = RunJournal(str(log))
    svc.apply([0, 0], [[1, 1, 1], [1, 1, 1]], run_id="corr-1", batch_id="bx")
    try:
        svc.apply([9], [[1, 1, 1]], run_id="corr-bad")
    except ValidationBatchRejectedError:
        pass

    lines = [json.loads(l) for l in log.read_text().splitlines()]
    assert len(lines) == 2
    applied, rejected = lines
    assert applied["run_id"] == "corr-1"
    assert applied["verdict"] == "applied"
    assert applied["stages"] == ["validate", "aggregate", "clip", "optimizer_step"]
    assert applied["versions"]["numpy"] == np.__version__
    assert applied["versions"]["service"]
    assert rejected["run_id"] == "corr-bad"
    assert rejected["verdict"] == "rejected"
    assert rejected["error_code"] == "batch_rejected"
    # The failure must not be disguised as success.
    assert all(l["verdict"] != "applied" or l["run_id"] != "corr-bad" for l in lines)


def test_unknown_exception_is_not_reported_as_success(make_service, tmp_path, monkeypatch):
    from sparse_embedding import state as state_mod

    svc = make_service()
    # Force a post-validation numerical failure inside the step.
    def boom(*a, **k):
        raise RuntimeError("synthetic failure")

    monkeypatch.setattr(state_mod, "step_rows", boom)
    with pytest.raises(RuntimeError):
        svc.apply([0], [[1, 1, 1]], run_id="boom")
    # State untouched and step not advanced.
    assert svc.state.global_step == 0
    assert 0 not in svc.state.touched_rows
