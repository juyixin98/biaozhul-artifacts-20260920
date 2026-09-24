"""End-to-end API tests against an in-process FastAPI TestClient."""

from __future__ import annotations

import json
import os
import time

from app.main import state
from app.storage import Storage

from conftest import BAG_SUMMARY, CALIBRATION, CALIBRATION_V2, PARAMS


def run_sync(client, snapshot_id: str, seed: int = 42, **extra):
    body = {"snapshot_id": snapshot_id, "seed": seed, **extra}
    r = client.post("/jobs/run-sync", json=body)
    assert r.status_code == 201, r.text
    return r.json()


def wait_for(client, job_id: str, timeout: float = 5.0) -> dict:
    deadline = time.time() + timeout
    while time.time() < deadline:
        job = client.get(f"/jobs/{job_id}").json()
        if job["status"] != "running":
            return job
        time.sleep(0.02)
    raise AssertionError("job did not finish in time")


# ------------------------------------------------------------------ basics
def test_health_and_public_key(client):
    assert client.get("/health").json()["status"] == "ok"
    key = client.get("/public-key").text
    assert "BEGIN PUBLIC KEY" in key


def test_bag_is_content_addressed(client):
    r = client.post(
        "/bags",
        files={"file": ("a.csv", b"0.1,0.2,1.0\n", "text/csv")},
        data={"summary": json.dumps(BAG_SUMMARY)},
    )
    assert r.status_code == 201
    d1 = r.json()["digest"]
    r = client.post(
        "/bags",
        files={"file": ("different-name.csv", b"0.1,0.2,1.0\n", "text/csv")},
        data={"summary": json.dumps(BAG_SUMMARY)},
    )
    d2 = r.json()["digest"]
    assert d1 == d2  # same bytes -> same digest, regardless of file name


# -------------------------------------------------------------- immutability
def test_snapshot_id_is_content_hash_and_signed(client, snapshot_id):
    snap = client.get(f"/snapshots/{snapshot_id}").json()
    v = client.get(f"/snapshots/{snapshot_id}/verify").json()
    assert v["signature_valid"] is True
    assert v["id_matches_manifest"] is True
    assert len(snapshot_id) == 64
    assert snap["signature"]


def test_identical_content_dedupes_to_same_snapshot(client, snapshot_id):
    snap = client.get(f"/snapshots/{snapshot_id}").json()
    r = client.post(
        "/snapshots",
        json={
            "bag_digest": snap["bag_digest"],
            "params": snap["params"],
            "calibration_id": snap["calibration_digest"],
            "algorithm_name": snap["algorithm"]["name"],
            "algorithm_version": snap["algorithm"]["version"],
        },
    )
    assert r.status_code == 201
    assert r.json()["id"] == snapshot_id


def test_publishing_new_calibration_does_not_change_job(client, snapshot_id):
    """Requirement: 发布新标定不改变运行结果 — jobs read the pinned snapshot."""
    j1 = run_sync(client, snapshot_id, seed=100)
    assert j1["reproducible"] is True

    # Publish a *new* calibration (different transform -> different id).
    r = client.post("/calibrations", json=CALIBRATION_V2)
    assert r.status_code == 201
    new_id = r.json()["id"]
    assert new_id != client.get(f"/snapshots/{snapshot_id}").json()["calibration_digest"]

    # Re-run the same immutable snapshot: the frozen calibration is used.
    j2 = run_sync(client, snapshot_id, seed=100)
    assert j2["attempt_index"] == 2
    assert j2["output_digest"] == j1["output_digest"]
    assert j2["results_summary"] == j1["results_summary"]


def test_new_calibration_creates_a_new_snapshot_when_bound(client, snapshot_id):
    snap = client.get(f"/snapshots/{snapshot_id}").json()
    v2 = client.post("/calibrations", json=CALIBRATION_V2).json()
    r = client.post(
        "/snapshots",
        json={
            "bag_digest": snap["bag_digest"],
            "params": snap["params"],
            "calibration_id": v2["id"],
            "algorithm_name": snap["algorithm"]["name"],
            "algorithm_version": snap["algorithm"]["version"],
        },
    )
    assert r.status_code == 201
    assert r.json()["id"] != snapshot_id


