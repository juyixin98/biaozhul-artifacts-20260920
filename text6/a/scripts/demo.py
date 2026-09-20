#!/usr/bin/env python3
"""End-to-end demo of ConsentVault against a running API.

Usage:
    python scripts/demo.py
Environment:
    BASE_URL                     API base URL (default http://localhost:8010)
    CONSENTVAULT_MANAGEMENT_KEY  management key (default change-me-management-key)

The script walks through: org provisioning, purpose + policy publish, grant,
idempotent retry, stale-version conflict, verification before/after expiry,
policy update (no auto-renewal), withdrawal with delayed retry protection,
rebuild equivalence, batch import rollback and post-erasure queries.
"""

from __future__ import annotations

import os
import sys
import time
import uuid
from datetime import datetime, timedelta, timezone

import httpx

BASE_URL = os.environ.get("BASE_URL", "http://localhost:8010")
MGMT_KEY = os.environ.get("CONSENTVAULT_MANAGEMENT_API_KEY", "change-me-management-key")


def banner(title: str) -> None:
    print(f"\n=== {title} ===")


def main() -> int:
    client = httpx.Client(base_url=BASE_URL, timeout=30)
    r = client.get("/healthz")
    r.raise_for_status()
    print("health:", r.json())

    # 1. Provision an organization.
    banner("1. provision organization")
    org_name = f"demo-{uuid.uuid4().hex[:8]}"
    r = client.post(
        "/management/organizations",
        json={"name": org_name},
        headers={"X-Management-Key": MGMT_KEY},
    )
    r.raise_for_status()
    org = r.json()
    admin_key = org["admin_api_key"]
    auditor_key = org["auditor_api_key"]
    print("organization:", org["organization_id"], org_name)
    admin_h = {"X-API-Key": admin_key}
    auditor_h = {"X-API-Key": auditor_key}

    # 2. Purpose + policy v1/v2.
    banner("2. purpose and immutable policy versions")
    r = client.post("/api/v1/purposes", json={"key": "marketing", "description": "demo"},
                    headers=admin_h)
    r.raise_for_status()
    print("purpose:", r.json()["key"])
    for body in ("marketing policy v1", "marketing policy v2"):
        r = client.post("/api/v1/purposes/marketing/policy-versions",
                        json={"body": body}, headers=admin_h)
        r.raise_for_status()
        print("published policy version:", r.json()["version"])

    # 3. Grant bound to policy v1.
    banner("3. grant bound to policy v1 (idempotent)")
    subject = f"user-{uuid.uuid4().hex[:8]}"
    grant_id = f"evt-{uuid.uuid4()}"
    grant_payload = {
        "event_id": grant_id,
        "expected_version": 0,
        "subject_ref": subject,
        "purpose_key": "marketing",
        "policy_version": 1,
    }
    r = client.post("/api/v1/events/grant", json=grant_payload, headers=admin_h)
    r.raise_for_status()
    first = r.json()
    print("grant valid:", first["valid"], "basis:", first["basis"])

    r = client.post("/api/v1/events/grant", json=grant_payload, headers=admin_h)
    r.raise_for_status()
    print("retry replayed:", r.json()["replayed"],
          "same basis:", r.json()["basis"] == first["basis"])

    # Same event_id with different body -> conflict.
    bad = dict(grant_payload, policy_version=2)
    r = client.post("/api/v1/events/grant", json=bad, headers=admin_h)
    print("same-id different-body ->", r.status_code, r.json()["error"])

    # 4. Publish v2 does NOT renew: grant still based on v1, still valid.
    banner("4. policy update does not auto-renew")
    r = client.get(f"/api/v1/verify?subject_ref={subject}&purpose_key=marketing",
                   headers=admin_h)
    print("verify after policy v2 published:", r.json()["valid"],
          "bound policy_version:", r.json()["basis"]["policy_version"])

    # 5. Concurrent/stale writer loses OCC.
    banner("5. optimistic concurrency")
    stale = {"event_id": f"evt-{uuid.uuid4()}", "expected_version": 0,
             "subject_ref": subject, "purpose_key": "marketing", "policy_version": 2}
    r = client.post("/api/v1/events/grant", json=stale, headers=admin_h)
    print("state expected_version=0 ->", r.status_code, r.json()["error"],
          "-", r.json()["message"])

    # 6. Withdrawal; delayed duplicate of the withdrawal cannot resurrect it.
    banner("6. withdrawal and delayed retry protection")
    withdrawal_id = f"evt-{uuid.uuid4()}"
    w_payload = {"event_id": withdrawal_id, "expected_version": 1,
                 "subject_ref": subject, "purpose_key": "marketing"}
    r = client.post("/api/v1/events/withdrawal", json=w_payload, headers=admin_h)
    r.raise_for_status()
    print("withdrawal valid:", r.json()["valid"])
    # Delayed retry returns the original withdrawal result (not a grant).
    r = client.post("/api/v1/events/withdrawal", json=w_payload, headers=admin_h)
    print("late withdrawal retry replayed:", r.json()["replayed"],
          "valid still:", r.json()["valid"])

    # Only a NEW explicit grant (against current version 2) restores consent.
    new_grant = {"event_id": f"evt-{uuid.uuid4()}", "expected_version": 2,
                 "subject_ref": subject, "purpose_key": "marketing",
                 "policy_version": 2}
    r = client.post("/api/v1/events/grant", json=new_grant, headers=admin_h)
    r.raise_for_status()
    print("new explicit grant -> valid:", r.json()["valid"],
          "policy_version:", r.json()["basis"]["policy_version"])

    # 7. Expiry is immediate on verification (no cleanup job).
    banner("7. expiry boundary")
    exp_subject = f"user-{uuid.uuid4().hex[:8]}"
    expires_at = (datetime.now(timezone.utc) + timedelta(seconds=2)).isoformat()
    r = client.post("/api/v1/events/grant",
                    json={"event_id": f"evt-{uuid.uuid4()}", "expected_version": 0,
                          "subject_ref": exp_subject, "purpose_key": "marketing",
                          "policy_version": 2, "expires_at": expires_at},
                    headers=admin_h)
    r.raise_for_status()
    r = client.get(f"/api/v1/verify?subject_ref={exp_subject}&purpose_key=marketing",
                   headers=admin_h)
    print("before expiry valid:", r.json()["valid"], "reason:", r.json()["reason"])
    time.sleep(2.2)
    r = client.get(f"/api/v1/verify?subject_ref={exp_subject}&purpose_key=marketing",
                   headers=admin_h)
    print("after expiry valid:", r.json()["valid"], "reason:", r.json()["reason"])

    # 8. Rebuild from immutable history.
    banner("8. rebuild projection from history")
    r = client.post("/api/v1/rebuild", headers=admin_h)
    r.raise_for_status()
    print("rebuild:", r.json())
    r = client.get(f"/api/v1/verify?subject_ref={subject}&purpose_key=marketing",
                   headers=admin_h)
    print("verify after rebuild:", r.json()["valid"], "reason:", r.json()["reason"],
          "basis:", r.json()["basis"])

    # 9. Batch import: one bad item rolls back the entire batch.
    banner("9. batch import all-or-nothing (max 500)")
    good_owner = f"user-{uuid.uuid4().hex[:8]}"
    batch = [
        {"event_id": f"evt-{uuid.uuid4()}", "expected_version": 0,
         "subject_ref": good_owner, "action": "grant", "purpose_key": "marketing",
         "policy_version": 2},
        {"event_id": f"evt-{uuid.uuid4()}", "expected_version": 0,
         "subject_ref": f"user-{uuid.uuid4().hex[:8]}", "action": "grant",
         "purpose_key": "marketing", "policy_version": 2},
        {"event_id": f"evt-{uuid.uuid4()}", "expected_version": 99,
         "subject_ref": f"user-{uuid.uuid4().hex[:8]}", "action": "grant",
         "purpose_key": "marketing", "policy_version": 2},
    ]
    r = client.post("/api/v1/events/batch", json={"events": batch}, headers=admin_h)
    print("batch with one bad item ->", r.status_code, r.json()["error"],
          "-", r.json()["message"])
    # First item must have rolled back too -> verify shows no record.
    r = client.get(f"/api/v1/verify?subject_ref={good_owner}&purpose_key=marketing",
                   headers=admin_h)
    print("first batch item rolled back:", r.json()["status"])

    # 10. Erasure: identifiable data gone; history retained, anonymised.
    banner("10. subject erasure")
    victim = subject
    client.post(f"/api/v1/subjects/{victim}/export-copies",
                json={"copy_label": "warehouse_export"}, headers=admin_h).raise_for_status()
    r = client.delete(f"/api/v1/subjects/{victim}", headers=admin_h)
    r.raise_for_status()
    print("delete:", r.json())
    r = client.get(f"/api/v1/verify?subject_ref={victim}&purpose_key=marketing",
                   headers=admin_h)
    print("verify after delete:", r.json()["status"], "valid:", r.json()["valid"])
    r = client.get(f"/api/v1/history?subject_ref={victim}&purpose_key=marketing",
                   headers=admin_h)
    print("identifiable history after delete ->", r.status_code, r.json()["error"])

    # 11. Role separation: auditor cannot write.
    banner("11. roles and audit logs")
    r = client.post("/api/v1/purposes", json={"key": "nope"}, headers=auditor_h)
    print("auditor write ->", r.status_code, r.json()["error"])
    r = client.get("/api/v1/audit-logs?limit=5", headers=auditor_h)
    r.raise_for_status()
    print("auditor can read", len(r.json()), "audit entries; latest actions:",
          [e["action"] for e in r.json()[:5]])

    print("\nDEMO OK")
    return 0


if __name__ == "__main__":
    try:
        raise SystemExit(main())
    except httpx.HTTPError as exc:
        print(f"demo failed to reach API: {exc}", file=sys.stderr)
        raise SystemExit(1)
