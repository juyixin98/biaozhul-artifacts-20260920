"""Lease expiry, late heartbeats, and restart recovery."""
from __future__ import annotations

from datetime import datetime, timedelta, timezone

from sqlalchemy import update
from starlette.testclient import TestClient

from app.models import Lease
from app.services.leases import run_sweep

from .helpers import admin_headers, connect, create_device, device_headers, make_tenant_env


def _force_expiry(db, lease_id: str, seconds_ago: int = 1):
    past = datetime.now(timezone.utc) - timedelta(seconds=seconds_ago)
    db.execute(update(Lease).where(Lease.id == lease_id).values(expires_at=past))
    db.commit()


def test_heartbeat_extends_expiry(client, db):
    h = admin_headers(client)
    tid, ap_id = make_tenant_env(client, h)
    _, token = create_device(client, h, tid)
    lease = connect(client, token, ap_id, key="k1").json()

    # Shrink the remaining TTL, then heartbeat: expiry must move back out.
    soon = datetime.now(timezone.utc) + timedelta(seconds=5)
    db.execute(update(Lease).where(Lease.id == lease["lease_id"]).values(expires_at=soon))
    db.commit()

    resp = client.post(
        "/device/heartbeat",
        json={"lease_id": lease["lease_id"], "generation": lease["generation"]},
        headers=device_headers(token),
    )
    assert resp.status_code == 200
    new_expiry = datetime.fromisoformat(resp.json()["expires_at"])
    assert new_expiry > datetime.now(timezone.utc) + timedelta(seconds=500)


def test_expired_lease_released_on_touch_and_sweep(client, db, app):
    h = admin_headers(client)
    tid, ap_id = make_tenant_env(client, h, cidr="10.8.1.0/30")  # 2 usable
    _, tok1 = create_device(client, h, tid, "d1")
    _, tok2 = create_device(client, h, tid, "d2")
    l1 = connect(client, tok1, ap_id, key="k1").json()
    l2 = connect(client, tok2, ap_id, key="k2").json()

    # d1 stops heartbeating; force its TTL to elapse.
    _force_expiry(db, l1["lease_id"])

    # Lazy path: a (late) heartbeat on the expired lease releases it, returns 410.
    resp = client.post(
        "/device/heartbeat",
        json={"lease_id": l1["lease_id"], "generation": l1["generation"]},
        headers=device_headers(tok1),
    )
    assert resp.status_code == 410

    # d2 is unaffected; d1's address is free again for a new device.
    _, tok3 = create_device(client, h, tid, "d3")
    l3 = connect(client, tok3, ap_id, key="k3")
    assert l3.status_code == 200
    assert l3.json()["ip"] == l1["ip"]

    # Sweeper path: expire d2 and let the sweeper collect it.
    _force_expiry(db, l2["lease_id"])
    released = run_sweep(app.state.SessionLocal)
    assert released == 1

    terms = client.get(f"/admin/tenants/{tid}/terminations", headers=h).json()
    reasons = {t["lease_id"]: t["release_reason"] for t in terms}
    assert reasons[l1["lease_id"]] == "expired"
    assert reasons[l2["lease_id"]] == "expired"


def test_late_heartbeat_cannot_revive_or_extend(client, db, app):
    h = admin_headers(client)
    tid, ap_id = make_tenant_env(client, h)
    _, token = create_device(client, h, tid)

    old = connect(client, token, ap_id, key="k1").json()
    _force_expiry(db, old["lease_id"])
    run_sweep(app.state.SessionLocal)

    # Device reconnects: new lease, new generation.
    new = connect(client, token, ap_id, key="k2").json()
    assert new["generation"] == old["generation"] + 1
    new_expiry_before = new["expires_at"]

    # Late heartbeat of the OLD session: must not revive it...
    resp = client.post(
        "/device/heartbeat",
        json={"lease_id": old["lease_id"], "generation": old["generation"]},
        headers=device_headers(token),
    )
    assert resp.status_code == 410

    # ...and the old generation aimed at the NEW lease must not extend it.
    resp = client.post(
        "/device/heartbeat",
        json={"lease_id": new["lease_id"], "generation": old["generation"]},
        headers=device_headers(token),
    )
    assert resp.status_code == 409

    leases = client.get(f"/admin/tenants/{tid}/leases", headers=h).json()
    by_id = {l["id"]: l for l in leases}
    assert by_id[old["lease_id"]]["state"] == "released"
    assert by_id[new["lease_id"]]["state"] == "active"
    # Same instant, possibly a different offset spelling — compare as datetimes.
    assert datetime.fromisoformat(by_id[new["lease_id"]]["expires_at"]) == datetime.fromisoformat(
        new_expiry_before
    )


def test_restart_recovery_releases_expired_leases_exactly_once(client, db, settings):
    h = admin_headers(client)
    tid, ap_id = make_tenant_env(client, h)
    _, token = create_device(client, h, tid)
    lease = connect(client, token, ap_id, key="k1").json()
    _force_expiry(db, lease["lease_id"], seconds_ago=3600)

    # Simulate a service restart: a brand-new app instance runs its lifespan
    # startup, which sweeps leases that expired while the service was down.
    from app.main import create_app

    app2 = create_app(settings)
    with TestClient(app2):
        pass

    terms = client.get(f"/admin/tenants/{tid}/terminations", headers=h).json()
    assert len(terms) == 1
    assert terms[0]["lease_id"] == lease["lease_id"]
    assert terms[0]["release_reason"] == "expired"
    released_at = terms[0]["released_at"]

    # A second sweep (or a second restart) must not move released_at: one lease,
    # one termination.
    run_sweep(app2.state.SessionLocal)
    with TestClient(create_app(settings)):
        pass
    terms2 = client.get(f"/admin/tenants/{tid}/terminations", headers=h).json()
    assert len(terms2) == 1
    assert terms2[0]["released_at"] == released_at
