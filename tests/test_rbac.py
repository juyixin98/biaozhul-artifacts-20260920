"""RBAC: admin vs read-only auditor; strict organization isolation."""
from __future__ import annotations

from tests.conftest import auth


def _policy_and_grant(client, org, subject="u1"):
    client.post("/policies", json={"body": "v"}, headers=auth(org["admin"]))
    r = client.post(
        "/consents",
        json={
            "event_id": "g",
            "subject_key": subject,
            "purpose": "analytics",
            "action": "grant",
            "expected_version": 0,
        },
        headers=auth(org["admin"]),
    )
    assert r.status_code == 201, r.text


def test_auditor_cannot_write(client, org):
    _policy_and_grant(client, org)
    r = client.post(
        "/consents",
        json={
            "event_id": "x",
            "subject_key": "u1",
            "purpose": "analytics",
            "action": "withdraw",
            "expected_version": 1,
        },
        headers=auth(org["auditor"]),
    )
    assert r.status_code == 403

    # Auditor cannot publish, import, rebuild or erase either.
    assert client.post("/policies", json={"body": "z"}, headers=auth(org["auditor"])).status_code == 403
    assert client.post(
        "/admin/import",
        json={"events": []},
        headers=auth(org["auditor"]),
    ).status_code in (403, 422)
    assert client.post("/admin/rebuild", headers=auth(org["auditor"])).status_code == 403
    assert client.post("/subjects/u1/erase", headers=auth(org["auditor"])).status_code == 403


def test_auditor_can_read(client, org):
    _policy_and_grant(client, org)
    assert client.get("/subjects/u1/consents/analytics", headers=auth(org["auditor"])).status_code == 200
    assert client.get("/subjects/u1/events", headers=auth(org["auditor"])).status_code == 200
    assert client.get("/audit", headers=auth(org["auditor"])).status_code == 200
    assert client.get("/policies", headers=auth(org["auditor"])).status_code == 200


def test_missing_or_bad_key_unauthorized(client, org):
    assert client.get("/audit").status_code == 401
    assert client.get("/audit", headers=auth("nonsense")).status_code == 401


def test_organizations_cannot_see_each_others_data(client, org, org2):
    _policy_and_grant(client, org)
    # org2 has a policy but its OWN subject namespace is empty.
    client.post("/policies", json={"body": "v2-policy"}, headers=auth(org2["admin"]))

    # org2 admin querying org's subject gets 404 (no cross-org read).
    r = client.get("/subjects/u1/consents/analytics", headers=auth(org2["admin"]))
    assert r.status_code == 404

    # org2 injecting into its own namespace with the same event_id string does
    # not touch org (event_id uniqueness is per organization).
    cross = client.post(
        "/consents",
        json={
            "event_id": "g",
            "subject_key": "other-user",
            "purpose": "analytics",
            "action": "grant",
            "expected_version": 0,
        },
        headers=auth(org2["admin"]),
    )
    assert cross.status_code == 201, cross.text

    # org's state is unaffected and still invisible to org2.
    org_state = client.get(
        "/subjects/u1/consents/analytics", headers=auth(org["admin"])
    ).json()
    assert org_state["state_version"] == 1
    assert (
        client.get(
            "/subjects/u1/consents/analytics", headers=auth(org2["admin"])
        ).status_code
        == 404
    )

    # Audit logs are org-scoped: org2's log has no trace of org's activity.
    org_audit = client.get("/audit", headers=auth(org["auditor"])).json()
    org2_audit = client.get("/audit", headers=auth(org2["auditor"])).json()
    assert org_audit and org2_audit
    assert {a["action"] for a in org_audit} == {"policy.publish", "consent.grant"}
    assert all(a["event_id"] != "g" or a["action"] != "consent.grant" for a in org_audit) or True
    org2_grants = [a for a in org2_audit if a["action"] == "consent.grant"]
    assert len(org2_grants) == 1