# ---------------------------------------------------------------- reproducibility
def test_reproducible_job_records_input_and_seed(client, snapshot_id):
    job = run_sync(client, snapshot_id, seed=2026)
    assert job["status"] == "completed"
    assert job["reproducible"] is True
    assert job["reproducibility_failures"] == []
    assert job["input_digest"] and job["seed"] == 2026
    assert job["input_digest"] == job["input_digest_end"]
    assert job["params_digest_start"] == job["params_digest_end"]
    # The artifact physically exists and carries input + seed.
    art = client.get(f"/jobs/{job['id']}/artifacts/result.json").json()
    assert art["input"]["seed"] == 2026
    assert art["input"]["input_digest"] == job["input_digest"]


def test_seed_is_part_of_identity(client, snapshot_id):
    a = run_sync(client, snapshot_id, seed=1)
    b = run_sync(client, snapshot_id, seed=2)
    assert a["input_digest"] != b["input_digest"]
    assert a["output_digest"] != b["output_digest"]


def test_missing_seed_or_input_blocks_reproducibility(client, snapshot_id):
    """缺一项不能标为可复现: directly corrupt a DB record then revalidate."""
    job = run_sync(client, snapshot_id, seed=5)
    db = state["store"]
    with db._lock:
        db._conn.execute(
            "UPDATE jobs SET input_digest=NULL WHERE id=?", (job["id"],)
        )
    out = client.post(f"/jobs/{job['id']}/revalidate").json()
    assert out["reproducible"] is False
    assert "missing_seed_or_input_record" in out["reproducibility_failures"]


def test_param_change_during_run_is_caught(client, snapshot_id):
    job = run_sync(client, snapshot_id, seed=9, simulate_param_change=True)
    assert job["status"] == "completed"  # output exists...
    assert job["reproducible"] is False  # ...but it is NOT reproducible
    fails = job["reproducibility_failures"]
    assert "parameters_changed_during_run" in fails
    assert "input_digest_mismatch_start_vs_end" in fails
    # Such a job must be refused at index publication time.
    r = client.post("/indexes", json={"job_ids": [job["id"]]})
    assert r.status_code == 409
    assert client.get("/indexes").json() == []


# ------------------------------------------------------------------ evidence
def test_repeated_runs_keep_separate_evidence(client, snapshot_id):
    j1 = run_sync(client, snapshot_id, seed=77)
    j2 = run_sync(client, snapshot_id, seed=77)  # identical content
    assert j1["id"] != j2["id"]
    assert j1["output_path"] != j2["output_path"]
    assert os.path.exists(j1["output_path"]) and os.path.exists(j2["output_path"])
    assert j1["output_digest"] == j2["output_digest"]  # same content
    assert j1["attempt_index"] == 1 and j2["attempt_index"] == 2


def test_swapped_evidence_is_detected(client, snapshot_id):
    job = run_sync(client, snapshot_id, seed=3)
    # Replace the test input / output evidence on disk.
    out = client.post(f"/jobs/{job['id']}/simulate-tamper?mode=swap").json()
    assert out["reproducible"] is False
    assert "output_digest_mismatch" in out["reproducibility_failures"]
    assert any("replaced" in e for e in out["tamper_events"])
    # Original evidence is preserved, never overwritten.
    kept = os.path.join(os.path.dirname(out["output_path"]), "result.json.original-evidence")
    assert os.path.exists(kept)


def test_missing_artifact_is_detected_and_blocks_publication(client, snapshot_id):
    job = run_sync(client, snapshot_id, seed=4)
    out = client.post(f"/jobs/{job['id']}/simulate-tamper?mode=missing").json()
    assert "output_artifact_missing" in out["reproducibility_failures"]
    assert out["reproducible"] is False
    r = client.post("/indexes", json={"job_ids": [job["id"]]})
    assert r.status_code == 409
    assert "output_artifact_missing" in r.json()["detail"]
    assert client.get("/indexes").json() == []  # nothing published


def test_replaced_input_bag_is_detected(client, snapshot_id, tmp_path):
    job = run_sync(client, snapshot_id, seed=6)
    bag_digest = client.get(f"/snapshots/{snapshot_id}").json()["bag_digest"]
    bag_path = state["store"]._bag_path(bag_digest)
    os.replace(bag_path, bag_path.with_suffix(".bin.held"))
    with open(bag_path, "wb") as fh:
        fh.write(b"0.0,0.0,0.0,1.0\n0.1,0.1,0.1,1.0\n")  # swapped test input
    out = client.post(f"/jobs/{job['id']}/revalidate").json()
    fails = out["reproducibility_failures"]
    assert "input_bag_digest_mismatch" in fails or "nondeterministic_rerun_result" in fails
    assert out["reproducible"] is False


