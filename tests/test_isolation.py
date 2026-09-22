"""Deletion races, purge guarantees, cross-workspace isolation, migrations."""
from __future__ import annotations

import sqlite3

from kex.db import session_scope
from kex.models import (
    Document,
    Entity,
    IndexDocumentStat,
    IndexGeneration,
    IndexPosting,
    JobItem,
    WorkspaceDocument,
)
from kex.services import jobs as jobs_service
from .conftest import auth, create_ws, drain


def _upload(client, ws_id, key, text, title="d"):
    r = client.post(
        f"/api/workspaces/{ws_id}/documents",
        json={"text": text, "title": title},
        headers=auth(ws_id, key),
    )
    assert r.status_code in (200, 202)
    return r.get_json()["document_id"]


def test_deleted_document_vanishes_from_search_entities_export(client):
    ws_id, key = create_ws(client, "ws-delete")
    h = auth(ws_id, key)
    doc_id = _upload(client, ws_id, key, "李明在2024-07-07部署Kafka集群", "secret")
    drain()
    assert client.get(f"/api/workspaces/{ws_id}/search?q=Kafka", headers=h).get_json()["total"] == 1

    r = client.delete(f"/api/workspaces/{ws_id}/documents/{doc_id}", headers=h)
    assert r.status_code == 200

    assert client.get(f"/api/workspaces/{ws_id}/search?q=Kafka", headers=h).get_json()["total"] == 0
    assert client.get(f"/api/workspaces/{ws_id}/entities", headers=h).get_json()["total"] == 0
    export = client.get(f"/api/workspaces/{ws_id}/export", headers=h).data.decode()
    assert "Kafka" not in export and "2024-07-07" not in export
    # Direct fetch is 404.
    assert client.get(f"/api/workspaces/{ws_id}/documents/{doc_id}", headers=h).status_code == 404


def test_deleted_document_purged_from_every_generation(client):
    ws_id, key = create_ws(client, "ws-del-gens")
    h = auth(ws_id, key)
    doc_id = _upload(client, ws_id, key, "待删文档 Kafka 与清华", "purge")
    drain()
    new_rules = {
        "gazetteer": {
            "ORG": [{"canonical": "新组织", "aliases": ["清华"]}],
            "PERSON": [], "TECH": [{"canonical": "Kafka", "aliases": []}],
        },
        "tech_patterns": [],
    }
    client.post(f"/api/workspaces/{ws_id}/rules/publish",
                json={"rules": new_rules}, headers=h)
    drain()
    # Now three generations exist (active v2 + retired v1) — purge all.
    client.delete(f"/api/workspaces/{ws_id}/documents/{doc_id}", headers=h)
    with session_scope() as db:
        assert db.query(IndexPosting).filter_by(workspace_document_id=doc_id).count() == 0
        assert db.query(IndexDocumentStat).filter_by(workspace_document_id=doc_id).count() == 0
        assert db.query(Entity).filter_by(workspace_document_id=doc_id).count() == 0
        assert db.query(JobItem).filter_by(workspace_document_id=doc_id).count() == 0


def test_delete_then_reprocessing_race_is_safe(client):
    """Item claimed, document deleted before the worker writes -> skipped."""
    ws_id, key = create_ws(client, "ws-del-race")
    h = auth(ws_id, key)
    doc_id = _upload(client, ws_id, key, "删除竞争 李明 Kafka 2024-08-08", "race")

    with session_scope() as db:
        claimed = jobs_service.claim_item(db, "slow-worker", lease_seconds=120)
        assert claimed is not None

    # Another session deletes the document while the item is leased.
    client.delete(f"/api/workspaces/{ws_id}/documents/{doc_id}", headers=h)

    with session_scope() as db:
        status = jobs_service.process_item(db, claimed)
        assert status == "skipped"
        jobs_service.maybe_finalize_job(db, claimed.job.id)

    # No entities/postings leaked despite the worker completing afterwards.
    with session_scope() as db:
        assert db.query(Entity).filter_by(workspace_id=ws_id).count() == 0
        assert db.query(IndexPosting).count() == 0


