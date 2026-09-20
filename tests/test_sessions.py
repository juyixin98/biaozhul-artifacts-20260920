"""Session lifecycle: connect, idempotency, generation checks, disconnect."""
from __future__ import annotations

from .helpers import admin_headers, connect, create_device, device_headers, make_tenant_env


def _setup(client):
    h = admin_headers(client)
    tid, ap_id = make_tenant_env(client, h)
    dev_id, token = create_device(client, h, tid)
    return h, tid, ap_id, dev_id, token


def test_connect_heartbeat_disconnect_happy_path(client):
    h, tid, ap_id, dev_id, token = _setup(client)

    r = connect(client, token, ap_id, key="k1")
    assert r.status_code == 200
    lease = r.json()
    assert lease["state"] == "active"
    assert lease["generation"] == 1
    assert lease["reused"] is False
    assert lease["heartbeat_interval_seconds"] == 60
    assert lease["ip"].startswith("10.0.0.")

    hb = client.post(
        "/device/heartbeat",
        json={"lease_id": lease["lease_id"], "generation": lease["generation"]},
        headers=device_headers(token),
    )
    assert hb.status_code == 200
    assert hb.json()["expires_at"] >= lease["expires_at"]

    disc = client.post(
        "/device/disconnect",
        json={"lease_id": lease["lease_id"], "generation": lease["generation"]},
        headers=device_headers(token),
    )
    assert disc.status_code == 200
    body = disc.json()
    assert body["state"] == "released"
    assert body["release_reason"] == "disconnected"
    assert body["released_at"] is not None

    # Termination record is visible to the tenant admin.
    terms = client.get(f"/admin/tenants/{tid}/terminations", headers=h).json()
    assert len(terms) == 1
    assert terms[0]["lease_id"] == lease["lease_id"]
    assert terms[0]["release_reason"] == "disconnected"


def test_connect_is_idempotent_per_key(client):
    _, _, ap_id, _, token = _setup(client)
    r1 = connect(client, token, ap_id, key="same-key")
    r2 = connect(client, token, ap_id, key="same-key")
    assert r1.status_code == r2.status_code == 200
    assert r1.json()["lease_id"] == r2.json()["lease_id"]
    assert r1.json()["reused"] is False
    assert r2.json()["reused"] is True


def test_one_active_session_per_device(client):
    _, _, ap_id, _, token = _setup(client)
    assert connect(client, token, ap_id, key="k1").status_code == 200
    r = connect(client, token, ap_id, key="k2")  # different key while active
    assert r.status_code == 409
    assert "active session" in r.json()["detail"]


def test_reconnect_after_disconnect_gets_new_generation(client):
    _, _, ap_id, _, token = _setup(client)
    r1 = connect(client, token, ap_id, key="k1").json()
    client.post(
        "/device/disconnect",
        json={"lease_id": r1["lease_id"], "generation": r1["generation"]},
        headers=device_headers(token),
    )
    r2 = connect(client, token, ap_id, key="k2").json()
    assert r2["generation"] == r1["generation"] + 1
    assert r2["lease_id"] != r1["lease_id"]


def test_heartbeat_and_disconnect_validate_generation(client):
    _, _, ap_id, _, token = _setup(client)
    lease = connect(client, token, ap_id, key="k1").json()
    lid = lease["lease_id"]

    for bad_gen in (lease["generation"] + 1, lease["generation"] - 1, 999):
        resp = client.post(
            "/device/heartbeat",
            json={"lease_id": lid, "generation": bad_gen},
            headers=device_headers(token),
        )
        assert resp.status_code == 409
        resp = client.post(
            "/device/disconnect",
            json={"lease_id": lid, "generation": bad_gen},
            headers=device_headers(token),
        )
        assert resp.status_code == 409

    # Correct generation still works afterwards.
    assert client.post(
        "/device/heartbeat",
        json={"lease_id": lid, "generation": lease["generation"]},
        headers=device_headers(token),
    ).status_code == 200


def test_terminated_session_rejects_heartbeat_and_second_disconnect(client):
    _, _, ap_id, _, token = _setup(client)
    lease = connect(client, token, ap_id, key="k1").json()
    lid, gen = lease["lease_id"], lease["generation"]

    client.post(
        "/device/disconnect", json={"lease_id": lid, "generation": gen}, headers=device_headers(token)
    )
    resp = client.post(
        "/device/heartbeat", json={"lease_id": lid, "generation": gen}, headers=device_headers(token)
    )
    assert resp.status_code == 410
    resp = client.post(
        "/device/disconnect", json={"lease_id": lid, "generation": gen}, headers=device_headers(token)
    )
    assert resp.status_code == 410


def test_current_session_endpoint(client):
    _, _, ap_id, _, token = _setup(client)
    assert client.get("/device/session", headers=device_headers(token)).json() is None
    lease = connect(client, token, ap_id, key="k1").json()
    current = client.get("/device/session", headers=device_headers(token)).json()
    assert current["id"] == lease["lease_id"]
