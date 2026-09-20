"""Generations: reconnects supersede the old lease and late operations on the
old generation can never revive or extend it."""

from app.db import SessionLocal
from app.models import LeaseTermination


def test_reconnect_bumps_generation_and_releases_old(client, make_ap, make_device, api_connect):
    ap = make_ap(cidr="10.50.0.0/24", capacity=10)
    device = make_device(ap["tenant"], name="gen")

    l1 = api_connect(device, ap).json()
    assert l1["session"]["generation"] == 1

    l2 = api_connect(device, ap).json()
    assert l2["session"]["generation"] == 2
    assert l2["session"]["id"] != l1["session"]["id"]
    assert l2["session"]["ip"] != l1["session"]["ip"]
    assert l2["lease_token"] != l1["lease_token"]

    with SessionLocal() as db:
        t1 = db.query(LeaseTermination).filter_by(session_id=l1["session"]["id"]).one()
    assert t1.reason == "reconnect"


def heartbeat(client, device, lease, generation=None):
    return client.post(
        "/api/device/heartbeat",
        headers={"X-Device-Token": device["token"], "X-Lease-Token": lease["lease_token"]},
        json={
            "session_id": lease["session"]["id"],
            "generation": generation if generation is not None else lease["session"]["generation"],
        },
    )


def test_late_heartbeat_old_generation_rejected(client, make_ap, make_device, api_connect):
    ap = make_ap(cidr="10.51.0.0/24", capacity=10)
    device = make_device(ap["tenant"], name="late")
    old = api_connect(device, ap).json()
    new = api_connect(device, ap).json()

    # Old token against old session/gen: session closed -> 409.
    resp = heartbeat(client, device, old)
    assert resp.status_code == 409

    # Old token cannot authenticate against the new session either.
    resp = client.post(
        "/api/device/heartbeat",
        headers={"X-Device-Token": device["token"], "X-Lease-Token": old["lease_token"]},
        json={"session_id": new["session"]["id"], "generation": new["session"]["generation"]},
    )
    assert resp.status_code == 403
    assert resp.json()["error"]["code"] == "invalid_lease_token"

    # Forged/garbage generation on the active lease rejected.
    resp = heartbeat(client, device, new, generation=new["session"]["generation"] + 5)
    assert resp.status_code == 409
    assert resp.json()["error"]["code"] == "stale_generation"


def test_superseded_lease_cannot_be_closed_with_new_token(client, make_ap, make_device, api_connect):
    ap = make_ap(cidr="10.52.0.0/24", capacity=10)
    device = make_device(ap["tenant"], name="mix")
    old = api_connect(device, ap).json()
    new = api_connect(device, ap).json()

    # New lease token used to close the OLD session id must fail auth.
    resp = client.post(
        "/api/device/close",
        headers={"X-Device-Token": device["token"], "X-Lease-Token": new["lease_token"]},
        json={"session_id": old["session"]["id"], "generation": old["session"]["generation"]},
    )
    assert resp.status_code == 403


def test_heartbeat_updates_timestamp(client, make_ap, make_device, api_connect):
    import time as _time

    ap = make_ap(cidr="10.53.0.0/24", capacity=10)
    device = make_device(ap["tenant"], name="hb")
    lease = api_connect(device, ap).json()

    _time.sleep(0.01)
    resp = heartbeat(client, device, lease)
    assert resp.status_code == 200
    assert resp.json()["last_heartbeat_at"] >= lease["session"]["last_heartbeat_at"]
