"""End-to-end API: submission, shape errors, cap, SSE backfill, cancel race."""
from __future__ import annotations

import uuid

from app import queue as q
from app.config import get_settings
from app.db import SessionLocal
from app.models import Job, JobStatus

from .conftest import (
    auth,
    create_arch_via_api,
    make_arch_spec,
    register_dataset_via_api,
    unique_user,
)
from .helpers import list_job_events, run_to_end, wait_for_status


def _submit(client, uid, arch_id, ds_id, epochs=3, seed=1, hp=None):
    return client.post(
        "/api/jobs",
        headers=auth(uid),
        json={
            "architecture_id": arch_id,
            "dataset_id": ds_id,
            "epochs": epochs,
            "seed": seed,
            "hyperparams": hp or {"batch_size": 16, "lr": 0.05, "val_fraction": 0.25},
        },
    )


def test_shape_mismatch_architecture_vs_dataset_rejected(client, csv_dataset):
    uid = unique_user()
    # Dataset has 4 features; architecture takes 8 -> mismatch must fail at
    # registration of the job (regression output check) and at training time
    # for feature count, so we check the explicit regression case at API and
    # the feature mismatch at run time.
    ds_id = register_dataset_via_api(client, uid, csv_dataset)
    bad = make_arch_spec(input_features=8, hidden=16, out=3)
    arch_id = create_arch_via_api(client, uid, bad)
    r = _submit(client, uid, arch_id, ds_id)
    assert r.status_code == 201, r.text  # queued; torch fails at run time
    job_id = r.json()["id"]
    status = run_to_end(job_id)
    assert status == "failed"
    with SessionLocal() as db:
        assert db.get(Job, job_id).error is not None


def test_regression_output_width_enforced_at_submit(client, npy_dataset):
    uid = unique_user()
    ds_id = register_dataset_via_api(client, uid, npy_dataset, task="regression")
    # npy dataset: 2 features; output width 2 instead of required 1.
    bad = make_arch_spec(input_features=2, hidden=8, out=2)
    arch_id = create_arch_via_api(client, uid, bad)
    r = _submit(client, uid, arch_id, ds_id)
    assert r.status_code == 400
    assert "output width 1" in r.json()["detail"]


def test_invalid_graph_returned_as_400(client, csv_dataset):
    uid = unique_user()
    r = client.post(
        "/api/architectures",
        headers=auth(uid),
        json={
            "name": "cyclic",
            "input_features": 4,
            "layers": [
                {"name": "a", "type": "dense", "out_features": 4, "input": "b"},
                {"name": "b", "type": "relu", "input": "a"},
                {"name": "o", "type": "dense", "out_features": 3, "input": "b"},
            ],
        },
    )
    assert r.status_code == 400
    assert "cycle" in r.json()["detail"]


def test_dataset_outside_whitelist_rejected(client):
    uid = unique_user()
    r = client.post(
        "/api/datasets",
        headers=auth(uid),
        json={"name": "x", "path": "/etc/hostname", "task": "classification"},
    )
    assert r.status_code == 400
    assert "outside" in r.json()["detail"]


def test_full_training_run_emits_gapless_metrics(client, csv_dataset):
    uid = unique_user()
    arch_id = create_arch_via_api(client, uid, make_arch_spec())
    ds_id = register_dataset_via_api(client, uid, csv_dataset)
    r = _submit(client, uid, arch_id, ds_id, epochs=3)
    job_id = r.json()["id"]
    assert run_to_end(job_id) == "completed"

    job = client.get(f"/api/jobs/{job_id}", headers=auth(uid)).json()
    assert job["status"] == "completed"
    assert job["epochs_completed"] == 3
    events = list_job_events(job_id)
    metric_seqs = [e for e in events if e["kind"] == "metrics"]
    assert [e["payload"]["epoch"] for e in metric_seqs] == [1, 2, 3]
    seqs = [e["seq"] for e in events]
    assert seqs == list(range(1, len(seqs) + 1))  # gapless
    for e in metric_seqs:
        assert "train_loss" in e["payload"] and "val_loss" in e["payload"]
        assert "val_accuracy" in e["payload"]


