"""Explicit restart recovery: active leases are persisted, so after a
simulated process restart they can be reclaimed by the boot-time sweep and the
address space stays consistent."""

import datetime as dt
import uuid

from sqlalchemy import func, select

from app.db import SessionLocal
from app.leasing import sweep_expired_sessions
from app.models import LeaseTermination, PoolAddress, Session as SessionModel, utcnow


def test_lease_persists_and_boot_sweep_reclaims(client, make_ap, make_device, api_connect):
    ap = make_ap(cidr="10.72.0.0/30", capacity=2)
    tenant = ap["tenant"]
    d1 = make_device(tenant, name="persist")
    l1 = api_connect(d1, ap).json()

    # "Crash": last heartbeat aged past the timeout, no sweeper ran in time.
    with SessionLocal() as db:
        db.execute(
            SessionModel.__table__.update()
            .where(SessionModel.id == uuid.UUID(l1["session"]["id"]))
            .values(last_heartbeat_at=utcnow() - dt.timedelta(seconds=10_000))
        )
        db.commit()

    # "Restart": exactly what the startup hook does.
    with SessionLocal() as db:
        released = sweep_expired_sessions(db, timeout_seconds=600)
    assert released == 1

    with SessionLocal() as db:
        s = db.get(SessionModel, uuid.UUID(l1["session"]["id"]))
        term = db.execute(
            select(LeaseTermination).where(LeaseTermination.session_id == s.id)
        ).scalar_one()
        allocated = db.execute(
            select(func.count()).select_from(PoolAddress).where(PoolAddress.status == "allocated")
        ).scalar_one()
    assert s.status == "closed"
    assert term.reason == "heartbeat_timeout"
    assert allocated == 0


def test_fresh_sessions_are_not_reaped_on_restart(client, make_ap, make_device, api_connect):
    ap = make_ap(cidr="10.73.0.0/30", capacity=2)
    device = make_device(ap["tenant"], name="fresh")
    lease = api_connect(device, ap).json()

    # Boot-time sweep with healthy heartbeats: nothing released.
    with SessionLocal() as db:
        assert sweep_expired_sessions(db, timeout_seconds=600) == 0
        s = db.get(SessionModel, uuid.UUID(lease["session"]["id"]))
    assert s.status == "active"
