import time

from conftest import (
    connect_ok,
    disconnect,
    heartbeat,
    make_ap,
    make_device,
    make_pool,
    make_tenant,
)
from app.db import SessionLocal
from app.models import Lease, LeaseStatus
from app.services import sweep_expired


def test_heartbeat_rejects_wrong_token(client):
    tenant = make_tenant(client)
    ap = make_ap(client, tenant, make_pool(client, tenant)["id"])
    dev = make_device(client, tenant)
    connect_ok(client, dev, ap.id)

    resp = client.post(
        "/api/v1/sessions/heartbeat",
        headers={"X-Device-Token": dev.token, "X-Session-Token": "garbage.token"},
    )
    assert resp.status_code == 401

    resp = client.post("/api/v1/sessions/heartbeat", headers={"X-Device-Token": dev.token})
    assert resp.status_code == 401


def test_heartbeat_extends_lease(client):
    tenant = make_tenant(client)
    ap = make_ap(client, tenant, make_pool(client, tenant)["id"])
    dev = make_device(client, tenant)
    sess = connect_ok(client, dev, ap.id)
    original_expiry = sess["lease"]["expires_at"]

    time.sleep(0.1)
    resp = heartbeat(client, dev, sess["session_token"])
    assert resp.status_code == 200
    assert resp.json()["expires_at"] > original_expiry
    assert resp.json()["generation"] == 1


def test_heartbeat_after_deadline_expires_and_fails(client):
    tenant = make_tenant(client)
    ap = make_ap(client, tenant, make_pool(client, tenant)["id"])
    dev = make_device(client, tenant)
    sess = connect_ok(client, dev, ap.id)

    # TTL is 3s in tests
    time.sleep(3.3)
    assert heartbeat(client, dev, sess["session_token"]).status_code == 409

    with SessionLocal() as db:
        lease = db.get(Lease, sess["lease"]["id"])
        assert lease.status == LeaseStatus.closed
        assert lease.close_reason.value == "expired"
        assert lease.closed_at is not None
        closed_events = [e for e in lease.events if e.event_type == "closed"]
        assert len(closed_events) == 1


def test_late_heartbeat_cannot_revive_or_extend_new_session(client):
    tenant = make_tenant(client)
    ap = make_ap(client, tenant, make_pool(client, tenant)["id"])
    dev = make_device(client, tenant)

    old = connect_ok(client, dev, ap.id, key="a")
    assert disconnect(client, dev, old["session_token"]).status_code == 200

    new = connect_ok(client, dev, ap.id, key="b")
    assert new["lease"]["generation"] == old["lease"]["generation"] + 1

    # old-generation token must never touch the new session
    resp = heartbeat(client, dev, old["session_token"])
    assert resp.status_code == 409

    with SessionLocal() as db:
        fresh = db.get(Lease, new["lease"]["id"])
        # new session untouched by late heartbeat
        assert fresh.status == LeaseStatus.active
        assert fresh.generation == new["lease"]["generation"]

    # and the new token works normally
    assert heartbeat(client, dev, new["session_token"]).status_code == 200


def test_disconnect_releases_and_validates_generation(client):
    tenant = make_tenant(client)
    ap = make_ap(client, tenant, make_pool(client, tenant)["id"])
    dev = make_device(client, tenant)
    sess = connect_ok(client, dev, ap.id)
    ip = sess["lease"]["ip_address"]

    assert disconnect(client, dev, sess["session_token"]).status_code == 200

    # second close with the same (now closed) generation token fails
    assert disconnect(client, dev, sess["session_token"]).status_code == 409

    with SessionLocal() as db:
        lease = db.get(Lease, sess["lease"]["id"])
        assert lease.status == LeaseStatus.closed
        assert lease.close_reason.value == "device_disconnect"

    # IP is reusable after release
    dev2 = make_device(client, tenant)
    again = connect_ok(client, dev2, ap.id)
    assert again["lease"]["ip_address"] == ip


def test_lease_released_exactly_once(client):
    """Expiry sweep racing an explicit close must close the lease once."""
    tenant = make_tenant(client)
    ap = make_ap(client, tenant, make_pool(client, tenant)["id"])
    dev = make_device(client, tenant)
    sess = connect_ok(client, dev, ap.id)

    time.sleep(3.3)  # lease now past deadline, still active in DB until swept

    # heartbeat notices expiry and closes it as 'expired'
    assert heartbeat(client, dev, sess["session_token"]).status_code == 409

    # a sweep immediately afterwards must not create a second close/event
    with SessionLocal() as db:
        closed = sweep_expired(db)
        assert closed == 0
        lease = db.get(Lease, sess["lease"]["id"])
        close_events = [e for e in lease.events if e.event_type == "closed"]
        assert len(close_events) == 1
        assert lease.close_reason.value == "expired"


def test_expired_lease_is_swept_and_ip_reusable(client):
    tenant = make_tenant(client)
    ap = make_ap(client, tenant, make_pool(client, tenant, cidr="10.8.8.0/30")["id"], capacity=2)
    dev = make_device(client, tenant)
    sess = connect_ok(client, dev, ap.id)
    ip = sess["lease"]["ip_address"]

    time.sleep(3.3)

    with SessionLocal() as db:
        closed = sweep_expired(db)
        assert closed == 1
        # repeat sweep is a no-op
        assert sweep_expired(db) == 0

    dev2 = make_device(client, tenant)
    new = connect_ok(client, dev2, ap.id)
    assert new["lease"]["ip_address"] == ip

    with SessionLocal() as db:
        old = db.get(Lease, sess["lease"]["id"])
        assert old.status == LeaseStatus.closed
        assert old.close_reason.value == "expired"
