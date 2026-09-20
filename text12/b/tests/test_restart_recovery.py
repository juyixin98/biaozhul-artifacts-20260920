"""Lease expiry and restart recovery.

Lease state is fully persisted: an expired lease is released by the next
sweeper run (which is exactly what happens on startup after a crash), stale
leases never come back to life, and released addresses are reusable.
"""

import datetime as dt
import time
import uuid

from sqlalchemy import func, select

from app.db import SessionLocal
from app.leasing import sweep_expired_sessions
from app.models import LeaseTermination, PoolAddress, Session as SessionModel, utcnow


def test_expired_lease_released_by_sweep(client, make_ap, make_device, api_connect):
    ap = make_ap(cidr="10.70.0.0/30", capacity=2)  # .1,.2 usable
    tenant = ap["tenant"]
    d1 = make_device(tenant, name="d1")
    d2 = make_device(tenant, name="d2")
    l1 = api_connect(d1, ap).json()
    l2 = api_connect(d2, ap).json()

    # Simulate 10 minutes without a heartbeat.
    with SessionLocal() as db:
        old = utcnow() - dt.timedelta(seconds=601)
        db.execute(
            SessionModel.__table__.update()
            .where(SessionModel.id == uuid.UUID(l1["session"]["id"]))
            .values(last_heartbeat_at=old)
        )
        db.commit()

    with SessionLocal() as db:
        released = sweep_expired_sessions(db, timeout_seconds=600)
    assert released == 1

    with SessionLocal() as db:
        s1 = db.get(SessionModel, uuid.UUID(l1["session"]["id"]))
        s2 = db.get(SessionModel, uuid.UUID(l2["session"]["id"]))
        t1 = db.execute(
            select(LeaseTermination).where(LeaseTermination.session_id == s1.id)
        ).scalar_one()
        allocated = db.execute(
            select(func.count()).select_from(PoolAddress).where(PoolAddress.status == "allocated")
        ).scalar_one()
    assert s1.status == "closed"
    assert s1.closed_at is not None
    assert t1.reason == "heartbeat_timeout"
    assert t1.terminated_at is not None
    assert s2.status == "active"  # fresh lease untouched
    assert allocated == 1

    # Address reusable
    d3 = make_device(tenant, name="d3")
    r = api_connect(d3, ap)
    assert r.status_code == 201
    assert r.json()["session"]["ip"] == l1["session"]["ip"]


def test_sweep_is_idempotent_on_repeat(client, make_ap, make_device, api_connect):
    ap = make_ap(cidr="10.71.0.0/30", capacity=2)
    device = make_device(ap["tenant"], name="d")
    lease = api_connect(device, ap).json()

    with SessionLocal() as db:
        db.execute(
            SessionModel.__table__.update()
            .where(SessionModel.id == uuid.UUID(lease["session"]["id"]))
            .values(last_heartbeat_at=utcnow() - dt.timedelta(hours=1))
        )
        db.commit()

    for _ in range(3):
        with SessionLocal() as db:
            assert sweep_expired_sessions(db, timeout_seconds=600) in (1, 0)

    with SessionLocal() as db:
        terms = db.execute(
            select(func.count()).select_from(LeaseTermination).where(
                LeaseTermination.session_id == uuid.UUID(lease["session"]["id"])
            )
        ).scalar_one()
        allocated = db.execute(
            select(func.count()).select_from(PoolAddress).where(PoolAddress.status == "allocated")
        ).scalar_one()
    assert terms == 1
    assert allocated == 0


def test_background_sweeper_releases_after_configured_timeout():
    """Integration test using a fresh app with a 1-second lifecycle. Runs only
    where RAPID_SWEEP env is set (docker tests use 2s timeout / 1s sweep)."""
    import os

    if os.environ.get("HEARTBEAT_TIMEOUT_SECONDS") not in ("1", "2"):
        import pytest

        pytest.skip("background sweep test requires a short HEARTBEAT_TIMEOUT_SECONDS")

    from fastapi.testclient import TestClient

    from app.main import create_app

    app = create_app(auto_migrate=False, auto_seed=False)
    with TestClient(app) as local_client:
        # minimal setup directly through service layer
        from app import service
        from app.db import SessionLocal

        with SessionLocal() as db:
            from app.models import Tenant
            from app.security import hash_password
            from app.models import AdminUser

            tenant = Tenant(name=f"sweep-{uuid.uuid4().hex[:6]}")
            db.add(tenant); db.flush()
            db.add(AdminUser(username=f"a-{uuid.uuid4().hex[:8]}", password_hash=hash_password("x" * 10),
                             tenant_id=tenant.id, is_platform_admin=False))
            pool = service.create_pool(db, tenant_id=tenant.id, name="p", cidr="10.80.0.0/30",
                                       reserved_addresses=[])
            ap = service.create_access_point(db, tenant_id=tenant.id, name="a", pool_id=pool.id, capacity=2)
            device, token = service.enroll_device(db, tenant_id=tenant.id, name="d")
            ids = (tenant.id, ap.id, device.id)

        r = local_client.post(
            "/api/device/connect",
            headers={"X-Device-Token": token},
            json={"access_point_id": str(ids[1]), "idempotency_key": "k"},
        )
        assert r.status_code == 201
        sid = uuid.UUID(r.json()["session"]["id"])

        # Wait long enough for the background sweeper (timeout+2 sweeps, generous ceiling).
        deadline = time.time() + 12
        while time.time() < deadline:
            with SessionLocal() as db:
                status = db.get(SessionModel, sid).status
            if status == "closed":
                break
            time.sleep(0.25)
        assert status == "closed"
