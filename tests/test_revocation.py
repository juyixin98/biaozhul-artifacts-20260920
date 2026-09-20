"""Device revocation and its races with heartbeat/expiry."""
from __future__ import annotations

from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timedelta, timezone

from sqlalchemy import update
from starlette.testclient import TestClient

from app.models import Lease
from app.services.leases import run_sweep

from .helpers import admin_headers, connect, create_device, device_headers, make_tenant_env


def test_revoke_terminates_session_and_blocks_token(client):
    h = admin_headers(client)
    tid, ap_id = make_tenant_env(client, h)
    dev_id, token = create_device(client, h, tid)
    lease = connect(client, token, ap_id, key="k1").json()

    resp = client.post(f"/admin/tenants/{tid}/devices/{dev_id}/revoke", headers=h)
    assert resp.status_code == 200
    assert resp.json()["revoked"] is True

    # Session terminated with a recorded reason.
    terms = client.get(f"/admin/tenants/{tid}/terminations", headers=h).json()
    assert len(terms) == 1
    assert terms[0]["lease_id"] == lease["lease_id"]
    assert terms[0]["release_reason"] == "revoked"
    assert terms[0]["released_at"] is not None

    # The old token is dead everywhere: reconnect, heartbeat, disconnect, query.
    assert connect(client, token, ap_id, key="k2").status_code == 401
    assert client.post(
        "/device/heartbeat",
        json={"lease_id": lease["lease_id"], "generation": lease["generation"]},
        headers=device_headers(token),
    ).status_code == 401
    assert client.get("/device/session", headers=device_headers(token)).status_code == 401

    # Address was released: another device gets it.
    _, tok2 = create_device(client, h, tid, "d2")
    r = connect(client, tok2, ap_id, key="k3")
    assert r.status_code == 200
    assert r.json()["ip"] == lease["ip"]


def test_revoke_without_active_session(client):
    h = admin_headers(client)
    tid, _ = make_tenant_env(client, h)
    dev_id, _ = create_device(client, h, tid)
    assert client.post(f"/admin/tenants/{tid}/devices/{dev_id}/revoke", headers=h).status_code == 200
    assert client.get(f"/admin/tenants/{tid}/terminations", headers=h).json() == []


def test_revoke_races_with_heartbeat_and_disconnect(client, app):
    """Concurrent revoke/heartbeat/disconnect: the lease is released exactly once."""
    h = admin_headers(client)
    tid, ap_id = make_tenant_env(client, h)
    dev_id, token = create_device(client, h, tid)
    lease = connect(client, token, ap_id, key="k1").json()
    lid, gen = lease["lease_id"], lease["generation"]

    def do_revoke():
        c = TestClient(app)
        return c.post(f"/admin/tenants/{tid}/devices/{dev_id}/revoke", headers=h).status_code

    def do_heartbeat():
        c = TestClient(app)
        return c.post(
            "/device/heartbeat", json={"lease_id": lid, "generation": gen},
            headers=device_headers(token),
        ).status_code

    def do_disconnect():
        c = TestClient(app)
        return c.post(
            "/device/disconnect", json={"lease_id": lid, "generation": gen},
            headers=device_headers(token),
        ).status_code

    with ThreadPoolExecutor(max_workers=6) as pool:
        futures = [pool.submit(fn) for fn in (do_revoke, do_heartbeat, do_disconnect,
                                              do_revoke, do_heartbeat, do_disconnect)]
        codes = [f.result() for f in futures]

    assert 200 in codes  # the revoke itself succeeded
    terms = client.get(f"/admin/tenants/{tid}/terminations", headers=h).json()
    mine = [t for t in terms if t["lease_id"] == lid]
    assert len(mine) == 1  # exactly one termination record
    assert mine[0]["release_reason"] in ("revoked", "disconnected")

    leases = client.get(f"/admin/tenants/{tid}/leases", headers=h).json()
    assert [l for l in leases if l["state"] == "active"] == []


def test_revoke_races_with_expiry_sweep(client, app, db):
    h = admin_headers(client)
    tid, ap_id = make_tenant_env(client, h)
    dev_id, token = create_device(client, h, tid)
    lease = connect(client, token, ap_id, key="k1").json()

    past = datetime.now(timezone.utc) - timedelta(seconds=5)
    db.execute(update(Lease).where(Lease.id == lease["lease_id"]).values(expires_at=past))
    db.commit()

    def do_revoke():
        c = TestClient(app)
        return c.post(f"/admin/tenants/{tid}/devices/{dev_id}/revoke", headers=h).status_code

    def do_sweep():
        return run_sweep(app.state.SessionLocal)

    with ThreadPoolExecutor(max_workers=4) as pool:
        futures = [pool.submit(do_revoke), pool.submit(do_sweep),
                   pool.submit(do_revoke), pool.submit(do_sweep)]
        for f in futures:
            f.result()

    terms = client.get(f"/admin/tenants/{tid}/terminations", headers=h).json()
    mine = [t for t in terms if t["lease_id"] == lease["lease_id"]]
    assert len(mine) == 1
    assert mine[0]["release_reason"] in ("revoked", "expired")
