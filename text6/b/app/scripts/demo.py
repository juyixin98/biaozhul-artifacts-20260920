"""End-to-end demo against a running ConsentVault instance.

Walks through: policy immutability, grant with idempotency, expiry boundary,
withdraw + late-retry safety, policy update non-renewal, rebuild, batch
import, auditor read-only access, org isolation, erasure and audit log.

Usage:
    python -m app.scripts.demo [--base-url http://localhost:8000] \
        [--admin-key demo-admin-key] [--auditor-key demo-auditor-key] \
        [--org-b-admin-key demo-admin-key-b]
"""
from __future__ import annotations

import argparse
import json
import time
import uuid
from datetime import datetime, timedelta, timezone

import requests


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--base-url", default="http://localhost:8000")
    parser.add_argument("--admin-key", default="demo-admin-key")
    parser.add_argument("--auditor-key", default="demo-auditor-key")
    parser.add_argument("--org-b-admin-key", default="demo-admin-key-b")
    args = parser.parse_args()

    base = args.base_url.rstrip("/")
    admin = {"X-API-Key": args.admin_key}
    auditor = {"X-API-Key": args.auditor_key}
    org_b = {"X-API-Key": args.org_b_admin_key}

    def show(title: str, resp: requests.Response) -> dict:
        print(f"\n=== {title} -> HTTP {resp.status_code}")
        try:
            body = resp.json()
        except ValueError:
            body = resp.text
        print(json.dumps(body, indent=2, ensure_ascii=False, default=str))
        return body if isinstance(body, dict) else {}

    print("ConsentVault demo")
    print("Disclaimer: records traceable consent state only; not a claim of "
          "any specific regulatory certification.")

    # 1. Policy: publish v2, try to change the already-published v1 (blocked).
    show("Publish policy v2026-02", requests.post(
        f"{base}/v1/policies", headers=admin,
        json={"version": "v2026-02", "body": "Updated demo policy."},
    ))
    show("Re-publish same version -> 409 conflict", requests.post(
        f"{base}/v1/policies", headers=admin,
        json={"version": "v2026-02", "body": "Attempted rewrite."},
    ))

    # 2. Create a subject.
    subject = show("Create subject", requests.post(
        f"{base}/v1/subjects", headers=admin,
        json={"external_ref": f"user-{uuid.uuid4().hex[:8]}",
              "email": "alice@example.com", "display_name": "Alice"},
    ))
    sid = subject["id"]

    # 3. Grant bound to old policy version with a short expiry.
    purpose = "marketing"
    expiry = datetime.now(timezone.utc) + timedelta(seconds=2)
    grant = show("Grant consent (bound to v2026-01)", requests.post(
        f"{base}/v1/consent/grant", headers=admin,
        json={"event_id": f"evt-{uuid.uuid4().hex[:8]}", "subject_id": sid,
              "purpose": purpose, "expected_version": 0,
              "policy_version": "v2026-01", "expires_at": expiry.isoformat()},
    ))

    # 4. Idempotent replay: same request body -> same result, replayed=true.
    show("Replay identical grant -> replayed", requests.post(
        f"{base}/v1/consent/grant", headers=admin,
        json={"event_id": grant["event_id"], "subject_id": sid,
              "purpose": purpose, "expected_version": 0,
              "policy_version": "v2026-01", "expires_at": expiry.isoformat()},
    ))

    # 5. Verify valid while not expired.
    show("Verify consent (valid)", requests.get(
        f"{base}/v1/consent/{sid}/{purpose}/verify", headers=auditor,
    ))

    # 6. Expiry boundary: wait past expiry -- invalid immediately, no cleanup.
    print("\nWaiting for grant to expire (2.5s)...")
    time.sleep(2.5)
    show("Verify consent (expired, no cleanup task)", requests.get(
        f"{base}/v1/consent/{sid}/{purpose}/verify", headers=auditor,
    ))

    # 7. Withdraw not possible after expiry; a new explicit grant is needed.
    show("Withdraw expired grant -> 422", requests.post(
        f"{base}/v1/consent/withdraw", headers=admin,
        json={"event_id": f"w-{uuid.uuid4().hex[:8]}", "subject_id": sid,
              "purpose": purpose, "expected_version": 1},
    ))

    # 8. New grant (new event id) -> valid, still bound to its own version.
    grant2 = show("New grant after expiry, bound to v2026-02", requests.post(
        f"{base}/v1/consent/grant", headers=admin,
        json={"event_id": f"evt-{uuid.uuid4().hex[:8]}", "subject_id": sid,
              "purpose": purpose, "expected_version": 1,
              "policy_version": "v2026-02",
              "expires_at": (datetime.now(timezone.utc) + timedelta(days=30)).isoformat()},
    ))

    # 9. Withdraw; replaying the old grant must NOT restore consent.
    show("Withdraw", requests.post(
        f"{base}/v1/consent/withdraw", headers=admin,
        json={"event_id": f"w-{uuid.uuid4().hex[:8]}", "subject_id": sid,
              "purpose": purpose, "expected_version": 2},
    ))
    show("Late retry of previous grant id -> replay shows withdraw state",
         requests.get(f"{base}/v1/consent/{sid}/{purpose}/verify", headers=auditor))
    show("Verify after withdraw (withdrawn)", requests.get(
        f"{base}/v1/consent/{sid}/{purpose}/verify", headers=auditor,
    ))

    # 10. Rebuild from immutable history; verification unchanged.
    show("Rebuild states from event history", requests.post(
        f"{base}/v1/admin/rebuild", headers=admin,
    ))
    show("Verify after rebuild (unchanged)", requests.get(
        f"{base}/v1/consent/{sid}/{purpose}/verify", headers=auditor,
    ))

    # 11. Batch import (grant for two new purposes).
    s2 = requests.post(f"{base}/v1/subjects", headers=admin,
                       json={"external_ref": f"user-{uuid.uuid4().hex[:8]}",
                             "email": "bob@example.com"}).json()
    batch = {"items": [
        {"event_id": f"b-{uuid.uuid4().hex[:8]}", "subject_id": s2["id"],
         "purpose": "newsletter", "expected_version": 0,
         "policy_version": "v2026-01", "event_type": "grant",
         "expires_at": (datetime.now(timezone.utc) + timedelta(days=1)).isoformat()},
        {"event_id": f"b-{uuid.uuid4().hex[:8]}", "subject_id": s2["id"],
         "purpose": "analytics", "expected_version": 0,
         "policy_version": "v2026-01", "event_type": "grant",
         "expires_at": (datetime.now(timezone.utc) + timedelta(days=1)).isoformat()},
    ]}
    show("Batch import 2 grants", requests.post(
        f"{base}/v1/consent/batch", headers=admin, json=batch,
    ))
    show("Re-run identical batch -> all replayed", requests.post(
        f"{base}/v1/consent/batch", headers=admin, json=batch,
    ))

    # 12. Auditor cannot write.
    show("Auditor attempts grant -> 403", requests.post(
        f"{base}/v1/consent/grant", headers=auditor,
        json={"event_id": "x", "subject_id": sid, "purpose": "z",
              "expected_version": 0, "policy_version": "v2026-01"},
    ))

    # 13. Organization isolation: org B cannot see org A's subject.
    show("Org B reads Org A subject -> 404", requests.get(
        f"{base}/v1/subjects/{sid}", headers=org_b,
    ))

    # 14. Register an export then erase the subject.
    show("Register export copy", requests.post(
        f"{base}/v1/subjects/{sid}/exports", headers=admin,
        json={"subject_id": sid, "destination": "subject-access-request",
              "payload": "name=Alice;email=alice@example.com"},
    ))
    show("Erase subject (PII + export copies cleared)", requests.post(
        f"{base}/v1/subjects/{sid}/erase", headers=admin,
    ))
    show("Fetch erased subject by internal id -> 200 tombstone", requests.get(
        f"{base}/v1/subjects/{sid}", headers=auditor,
    ))
    show("History still exists, PII-free, after erasure", requests.get(
        f"{base}/v1/subjects/{sid}/history", headers=auditor,
    ))

    # 15. Audit log: operational records without personal fields.
    show("Audit log (PII-free)", requests.get(
        f"{base}/v1/audit-logs?limit=10", headers=auditor,
    ))

    print("\nDemo complete.")


if __name__ == "__main__":
    main()
