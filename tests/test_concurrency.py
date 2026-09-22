"""Concurrent rebuild, worker cap, and restart-recovery tests."""
from __future__ import annotations

import datetime as dt
import time

from kex.db import session_scope
from kex.models import (
    ExtractionJob,
    IndexGeneration,
    JobItem,
    WorkerHeartbeat,
    WorkspaceState,
)
from kex.services import jobs as jobs_service
from .conftest import auth, create_ws, drain, wait_drained


NEW_RULES = {
    "gazetteer": {
        "ORG": [{"canonical": "银河研究院", "aliases": ["银河"]}],
        "TECH": [{"canonical": "Kafka", "aliases": []}],
        "PERSON": [],
    },
    "tech_patterns": [],
}


def test_worker_cap_is_two(tmp_path, cfg, threaded_workers):
    tw = threaded_workers(2)
    time.sleep(0.3)  # let both register heartbeats
    with session_scope() as db:
        assert db.query(WorkerHeartbeat).count() == 2
        # A third worker must be denied registration while the two live.
        denied = jobs_service.register_worker(
            db, "intruder", max_workers=2, lease_seconds=cfg.worker_lease_seconds
        )
        assert denied is False
    tw.stop()
    time.sleep(0.2)
    with session_scope() as db:
        # Heartbeats are only reaped during registration; emulate restart by
        # clearing, then a fresh worker can join.
        db.query(WorkerHeartbeat).delete()
        ok = jobs_service.register_worker(
            db, "fresh", max_workers=2, lease_seconds=cfg.worker_lease_seconds
        )
        assert ok is True


def test_concurrent_workers_no_duplicate_entities(client, threaded_workers):
    ws_id, key = create_ws(client, "ws-concurrent")
    h = auth(ws_id, key)
    for i in range(12):
        client.post(
            f"/api/workspaces/{ws_id}/documents",
            json={"text": f"文档{i}：李明在二〇二四年三月{i % 9 + 1}日讨论Kafka。",
                  "title": f"c{i}"},
            headers=h,
        )
    threaded_workers(2)
    wait_drained()

    with session_scope() as db:
        items = db.query(JobItem).all()
        assert all(i.status in ("done", "skipped") for i in items)
        # Idempotency under concurrency: no duplicated mention rows.
        from sqlalchemy import text as sql_text

        rows = db.execute(
            sql_text(
                "select count(*) from (select 1 from entities "
                "group by workspace_id, document_id, rule_version_id, "
                "start_char, end_char, entity_type having count(*) > 1)"
            )
        ).scalar_one()
        assert rows == 0


def test_new_document_during_concurrent_rebuild_is_not_lost(client, cfg, threaded_workers):
    ws_id, key = create_ws(client, "ws-rebuild-concurrent")
    h = auth(ws_id, key)

    # Seed initial docs and process them.
    for i in range(5):
        client.post(
            f"/api/workspaces/{ws_id}/documents",
            json={"text": f"历史文档{i} Kafka 集群说明", "title": f"old{i}"}, headers=h,
        )
    drain("seed")

    # Publish new rules: a rebuild generation starts.
    pr = client.post(f"/api/workspaces/{ws_id}/rules/publish",
                     json={"rules": NEW_RULES}, headers=h).get_json()
    rebuild_gen = pr["rebuild"]["index_generation"]

    # Upload a brand-new document while rebuild items are queued. The
    # upload must enqueue items into BOTH generations.
    new_resp = client.post(
        f"/api/workspaces/{ws_id}/documents",
        json={"text": "新文档：银河研究院升级Kafka集群。", "title": "new-during-rebuild"},
        headers=h,
    )
    assert new_resp.status_code == 202

    threaded_workers(2)
    wait_drained()

    # New generation active and contains the new document's vocabulary.
    search = client.get(f"/api/workspaces/{ws_id}/search?q=银河", headers=h).get_json()
    if search["index_generation"] != rebuild_gen:
        with session_scope() as db0:
            print("DEBUG gens:", [(g.generation,g.status) for g in db0.query(IndexGeneration).all()])
            print("DEBUG jobs:", [(j.id,j.kind,j.status) for j in db0.query(ExtractionJob).order_by(ExtractionJob.id).all()])
            print("DEBUG items:", [(i.id,i.job_id,i.index_generation_id,i.status,i.stage,(i.last_error or "")[:40]) for i in db0.query(JobItem).order_by(JobItem.id).all()])
    assert search["index_generation"] == rebuild_gen
    assert search["total"] == 1
    assert search["results"][0]["title"] == "new-during-rebuild"

    with session_scope() as db:
        # The new doc produced postings in the rebuild generation.
        from sqlalchemy import func, select

        from kex.models import IndexPosting
        gen = db.query(IndexGeneration).filter_by(
            workspace_id=ws_id, generation=rebuild_gen
        ).one()
        count = db.scalar(
            select(func.count(IndexPosting.id)).where(
                IndexPosting.index_generation_id == gen.id,
                IndexPosting.workspace_document_id == search["results"][0]["document_id"],
            )
        )
        assert count > 0