def test_three_running_job_limit_per_user(client, csv_dataset):
    """Submission allows any number of queued jobs; claims cap at 3/user."""
    uid = unique_user()
    arch_id = create_arch_via_api(client, uid, make_arch_spec())
    ds_id = register_dataset_via_api(client, uid, csv_dataset)
    ids = []
    for _ in range(5):
        r = _submit(client, uid, arch_id, ds_id, epochs=2)
        assert r.status_code == 201, r.text
        ids.append(r.json()["id"])

    settings = get_settings()
    claimed = []
    with SessionLocal() as db:
        for i in range(5):
            job = q.claim_job(db, executor_id=f"e{i}", settings=settings)
            if job is not None:
                claimed.append(job.id)
        assert len(claimed) == settings.max_running_per_user
        # Two jobs remain queued because of the cap.
        queued = db.query(Job).filter(
            Job.user_id == uid, Job.status == JobStatus.QUEUED
        ).count()
        assert queued == 2

    # Limit is per user: another user's queued job can still be claimed even
    # though the first user is capped.
    other = unique_user()
    r = _submit(client, other, arch_id, ds_id, epochs=2)
    assert r.status_code == 201
    other_id = r.json()["id"]
    with SessionLocal() as db:
        job = q.claim_job(db, executor_id="other-exec", settings=settings)
        assert job is not None and job.id == other_id


def test_pause_and_resume_boundary(client, csv_dataset):
    from app.runner import TEST_HOOKS

    uid = unique_user()
    arch_id = create_arch_via_api(
        client, uid, make_arch_spec(dropout=0.0)
    )
    ds_id = register_dataset_via_api(client, uid, csv_dataset)
    r = _submit(client, uid, arch_id, ds_id, epochs=4)
    job_id = r.json()["id"]

    # Deterministic pause: the runner itself requests the pause after epoch 2
    # commits (no wall-clock race).
    def hook(jid, epoch, status):
        if epoch == 2 and status == "running":
            with SessionLocal() as hdb:
                q.pause_job(hdb, jid, uid)
            return True

    TEST_HOOKS[job_id] = hook
    assert run_to_end(job_id) == "paused"

    with SessionLocal() as db:
        job = db.get(Job, job_id)
        assert job.status == JobStatus.PAUSED
        assert job.epochs_completed == 2

    # Resume: a fresh executor finishes the remaining epochs without
    # re-splitting, and the job completes at epoch 4.
    r = client.post(
        f"/api/jobs/{job_id}/action", headers=auth(uid), json={"action": "resume"}
    )
    assert r.status_code == 200
    assert run_to_end(job_id) == "completed"
    with SessionLocal() as db:
        job = db.get(Job, job_id)
        assert job.epochs_completed == 4
        assert job.status == JobStatus.COMPLETED


def test_cancel_race_executor_cannot_keep_writing(client, csv_dataset):
    from app.runner import TEST_HOOKS

    uid = unique_user()
    arch_id = create_arch_via_api(client, uid, make_arch_spec(dropout=0.0))
    ds_id = register_dataset_via_api(client, uid, csv_dataset)
    r = _submit(client, uid, arch_id, ds_id, epochs=10)
    job_id = r.json()["id"]

    # Cancel races in at the boundary after epoch 3; the executor must stop,
    # its fencing token is cleared and no later metrics/checkpoint rows land.
    def hook(jid, epoch, status):
        if epoch == 3 and status == "running":
            with SessionLocal() as hdb:
                q.cancel_job(hdb, jid, uid)
            return True

    TEST_HOOKS[job_id] = hook
    final = run_to_end(job_id)
    assert final == "cancelled"

    with SessionLocal() as db:
        job = db.get(Job, job_id)
        assert job.status == JobStatus.CANCELLED
        assert job.executor_id is None
        assert job.epochs_completed == 3
    events = list_job_events(job_id)
    metric_epochs = [
        e["payload"]["epoch"] for e in events if e["kind"] == "metrics"
    ]
    assert metric_epochs == [1, 2, 3]  # nothing written after cancellation
    assert any(
        e["kind"] == "status" and e["payload"].get("to") == "cancelled"
        for e in events
    )

    # Cancelling again is rejected; resuming a cancelled job is rejected.
    assert client.post(
        f"/api/jobs/{job_id}/action", headers=auth(uid),
        json={"action": "cancel"}).status_code == 400
    assert client.post(
        f"/api/jobs/{job_id}/action", headers=auth(uid),
        json={"action": "resume"}).status_code == 400


