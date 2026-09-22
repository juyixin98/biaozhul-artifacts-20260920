"""Rule versioning, rebuild isolation and rollback tests."""
from __future__ import annotations

from kex.db import session_scope
from kex.models import Entity, IndexGeneration, RuleVersion, WorkspaceState
from .conftest import auth, create_ws, drain


V1_TEXT = "北极星团队使用Kafka处理数据。"
V2_RULES = {
    "gazetteer": {
        "PERSON": [{"canonical": "赵六", "aliases": []}],
        "ORG": [{"canonical": "北极星", "aliases": ["北极星团队"]}],
        "TECH": [{"canonical": "Kafka", "aliases": []}],
    },
    "tech_patterns": [],
}
V3_RULES = {
    "gazetteer": {
        "PERSON": [{"canonical": "赵六", "aliases": []}],
        "ORG": [{"canonical": "北极星科技", "aliases": ["北极星团队", "北极星"]}],
        "TECH": [{"canonical": "Kafka", "aliases": []}],
    },
    "tech_patterns": [],
}


def _upload_and_extract(client, ws_id, key, text=V1_TEXT, title="d"):
    r = client.post(
        f"/api/workspaces/{ws_id}/documents",
        json={"text": text, "title": title},
        headers=auth(ws_id, key),
    )
    assert r.status_code in (200, 202)
    drain()
    return r.get_json()["document_id"]


def test_published_rules_are_immutable_rows(client):
    ws_id, key = create_ws(client, "ws-imm")
    h = auth(ws_id, key)
    _upload_and_extract(client, ws_id, key)
    client.post(f"/api/workspaces/{ws_id}/rules/publish",
                json={"rules": V2_RULES, "note": "v2"}, headers=h)
    drain()
    with session_scope() as db:
        rows = db.query(RuleVersion).filter_by(workspace_id=ws_id).order_by(
            RuleVersion.version
        ).all()
        assert len(rows) >= 2
        snapshots = [r.snapshot for r in rows]
        assert snapshots[0] != snapshots[-1]
        # status frozen
        assert all(r.status == "published" for r in rows)


def test_republish_same_rules_is_idempotent(client):
    ws_id, key = create_ws(client, "ws-repub")
    h = auth(ws_id, key)
    r1 = client.post(f"/api/workspaces/{ws_id}/rules/publish",
                     json={"rules": V2_RULES}, headers=h)
    drain()
    r2 = client.post(f"/api/workspaces/{ws_id}/rules/publish",
                     json={"rules": V2_RULES}, headers=h)
    assert r1.get_json()["version"] == r2.get_json()["version"]
    assert r2.get_json()["rebuild"].startswith("unchanged")


def test_rebuild_switches_generation_atomically_and_history_remains(client):
    ws_id, key = create_ws(client, "ws-rebuild")
    h = auth(ws_id, key)
    _upload_and_extract(client, ws_id, key)

    before = client.get(f"/api/workspaces/{ws_id}/rules", headers=h).get_json()
    v1_num = before["active_version"]

    # Publish v2: entities under v1 must remain queryable afterwards.
    pr = client.post(f"/api/workspaces/{ws_id}/rules/publish",
                     json={"rules": V2_RULES, "note": "org rename"}, headers=h)
    assert pr.status_code == 202
    gen2 = pr.get_json()["rebuild"]["index_generation"]
    drain()

    after = client.get(f"/api/workspaces/{ws_id}/rules", headers=h).get_json()
    assert after["active_version"] == v1_num + 1

    search = client.get(f"/api/workspaces/{ws_id}/search?q=北极星", headers=h).get_json()
    assert search["index_generation"] == gen2

    # Current entities resolve to the new canonical; v1 evidence is intact.
    cur = client.get(f"/api/workspaces/{ws_id}/entities", headers=h).get_json()
    orgs = [e for e in cur["entities"] if e["entity_type"] == "ORG"]
    assert {e["canonical_name"] for e in orgs} == {"北极星"}

    v1ents = client.get(
        f"/api/workspaces/{ws_id}/entities?rule_version={v1_num}", headers=h
    ).get_json()
    # v1 dictionary had no "北极星" org -> no such entity under v1.
    assert not [e for e in v1ents["entities"] if e["canonical_name"] == "北极星"]

    with session_scope() as db:
        gens = db.query(IndexGeneration).filter_by(workspace_id=ws_id).order_by(
            IndexGeneration.generation
        ).all()
        statuses = [g.status for g in gens]
        assert statuses.count("active") == 1
        assert "retired" in statuses


