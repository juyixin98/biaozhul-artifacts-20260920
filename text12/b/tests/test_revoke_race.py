"""Revocation races: revoke vs heartbeat vs expiry vs reconnect must leave
exactly one released lease and revoke all future use of the old credential."""

import threading
import uuid

from sqlalchemy import func, select

from app.db import SessionLocal
from app.leasing import sweep_expired_sessions
from app.models import Device, LeaseTermination, PoolAddress, Session as SessionModel


def _revoke(client, tenant, device_id, token):
    return client.post(
        f"{tenant['base']}/devices/{device_id}/revoke",
        headers={"Authorization": f"Bearer {token}"},
    )


def test_revoke_terminates_lease_and_blocks_old_token(client, make_ap, make_device, api_connect):
    ap = make_ap(cidr="10.60.0.0/24", capacity=10)
    tenant = ap["tenant"]
    device = make_device(tenant, name="rev")
    lease = api_connect(device, ap).json()

    resp = _revoke(client, tenant, device["id"], tenant["token"])
    assert resp.status_code == 200 and resp.json()["revoked"] is True

    # Active lease terminated with reason revoked, exactly once.
    with SessionLocal() as db:
        term = db.execute(
            select(LeaseTermination).where(LeaseTermination.session_id == uuid.UUID(lease["session"]["id"]))
        ).scalar_one()
        assert term.reason == "revoked"
        allocated = db.execute(
            select(func.count()).select_from(PoolAddress).where(PoolAddress.status == "allocated")
        ).scalar_one()
        assert allocated == 0
        device_row = db.get(Device, uuid.UUID(device["id"]))
        assert device_row.revoked is True

    # Old device token rejected on connect / heartbeat.
    resp = api_connect(device, ap)
    assert resp.status_code in (401, 403)
    resp = client.post(
        "/api/device/heartbeat",
        headers={"X-Device-Token": device["token"], "X-Lease-Token": lease["lease_token"]},
        json={"session_id": lease["session"]["id"], "generation": lease["session"]["generation"]},
    )
    assert resp.status_code in (401, 403)


def test_revoke_is_idempotent(client, make_ap, make_device, api_connect):
    ap = make_ap(cidr="10.61.0.0/24", capacity=10)
    tenant = ap["tenant"]
    device = make_device(tenant, name="rev2")
    api_connect(device, ap)

    r1 = _revoke(client, tenant, device["id"], tenant["token"])
    r2 = _revoke(client, tenant, device["id"], tenant["token"])
    assert r1.status_code == 200 and r2.status_code == 200

    with SessionLocal() as db:
        terms = db.execute(
            select(func.count())
            .select_from(LeaseTermination)
            .join(SessionModel, LeaseTermination.session_id == SessionModel.id)
            .where(SessionModel.device_id == uuid.UUID(device["id"]))
        ).scalar_one()
    assert terms == 1


def test_revoke_race_with_heartbeats(client, make_ap, make_device, api_connect):
    ap = make_ap(cidr="10.62.0.0/24", capacity=10)
    tenant = ap["tenant"]
    device = make_device(tenant, name="race")
    lease = api_connect(device, ap).json()

    errors: list = []

    def hammer_heartbeats():
        for _ in range(10):
            r = client.post(
                "/api/device/heartbeat",
                headers={"X-Device-Token": device["token"], "X-Lease-Token": lease["lease_token"]},
                json={"session_id": lease["session"]["id"],
                      "generation": lease["session"]["generation"]},
            )
            if r.status_code not in (200, 401, 403, 409):
                errors.append(r.status_code)

    t = threading.Thread(target=hammer_heartbeats)
    t.start()
    revoke_resp = _revoke(client, tenant, device["id"], tenant["token"])
    t.join()

    assert revoke_resp.status_code == 200
    assert errors == []

    with SessionLocal() as db:
        active = db.execute(
            select(func.count()).select_from(SessionModel).where(
                SessionModel.device_id == uuid.UUID(device["id"]),
                SessionModel.status == "active",
            )
        ).scalar_one()
        allocated = db.execute(
            select(func.count()).select_from(PoolAddress).where(PoolAddress.status == "allocated")
        ).scalar_one()
        terms = db.execute(
            select(func.count()).select_from(LeaseTermination).where(
                LeaseTermination.session_id == uuid.UUID(lease["session"]["id"])
            )
        ).scalar_one()
    assert active == 0
    assert allocated == 0
    assert terms == 1


def test_revoke_race_with_expiry_sweep(client, make_ap, make_device, api_connect):
    """Revoke and timeout fire concurrently: exactly one termination, address
    freed once, device still revoked."""
    ap = make_ap(cidr="10.63.0.0/24", capacity=10)
    tenant = ap["tenant"]
    device = make_device(tenant, name="exp-rev")
    lease = api_connect(device, ap).json()
    sid = uuid.UUID(lease["session"]["id"])

    barrier = threading.Barrier(2)

    def revoke():
        barrier.wait()
        _revoke(client, tenant, device["id"], tenant["token"])

    def expire():
        barrier.wait()
        # Run sweeper with a timeout of 0 to force the lease to look stale.
        with SessionLocal() as db:
            sweep_expired_sessions(db, timeout_seconds=0)

    t1 = threading.Thread(target=revoke)
    t2 = threading.Thread(target=expire)
    t1.start(); t2.start()
    t1.join(); t2.join()

    with SessionLocal() as db:
        terms = db.execute(
            select(LeaseTermination).where(LeaseTermination.session_id == sid)
        ).scalars().all()
        allocated = db.execute(
            select(func.count()).select_from(PoolAddress).where(PoolAddress.status == "allocated")
        ).scalar_one()
        revoked = db.get(Device, uuid.UUID(device["id"])).revoked
    assert len(terms) == 1
    assert terms[0].reason in ("revoked", "heartbeat_timeout")
    assert allocated == 0
    assert revoked is True


def test_reinstate_issues_working_token(client, make_ap, make_device, api_connect):
    ap = make_ap(cidr="10.64.0.0/24", capacity=10)
    tenant = ap["tenant"]
    device = make_device(tenant, name="back")
    api_connect(device, ap)
    _revoke(client, tenant, device["id"], tenant["token"])

    resp = client.post(
        f"{tenant['base']}/devices/{device['id']}/reinstate",
        headers={"Authorization": f"Bearer {tenant['token']}"},
    )
    assert resp.status_code == 200
    new_token = resp.json()["device_token"]

    device["token"] = new_token
    r = api_connect(device, ap)
    assert r.status_code == 201