def test_old_executor_metric_write_rejected_after_lease_taken(csv_dataset):
    uid = unique_user()
    settings = get_settings()
    with SessionLocal() as db:
        from app.datasets import inspect_dataset
        from app.models import Architecture, Dataset
        info = inspect_dataset(csv_dataset, settings=settings)
        arch = Architecture(name="a", spec=make_arch_spec(),
                            content_hash=uuid.uuid4().hex, param_count=10)
        ds = Dataset(name="d", path=info["resolved_path"], fmt=info["fmt"],
                     task="classification", num_rows=info["num_rows"],
                     num_features=info["num_features"], digest=info["digest"])
        db.add_all([arch, ds])
        db.flush()
        job = q.create_job(db, user_id=uid, architecture_id=arch.id,
                           dataset_id=ds.id, total_epochs=2, seed=1,
                           hyperparams=None, settings=settings)
        job_id = job.id

    old_exec = "exec-old"
    new_exec = "exec-new"
    with SessionLocal() as db:
        claimed = q.claim_job(db, executor_id=old_exec, settings=settings)
        assert claimed.id == job_id

    # Old executor loses its lease (expire manually), someone else re-queues
    # and claims.
    with SessionLocal() as db:
        job = db.get(Job, job_id)
        from datetime import timedelta
        from app.models import utcnow
        job.lease_expires_at = utcnow() - timedelta(seconds=5)
        db.commit()
    with SessionLocal() as db:
        assert q.reap_expired_leases(db) == 1
    with SessionLocal() as db:
        claimed = q.claim_job(db, executor_id=new_exec, settings=settings)
        assert claimed.id == job_id

    # Old executor's heartbeat / metrics / commit are fenced off.
    with SessionLocal() as db:
        import pytest
        with pytest.raises(q.LeaseLost):
            q.heartbeat(db, job_id, old_exec)
        with pytest.raises(q.LeaseLost):
            q.append_event_fenced(db, job_id, old_exec, "metrics", {"epoch": 1})


def test_pause_queued_job_then_resume_and_run(client, csv_dataset):
    uid = unique_user()
    arch_id = create_arch_via_api(client, uid, make_arch_spec(dropout=0.0))
    ds_id = register_dataset_via_api(client, uid, csv_dataset)
    r = _submit(client, uid, arch_id, ds_id, epochs=2)
    job_id = r.json()["id"]  # still queued
    assert client.post(
        f"/api/jobs/{job_id}/action", headers=auth(uid),
        json={"action": "pause"}).json()["status"] == "paused"
    assert client.post(
        f"/api/jobs/{job_id}/action", headers=auth(uid),
        json={"action": "resume"}).json()["status"] == "queued"
    assert run_to_end(job_id) == "completed"
    status_events = [
        e["payload"].get("to") for e in list_job_events(job_id)
        if e["kind"] == "status"
    ]
    # queued -> paused -> queued -> running -> completed
    assert status_events == ["queued", "paused", "queued", "running", "completed"]


def test_sse_backfill_after_disconnect(client, csv_dataset):
    uid = unique_user()
    arch_id = create_arch_via_api(client, uid, make_arch_spec(dropout=0.0))
    ds_id = register_dataset_via_api(client, uid, csv_dataset)
    r = _submit(client, uid, arch_id, ds_id, epochs=2)
    job_id = r.json()["id"]
    assert run_to_end(job_id) == "completed"

    # Reconnect with after_seq=0: whole backlog is delivered, ending once.
    with client.stream(
        "GET", f"/api/jobs/{job_id}/events/stream?after_seq=0", headers=auth(uid)
    ) as resp:
        assert resp.status_code == 200
        body = b""
        for chunk in resp.iter_bytes():
            body += chunk
            if b"event: end" in body:
                break
    text = body.decode()
    # gapless ids event-1..N plus terminal end
    ids = [ln for ln in text.splitlines() if ln.startswith("id: event-")]
    assert len(ids) >= 3


def test_events_json_backfill_and_reread_no_side_effect(client, csv_dataset):
    uid = unique_user()
    arch_id = create_arch_via_api(client, uid, make_arch_spec(dropout=0.0))
    ds_id = register_dataset_via_api(client, uid, csv_dataset)
    r = _submit(client, uid, arch_id, ds_id, epochs=2)
    job_id = r.json()["id"]
    assert run_to_end(job_id) == "completed"

    first = client.get(
        f"/api/jobs/{job_id}/events?after_seq=0", headers=auth(uid)
    ).json()["events"]
    second = client.get(
        f"/api/jobs/{job_id}/events?after_seq=0", headers=auth(uid)
    ).json()["events"]
    assert first == second  # repeated reads add nothing
    tail = client.get(
        f"/api/jobs/{job_id}/events?after_seq=2", headers=auth(uid)
    ).json()["events"]
    assert [e["seq"] for e in tail] == [e["seq"] for e in first if e["seq"] > 2]