def test_shared_blob_isolation_between_workspaces(client):
    a, ka = create_ws(client, "ws-blob-a")
    b, kb = create_ws(client, "ws-blob-b")
    text = "共享内容：联合国在2024-01-10开会，Kafka提供支持。"
    id_a = _upload(client, a, ka, text, "shared-in-a")
    id_b = _upload(client, b, kb, text, "shared-in-b")
    drain()

    # Same physical content blob, distinct workspace links.
    with session_scope() as db:
        wd_a = db.get(WorkspaceDocument, id_a)
        wd_b = db.get(WorkspaceDocument, id_b)
        assert wd_a.document_id == wd_b.document_id
        blob = db.get(Document, wd_a.document_id)
        assert blob.sha256

    # A cannot read B's link or entities, and vice versa.
    assert client.get(f"/api/workspaces/{a}/documents/{id_b}",
                      headers=auth(a, ka)).status_code == 404
    assert client.get(f"/api/workspaces/{b}/documents/{id_a}",
                      headers=auth(b, kb)).status_code == 404
    ent_a = client.get(f"/api/workspaces/{a}/entities", headers=auth(a, ka)).get_json()
    ent_b = client.get(f"/api/workspaces/{b}/entities", headers=auth(b, kb)).get_json()
    assert {e["document_id"] for e in ent_a["entities"]} == {id_a}
    assert {e["document_id"] for e in ent_b["entities"]} == {id_b}

    # Deleting in A must not damage B (blob refcount keeps it alive).
    client.delete(f"/api/workspaces/{a}/documents/{id_a}", headers=auth(a, ka))
    content = client.get(f"/api/workspaces/{b}/documents/{id_b}",
                         headers=auth(b, kb)).get_json()
    assert content["content"] == text
    search = client.get(f"/api/workspaces/{b}/search?q=Kafka",
                        headers=auth(b, kb)).get_json()
    assert search["total"] == 1

    with session_scope() as db:
        # A's entities are gone; B's survive.
        assert db.query(Entity).filter_by(workspace_id=a).count() == 0
        assert db.query(Entity).filter_by(workspace_id=b).count() > 0


def test_foreign_workspace_job_and_rules_inaccessible(client):
    a, ka = create_ws(client, "ws-fa")
    b, kb = create_ws(client, "ws-fb")
    _upload(client, a, ka, "李明 Kafka 2024-01-01", "a-doc")
    drain()
    jobs = client.get(f"/api/workspaces/{a}/jobs", headers=auth(a, ka)).get_json()["jobs"]
    job_id = jobs[0]["job_id"]
    # B cannot inspect A's job.
    assert client.get(f"/api/workspaces/{b}/jobs/{job_id}",
                      headers=auth(b, kb)).status_code == 404
    # B cannot rollback A's rule versions.
    rb = client.post(f"/api/workspaces/{b}/rules/rollback",
                     json={"version": 999}, headers=auth(b, kb))
    assert rb.status_code == 404


def test_search_only_reads_active_generation_during_rebuild(client):
    ws_id, key = create_ws(client, "ws-gens-read")
    h = auth(ws_id, key)
    _upload(client, ws_id, key, "旧索引词 Kafka 稳定可查", "old")
    drain()

    new_rules = {"gazetteer": {"PERSON": [], "ORG": [{"canonical": "新机构", "aliases": []}],
                               "TECH": [{"canonical": "Kafka", "aliases": []}]},
                 "tech_patterns": []}
    client.post(f"/api/workspaces/{ws_id}/rules/publish",
                json={"rules": new_rules}, headers=h)
    # Do NOT drain: rebuild generation is half-built (actually unbuilt here).
    with session_scope() as db:
        active = db.query(IndexGeneration).filter_by(
            workspace_id=ws_id, status="active"
        ).one()
        building = db.query(IndexGeneration).filter_by(
            workspace_id=ws_id, status="building"
        ).one()
        assert active.id != building.id
    # Queries still answer from the single complete old generation.
    resp = client.get(f"/api/workspaces/{ws_id}/search?q=Kafka", headers=h).get_json()
    assert resp["total"] == 1
    # No query path can reference the building generation.
    with session_scope() as db:
        from kex.models import WorkspaceState
        state = db.get(WorkspaceState, ws_id)
        assert resp["index_generation"] != building.generation
        assert state.active_index_generation_id == active.id


def test_repeated_extraction_does_not_duplicate(client):
    ws_id, key = create_ws(client, "ws-idem")
    h = auth(ws_id, key)
    _upload(client, ws_id, key, "李明 李明 王芳 Kafka 2024-02-02", "dup")
    drain("first")
    # Manually re-run the same items (simulates duplicate delivery).
    with session_scope() as db:
        from kex.models import JobItem
        for item in db.query(JobItem).all():
            item.status = "queued"
            item.stage = "queued"
            item.leased_by = None
            item.leased_until = None
        from kex.models import ExtractionJob
        for job in db.query(ExtractionJob).all():
            job.status = "queued"
            job.finished_at = None
        db.commit()
    drain("second")
    ents = client.get(f"/api/workspaces/{ws_id}/entities", headers=h).get_json()
    keys = [(e["document_id"], e["start_char"], e["entity_type"]) for e in ents["entities"]]
    assert len(keys) == len(set(keys))


def test_migration_creates_schema_in_wal(migrated_db_path):
    """The shipped Alembic migration alone produces the working schema."""
    con = sqlite3.connect(migrated_db_path)
    assert con.execute("PRAGMA journal_mode").fetchone()[0].lower() == "wal"
    tables = {r[0] for r in con.execute(
        "select name from sqlite_master where type='table'"
    )}
    assert {"workspaces", "documents", "workspace_documents", "rule_versions",
            "workspace_states", "extraction_jobs", "job_items", "entities",
            "index_generations", "index_terms", "index_postings",
            "index_document_stats", "worker_heartbeats"} <= tables
    # FK enforcement is encoded in schema DDL.
    fk = con.execute("pragma foreign_key_list(entities)").fetchall()
    assert fk  # entities has foreign keys
