"""Idempotent first-boot bootstrap: organizations, demo API keys and demo data.

Run automatically by the container entrypoint after migrations. Safe to run
multiple times — it never duplicates organizations or keys. Prints demo
credentials to stdout when it creates them (in Docker these appear in logs).

DEMO ONLY: the fixed keys from settings are convenient for local exploration
and must never be used in a real deployment.
"""
from __future__ import annotations

from datetime import timedelta

from sqlalchemy import select

from app.config import get_settings
from app.db import SessionLocal
from app.models import ApiKey, Organization
from app.schemas import GrantIn
from app.security import generate_key, hash_key
from app import service

DEMO_ORGS = [
    ("demo-org", "demo_admin_key", "demo_auditor_key", "admin", "auditor"),
    ("demo-org-2", "demo_org2_admin_key", "demo_org2_auditor_key", "org2-admin", "org2-auditor"),
]


def _ensure_api_key(db, org_id: int, label: str, role: str, fixed: str | None) -> str:
    token = fixed or generate_key()
    existing = db.scalars(select(ApiKey).where(ApiKey.key_hash == hash_key(token))).one_or_none()
    if existing is not None:
        return token
    db.add(ApiKey(organization_id=org_id, key_hash=hash_key(token), label=label, role=role))
    return token


def bootstrap(seed_demo_data: bool = True) -> dict[str, str]:
    settings = get_settings()
    keys: dict[str, str] = {}
    db = SessionLocal()
    try:
        for idx, (org_name, admin_attr, auditor_attr, admin_label, auditor_label) in enumerate(
            DEMO_ORGS, start=1
        ):
            org = db.scalars(select(Organization).where(Organization.name == org_name)).one_or_none()
            created = org is None
            if org is None:
                org = Organization(name=org_name)
                db.add(org)
                db.flush()

            prefix = "" if idx == 1 else "org2_"
            admin_key = _ensure_api_key(
                db, org.id, admin_label, "admin", getattr(settings, f"demo_{prefix}admin_key", None)
            )
            auditor_key = _ensure_api_key(
                db,
                org.id,
                auditor_label,
                "auditor",
                getattr(settings, f"demo_{prefix}auditor_key", None),
            )
            keys[f"{org_name}_admin"] = admin_key
            keys[f"{org_name}_auditor"] = auditor_key

            if created and seed_demo_data:
                _seed_demo_consents(db, org.id)

        db.commit()
    finally:
        db.close()
    return keys


def _seed_demo_consents(db, org_id: int) -> None:
    """A small, deterministic scenario for demos and manual exploration."""
    from app.models import utcnow

    pv1 = service.publish_policy(db, org_id, "Demo policy v1: analytics + marketing terms.")
    # Grant for analytics under v1, valid for 30 days.
    service.apply_event(
        db,
        org_id,
        GrantIn(
            event_id="seed-grant-analytics",
            subject_key="user-1001",
            purpose="analytics",
            action="grant",
            expected_version=0,
            expires_at=utcnow() + timedelta(days=30),
            policy_version=pv1.version,
        ),
    )
    # Grant then withdraw marketing, so the state reads "withdrawn".
    service.apply_event(
        db,
        org_id,
        GrantIn(
            event_id="seed-grant-marketing",
            subject_key="user-1001",
            purpose="marketing",
            action="grant",
            expected_version=0,
            policy_version=pv1.version,
        ),
    )
    service.apply_event(
        db,
        org_id,
        GrantIn(
            event_id="seed-withdraw-marketing",
            subject_key="user-1001",
            purpose="marketing",
            action="withdraw",
            expected_version=1,
        ),
    )


if __name__ == "__main__":
    created_keys = bootstrap()
    print("\n=== ConsentVault demo credentials ===")
    for name, key in created_keys.items():
        print(f"{name}: {key}")
    print("=====================================\n")
