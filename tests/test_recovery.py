import time

from fastapi.testclient import TestClient

from conftest import connect_ok, make_ap, make_device, make_pool, make_tenant, wait_for
from app.db import SessionLocal
from app.main import create_app
from app.models import Lease, LeaseStatus


def test_healthy_lease_survives_restart(client):
    tenant = make_tenant(client)
    ap = make_ap(client, tenant, make_pool(client, tenant)["id"])
    dev = make_device(client, tenant)
    sess = connect_ok(client, dev, ap.id)
    lease_id = sess["lease"]["id"]
    ip = sess["lease"]["ip_address"]

    # "restart": a fresh app process runs startup recovery against the same DB
    with TestClient(create_app()) as restarted:
        pass  # startup event runs recovery on enter; healthy lease must survive

    with SessionLocal() as db:
        lease = db.get(Lease, lease_id)
        assert lease.status == LeaseStatus.active
        assert lease.ip_address == ip
        assert lease.close_reason is None

    # the original session token is still valid for heartbeats after restart
    resp = client.post(
        "/api/v1/sessions/heartbeat",
        headers={"X-Device-Token": dev.token, "X-Session-Token": sess["session_token"]},
    )
    assert resp.status_code == 200


def test_startup_recovers_stale_leases(client):
    tenant = make_tenant(client)
    ap = make_ap(client, tenant, make_pool(client, tenant, cidr="10.6.6.0/30")["id"], capacity=2)
    dev = make_device(client, tenant)
    sess = connect_ok(client, dev, ap.id)
    lease_id = sess["lease"]["id"]
    ip = sess["lease"]["ip_address"]

    time.sleep(3.3)  # TTL (3s in tests) passes while the service is "down"

    with TestClient(create_app()) as restarted:
        # startup recovery runs synchronously in the startup event
        pass

    with SessionLocal() as db:
        lease = db.get(Lease, lease_id)
        assert lease.status == LeaseStatus.closed
        assert lease.close_reason.value == "expired"
        assert lease.closed_at is not None
        close_events = [e for e in lease.events if e.event_type == "closed"]
        assert len(close_events) == 1

    # recovered address is allocatable again
    dev2 = make_device(client, tenant)
    new = connect_ok(client, dev2, ap.id)
    assert new["lease"]["ip_address"] == ip


def test_background_sweeper_reaps_expired_leases(monkeypatch):
    from app.config import get_settings

    settings = get_settings()
    monkeypatch.setattr(settings, "run_sweeper", True)
    monkeypatch.setattr(settings, "sweeper_interval_seconds", 0.15)

    with TestClient(create_app()) as client:
        tenant = make_tenant(client)
        ap = make_ap(client, tenant, make_pool(client, tenant)["id"])
        dev = make_device(client, tenant)
        sess = connect_ok(client, dev, ap.id)

        def lease_closed():
            with SessionLocal() as db:
                lease = db.get(Lease, sess["lease"]["id"])
                return lease if lease.status == LeaseStatus.closed else None

        closed = wait_for(lease_closed, timeout=10)
        assert closed.close_reason.value == "expired"
