import threading
from concurrent.futures import ThreadPoolExecutor

from sqlalchemy import func, select

from conftest import connect, connect_ok, heartbeat, make_ap, make_device, make_pool, make_tenant, revoke
from app.db import SessionLocal
from app.models import Lease, LeaseStatus


def test_revoke_terminates_session_and_releases_ip(client):
    tenant = make_tenant(client)
    ap = make_ap(client, tenant, make_pool(client, tenant, cidr="10.4.4.0/30")["id"], capacity=5)
    dev = make_device(client, tenant)
    sess = connect_ok(client, dev, ap.id)
    ip = sess["lease"]["ip_address"]

    # heartbeat works pre-revoke
    assert heartbeat(client, dev, sess["session_token"]).status_code == 200

    resp = revoke(client, tenant, dev.id)
    assert resp.status_code == 200
    assert resp.json()["revoked"] is True

    with SessionLocal() as db:
        lease = db.get(Lease, sess["lease"]["id"])
        assert lease.status == LeaseStatus.closed
        assert lease.close_reason.value == "revoked"
        assert lease.closed_at is not None

    # session token no longer works (device itself is refused auth)
    assert heartbeat(client, dev, sess["session_token"]).status_code == 403

    # released IP is reusable by another device
    other = make_device(client, tenant)
    new = connect_ok(client, other, ap.id)
    assert new["lease"]["ip_address"] == ip


def test_revoked_device_token_cannot_reconnect(client):
    tenant = make_tenant(client)
    ap = make_ap(client, tenant, make_pool(client, tenant)["id"])
    dev = make_device(client, tenant)
    connect_ok(client, dev, ap.id)
    assert revoke(client, tenant, dev.id).status_code == 200

    resp = connect(client, dev, ap.id)
    assert resp.status_code == 403


def test_revoke_is_idempotent(client):
    tenant = make_tenant(client)
    dev = make_device(client, tenant)
    assert revoke(client, tenant, dev.id).status_code == 200
    assert revoke(client, tenant, dev.id).status_code == 200

    with SessionLocal() as db:
        # no lease ever existed -> nothing to release, no error
        count = db.scalar(select(func.count(Lease.id)).where(Lease.device_id == dev.id))
        assert count == 0


def test_revoke_racing_reconnect_keeps_single_lease(client):
    tenant = make_tenant(client)
    ap = make_ap(client, tenant, make_pool(client, tenant)["id"], capacity=50)
    dev = make_device(client, tenant)
    connect_ok(client, dev, ap.id)
    # free the active lease so reconnect has real work to race against
    # (revoke below is the thing racing fresh connect attempts)

    start = threading.Event()

    def try_revoke():
        start.wait()
        return revoke(client, tenant, dev.id)

    def try_connect():
        start.wait()
        return connect(client, dev, ap.id)

    outcomes = []
    with ThreadPoolExecutor(max_workers=12) as pool:
        futures = [pool.submit(try_revoke)]
        # many reconnect attempts, some landing just before/after the revoke
        futures += [pool.submit(try_connect) for _ in range(10)]
        start.set()
        outcomes = [f.result() for f in futures]

    # the revoke happened
    assert any(r.status_code == 200 and r.json()["revoked"] for r in outcomes)

    with SessionLocal() as db:
        active = db.scalars(
            select(Lease).where(Lease.device_id == dev.id, Lease.status == LeaseStatus.active)
        ).all()
        assert active == []  # no surviving active lease for revoked device

    # final state is durable: still cannot connect
    assert connect(client, dev, ap.id).status_code == 403