def test_rollback_restores_old_generation_without_rewrite(client):
    ws_id, key = create_ws(client, "ws-rollback")
    h = auth(ws_id, key)
    _upload_and_extract(client, ws_id, key)
    rules_before = client.get(f"/api/workspaces/{ws_id}/rules", headers=h).get_json()
    v1 = rules_before["active_version"]

    client.post(f"/api/workspaces/{ws_id}/rules/publish",
                json={"rules": V3_RULES}, headers=h)
    drain()
    mid = client.get(f"/api/workspaces/{ws_id}/rules", headers=h).get_json()
    assert mid["active_version"] == v1 + 1
    cur_orgs = client.get(f"/api/workspaces/{ws_id}/entities", headers=h).get_json()
    assert any(e["canonical_name"] == "北极星科技" for e in cur_orgs["entities"])

    rb = client.post(f"/api/workspaces/{ws_id}/rules/rollback",
                     json={"version": v1}, headers=h)
    assert rb.status_code == 200  # complete old generation exists -> switch
    body = rb.get_json()
    assert body["history_rewritten"] is False

    # v2/3 entity evidence still present (never rewritten).
    with session_scope() as db:
        rv2 = db.query(RuleVersion).filter_by(
            workspace_id=ws_id, version=v1 + 1
        ).one()
        assert db.query(Entity).filter_by(
            workspace_id=ws_id, rule_version_id=rv2.id,
            canonical_name="北极星科技"
        ).count() >= 1

    # Active search generation reads the old generation only.
    with session_scope() as db:
        state = db.get(WorkspaceState, ws_id)
        gen = db.get(IndexGeneration, state.active_index_generation_id)
        assert gen.rule_version_id == state.active_rule_version_id
        assert db.query(IndexGeneration).filter_by(
            workspace_id=ws_id, status="active"
        ).count() == 1


def test_rollback_to_version_without_generation_triggers_rebuild(client):
    ws_id, key = create_ws(client, "ws-rollback-rebuild")
    h = auth(ws_id, key)
    rules0 = client.get(f"/api/workspaces/{ws_id}/rules", headers=h).get_json()
    v1 = rules0["active_version"]
    _upload_and_extract(client, ws_id, key)

    client.post(f"/api/workspaces/{ws_id}/rules/publish",
                json={"rules": V2_RULES}, headers=h)
    drain()
    client.post(f"/api/workspaces/{ws_id}/rules/publish",
                json={"rules": V3_RULES}, headers=h)
    drain()
    # v2's generation got retired without ever being rolled back to; rolling
    # back to v2 may reuse its retired generation. Test the no-generation
    # path by deleting v2 generation to force a fresh rebuild.
    with session_scope() as db:
        rv2 = db.query(RuleVersion).filter_by(
            workspace_id=ws_id, version=v1 + 1
        ).one()
        db.query(IndexGeneration).filter_by(
            workspace_id=ws_id, rule_version_id=rv2.id
        ).delete(synchronize_session=False)
        db.commit()

    rb = client.post(f"/api/workspaces/{ws_id}/rules/rollback",
                     json={"version": v1 + 1}, headers=h)
    assert rb.status_code == 202
    assert "rebuild" in rb.get_json()
    drain()

    final = client.get(f"/api/workspaces/{ws_id}/rules", headers=h).get_json()
    assert final["active_version"] == v1 + 1
    search = client.get(f"/api/workspaces/{ws_id}/search?q=北极星", headers=h).get_json()
    assert search["total"] == 1


def test_active_rule_versions_are_workspace_scoped(client):
    a, ka = create_ws(client, "ws-a")
    b, kb = create_ws(client, "ws-b")
    client.post(f"/api/workspaces/{a}/rules/publish",
                json={"rules": V2_RULES}, headers=auth(a, ka))
    drain()
    # ws-b untouched: its active version remains the builtin v1.
    rb = client.get(f"/api/workspaces/{b}/rules", headers=auth(b, kb)).get_json()
    assert rb["active_version"] == 1