def test_incremental_job_cannot_early_activate_rebuild(client):
    """An incremental item that feeds a building gen must not activate it
    before the rebuild's own items are done; the last finishing item does."""
    from kex.services import jobs as jobs_service

    ws_id, key = create_ws(client, "ws-early-activate")
    h = auth(ws_id, key)
    r = client.post(
        f"/api/workspaces/{ws_id}/documents",
        json={"text": "旧文档 Kafka 内容", "title": "old"}, headers=h,
    )
    assert r.status_code in (200, 202)
    drain("seed")

    pr = client.post(
        f"/api/workspaces/{ws_id}/rules/publish",
        json={"rules": NEW_RULES}, headers=h,
    ).get_json()
    rebuild_job_id = pr["rebuild"]["job_id"]
    rebuild_gen = pr["rebuild"]["index_generation"]

    # Upload during rebuild; its incremental job writes BOTH gens.
    client.post(
        f"/api/workspaces/{ws_id}/documents",
        json={"text": "银河研究院新文档 Kafka", "title": "new"}, headers=h,
    )

    with session_scope() as db:
        rebuild_items = db.query(JobItem).filter_by(
            job_id=rebuild_job_id
        ).all()
        assert rebuild_items
        # Pick the incremental items (any job, building gen) and process them
        # FIRST, while rebuild items are still queued.
        from sqlalchemy import select

        inc_items = db.scalars(
            select(JobItem)
            .join(ExtractionJob, ExtractionJob.id == JobItem.job_id)
            .where(ExtractionJob.kind == "incremental")
            .order_by(JobItem.id)
        ).all()
        for it in inc_items:
            claimed = jobs_service.ClaimedItem(
                item=db.get(JobItem, it.id),
                job=db.get(ExtractionJob, it.job_id),
            )
            jobs_service.process_item(db, claimed)
            jobs_service.maybe_finalize_job(db, it.job_id)

        # The rebuild generation must still be building (pointer not moved).
        gen = db.get(IndexGeneration, db.query(IndexGeneration).filter_by(
            workspace_id=ws_id, generation=rebuild_gen
        ).one().id)
        assert gen.status == "building"
        state = db.get(WorkspaceState, ws_id)
        assert state.active_index_generation_id != gen.id

    # Now finish the rebuild itself.
    drain("finish")
    with session_scope() as db:
        state = db.get(WorkspaceState, ws_id)
        active = db.get(IndexGeneration, state.active_index_generation_id)
        assert active.generation == rebuild_gen
        assert active.status == "active"
        job = db.get(ExtractionJob, rebuild_job_id)
        assert job.status == "succeeded"


def test_restart_recovery_resumes_expired_lease(client, cfg):
    """Simulate a worker crash mid-item: lease expires, another worker resumes."""
    ws_id, key = create_ws(client, "ws-restart")
    h = auth(ws_id, key)
    client.post(
        f"/api/workspaces/{ws_id}/documents",
        json={"text": "崩溃恢复测试：李明与Kafka在2024-01-02。", "title": "crash"},
        headers=h,
    )

    # Worker A claims the item then "dies" (never processes).
    with session_scope() as db:
        claimed = jobs_service.claim_item(db, "crashed-worker", lease_seconds=60)
        assert claimed is not None
        item_id = claimed.item.id

    # Before lease expiry, B cannot steal it.
    with session_scope() as db:
        stolen = jobs_service.claim_item(db, "b-worker", lease_seconds=60)
        assert stolen is None

    # Force the lease into the past (simulating 60s passing / restart).
    with session_scope() as db:
        item = db.get(JobItem, item_id)
        item.leased_until = dt.datetime.utcnow() - dt.timedelta(seconds=5)
        db.commit()

    with session_scope() as db:
        claimed2 = jobs_service.claim_item(db, "b-worker", lease_seconds=60)
        assert claimed2 is not None
        assert claimed2.item.id == item_id
        status = jobs_service.process_item(db, claimed2)
        jobs_service.maybe_finalize_job(db, claimed2.job.id)
        assert status == "done"

    ents = client.get(f"/api/workspaces/{ws_id}/entities", headers=h).get_json()
    assert ents["total"] > 0


def test_failed_item_retries_and_locates_document_and_stage(client, monkeypatch):
    ws_id, key = create_ws(client, "ws-retry")
    h = auth(ws_id, key)
    client.post(
        f"/api/workspaces/{ws_id}/documents",
        json={"text": "失败注入文档 李明 Kafka 2024-05-05", "title": "fail"},
        headers=h,
    )

    # Make the extract stage fail once.
    from kex.services import search_index as si

    real = si.index_document
    calls = {"n": 0}

    def flaky(db, gen_id, ws_doc, content):
        calls["n"] += 1
        if calls["n"] == 1:
            raise RuntimeError("simulated disk hiccup at index stage")
        return real(db, gen_id, ws_doc, content)

    monkeypatch.setattr(si, "index_document", flaky)

    processed = drain("flaky")
    assert any(status == "failed" for _id, status in processed)

    # Job status locates the failed document and stage.
    jobs = client.get(f"/api/workspaces/{ws_id}/jobs", headers=h).get_json()["jobs"]
    job = jobs[0]
    assert job["status"] == "partial"
    detail = job["failed_items"][0]
    assert detail["stage"] == "index"
    assert detail["document_id"] >= 1
    assert "simulated disk hiccup" in detail["error"]

    # Retry: second attempt succeeds; no duplicate entities on the retry.
    monkeypatch.undo()
    rr = client.post(
        f"/api/workspaces/{ws_id}/jobs/{job['job_id']}/retry", headers=h
    )
    assert rr.status_code == 200
    drain("retry")
    job2 = client.get(
        f"/api/workspaces/{ws_id}/jobs/{job['job_id']}", headers=h
    ).get_json()
    assert job2["status"] == "succeeded"

    ents = client.get(f"/api/workspaces/{ws_id}/entities", headers=h).get_json()
    keys = [(e["document_id"], e["start_char"], e["entity_type"]) for e in ents["entities"]]
    assert len(keys) == len(set(keys))
