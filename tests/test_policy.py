"""Policy versioning: immutable once published; updates don't extend grants."""
from __future__ import annotations

import pytest
from sqlalchemy import text

from app.db import engine
from tests.conftest import auth


def test_published_versions_are_immutable_in_database(client, org):
    client.post("/policies", json={"body": "v1"}, headers=auth(org["admin"]))

    # Direct UPDATE and DELETE must each be rejected by the trigger.
    with engine.connect() as conn:
        with pytest.raises(Exception) as upd:
            conn.execute(text("UPDATE policy_versions SET body='tampered' WHERE version=1"))
        assert "immutable" in str(upd.value).lower()

    with engine.connect() as conn:
        with pytest.raises(Exception) as dele:
            conn.execute(text("DELETE FROM policy_versions WHERE version=1"))
        assert "immutable" in str(dele.value).lower()

    rows = client.get("/policies", headers=auth(org["auditor"])).json()
    assert len(rows) == 1 and rows[0]["body"] == "v1"


def test_policy_versions_are_monotonic(client, org):
    v1 = client.post("/policies", json={"body": "a"}, headers=auth(org["admin"])).json()
    v2 = client.post("/policies", json={"body": "b"}, headers=auth(org["admin"])).json()
    assert v1["version"] == 1 and v2["version"] == 2


def test_new_policy_does_not_rebind_or_extend_existing_grant(client, org):
    client.post("/policies", json={"body": "v1 terms"}, headers=auth(org["admin"]))
    grant = client.post(
        "/consents",
        json={
            "event_id": "g1",
            "subject_key": "u",
            "purpose": "analytics",
            "action": "grant",
            "expected_version": 0,
        },
        headers=auth(org["admin"]),
    )
    assert grant.status_code == 201
    assert grant.json()["policy_version"] == 1

    # Publish v2 after the grant.
    client.post("/policies", json={"body": "v2 terms"}, headers=auth(org["admin"]))

    # The standing consent is still based on v1.
    state = client.get("/subjects/u/consents/analytics", headers=auth(org["auditor"])).json()
    assert state["valid"] is True
    assert state["policy_version"] == 1

    # A new grant must explicitly target the new current version (default pins
    # the latest), it is not carried over silently.
    new = client.post(
        "/consents",
        json={
            "event_id": "g2",
            "subject_key": "u",
            "purpose": "analytics",
            "action": "grant",
            "expected_version": 1,
        },
        headers=auth(org["admin"]),
    )
    assert new.status_code == 201
    assert new.json()["policy_version"] == 2


def test_grant_without_any_policy_rejected(client, org):
    r = client.post(
        "/consents",
        json={
            "event_id": "g0",
            "subject_key": "u",
            "purpose": "x",
            "action": "grant",
            "expected_version": 0,
        },
        headers=auth(org["admin"]),
    )
    assert r.status_code == 422
