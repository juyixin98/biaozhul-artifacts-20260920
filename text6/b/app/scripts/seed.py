"""Idempotent demo bootstrap: two organizations with one admin + one auditor
key each, plus a published policy.

Only suitable for demos -- key material comes from environment defaults and
is printed on startup. Do not use in production.
"""
from __future__ import annotations

from sqlalchemy.orm import Session

from app.config import get_settings
from app.database import SessionLocal
from app.models import ApiKey, Organization, PolicyVersion, Role
from app.security import hash_key


def _ensure_org(db: Session, name: str) -> Organization:
    org = db.query(Organization).filter(Organization.name == name).one_or_none()
    if org is None:
        org = Organization(name=name)
        db.add(org)
        db.flush()
    return org


def _ensure_key(db: Session, org: Organization, raw: str, label: str, role: Role) -> None:
    exists = db.query(ApiKey).filter(ApiKey.key_hash == hash_key(raw)).one_or_none()
    if exists is None:
        db.add(
            ApiKey(
                organization_id=org.id,
                key_hash=hash_key(raw),
                label=label,
                role=role,
                active=True,
            )
        )


def _ensure_policy(db: Session, org: Organization, version: str, body: str) -> None:
    exists = (
        db.query(PolicyVersion)
        .filter(PolicyVersion.organization_id == org.id, PolicyVersion.version == version)
        .one_or_none()
    )
    if exists is None:
        db.add(PolicyVersion(organization_id=org.id, version=version, body=body))


def seed(db: Session) -> dict:
    settings = get_settings()

    org_a = _ensure_org(db, "Demo Org A")
    org_b = _ensure_org(db, "Demo Org B")
    db.flush()

    _ensure_key(db, org_a, settings.seed_admin_key, "demo-admin-a", Role.admin)
    _ensure_key(db, org_a, settings.seed_auditor_key, "demo-auditor-a", Role.auditor)
    _ensure_key(db, org_b, "demo-admin-key-b", "demo-admin-b", Role.admin)
    _ensure_key(db, org_b, "demo-auditor-key-b", "demo-auditor-b", Role.auditor)

    policy_body = (
        "Demo privacy policy. Consent is recorded per purpose, can be withdrawn "
        "at any time, and expires automatically at the granted expiry instant."
    )
    _ensure_policy(db, org_a, "v2026-01", policy_body)
    _ensure_policy(db, org_b, "v2026-01", policy_body)

    db.commit()
    return {
        "org_a_admin": settings.seed_admin_key,
        "org_a_auditor": settings.seed_auditor_key,
        "org_b_admin": "demo-admin-key-b",
        "org_b_auditor": "demo-auditor-key-b",
    }


def main() -> None:
    db = SessionLocal()
    try:
        keys = seed(db)
        print("Seeded demo credentials:")
        for name, value in keys.items():
            print(f"  {name}: {value}")
    finally:
        db.close()


if __name__ == "__main__":
    main()