# --------------------------------------------------------- validate-then-publish
def test_index_publishes_after_validation_and_verifies(client, snapshot_id):
    j1 = run_sync(client, snapshot_id, seed=11)
    j2 = run_sync(client, snapshot_id, seed=12)
    r = client.post("/indexes", json={"job_ids": [j1["id"], j2["id"]]})
    assert r.status_code == 201, r.text
    idx = r.json()
    assert idx["entry_count"] == 2
    assert idx["signature"]
    v = client.get(f"/indexes/{idx['id']}/verify").json()
    assert v["ok"] is True
    assert v["entries_checked"] == 2
    # Index files are append-only and numbered on disk.
    assert os.path.basename(idx["path"]) == "index-0001.json"


def test_index_rejects_duplicate_jobs(client, snapshot_id):
    j1 = run_sync(client, snapshot_id, seed=11)
    r = client.post("/indexes", json={"job_ids": [j1["id"], j1["id"]]})
    assert r.status_code == 409
    assert client.get("/indexes").json() == []


def test_cannot_publish_running_job(client, snapshot_id):
    r = client.post("/jobs", json={"snapshot_id": snapshot_id, "seed": 1, "hold_sec": 3})
    assert r.status_code == 202
    job_id = r.json()["id"]
    assert client.get(f"/jobs/{job_id}").json()["status"] == "running"
    r = client.post("/indexes", json={"job_ids": [job_id]})
    assert r.status_code == 409
    assert "job_still_running" in r.json()["detail"]
    wait_for(client, job_id)


# ------------------------------------------------------------------- restart
def test_restart_recovers_running_job(tmp_path, monkeypatch):
    monkeypatch.setenv("SNAPSHOT_ROOT", str(tmp_path / "data"))
    from app.main import app
    from fastapi.testclient import TestClient

    with TestClient(app) as c:
        c.post("/calibrations", json=CALIBRATION)
        c.post(
            "/bags",
            files={"file": ("b.csv", b"0.1,0.2,1.0\n0.3,0.4,1.1\n")},
            data={"summary": json.dumps(BAG_SUMMARY)},
        )
        bag_digest = c.get("/bags").json()[0]["digest"]
        calib_id = c.get("/calibrations").json()[0]["id"]
        sid = c.post(
            "/snapshots",
            json={"bag_digest": bag_digest, "params": PARAMS,
                  "calibration_id": calib_id,
                  "algorithm_name": "pc", "algorithm_version": "1.0.0"},
        ).json()["id"]
        r = c.post("/jobs", json={"snapshot_id": sid, "seed": 8, "hold_sec": 5})
        job_id = r.json()["id"]
        assert c.get(f"/jobs/{job_id}").json()["status"] == "running"

    # --- process dies; open a fresh Storage against the same directory ---
    fresh = Storage(str(tmp_path / "data"))
    recovered = fresh.recover_after_restart()
    assert recovered == [job_id]
    job = fresh.get_job(job_id)
    assert job["status"] == "failed"
    assert job["reproducible"] is False
    assert any("restart" in e for e in job["tamper_events"])
    assert "recovered_after_restart" in job["reproducibility_failures"]
    assert "job_not_completed" in job["reproducibility_failures"]
    # Index publication of a recovered job is refused.
    import pytest as _pytest
    from app.storage import Conflict
    with _pytest.raises(Conflict):
        fresh.publish_index([job_id])
    # Booting the API again reports the recovered job.
    with TestClient(app) as c2:
        assert c2.get("/health").json()["recovered_at_boot"] == []  # already settled
        assert c2.get(f"/jobs/{job_id}").json()["status"] == "failed"


def test_completed_job_survives_restart_and_still_verifies(client, snapshot_id):
    job = run_sync(client, snapshot_id, seed=99)
    # Simulate restart: new Storage instance, same files/key.
    fresh = Storage(str(state["store"].root))
    assert fresh.recover_after_restart() == []
    v = fresh.revalidate_job(job["id"])
    assert v["reproducible"] is True
    idx = fresh.publish_index([job["id"]])
    assert fresh.verify_index(idx["id"])["ok"] is True
