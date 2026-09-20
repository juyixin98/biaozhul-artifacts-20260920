"""Concurrency: capacity races, one-active-session, idempotent retries and
unique IP allocation. These use real HTTP worker threads, so row locks and
unique indexes are genuinely exercised."""

import threading

import pytest


def _post_async(client, results, idx, device_token, ap_id, key=None):
    def run():
        try:
            results[idx] = client.post(
                "/api/device/connect",
                headers={"X-Device-Token": device_token},
                json={"access_point_id": ap_id, "idempotency_key": key or f"k{idx}"},
            )
        except Exception as exc:  # pragma: no cover - surface in results
            results[idx] = exc

    t = threading.Thread(target=run)
    t.start()
    return t


def test_capacity_race_exactly_capacity_succeed(client, make_ap, make_device):
    # /28 = 14 usable hosts, AP capacity 5; 12 devices hammer simultaneously.
    ap = make_ap(cidr="192.168.50.0/28", capacity=5)
    tenant = ap["tenant"]
    devices = [make_device(tenant, name=f"d{i}") for i in range(12)]

    results: list = [None] * 12
    threads = [
        _post_async(client, results, i, devices[i]["token"], ap["id"]) for i in range(12)
    ]
    for t in threads:
        t.join()

    succeeded = [r for r in results if r.status_code == 201]
    rejected = [r for r in results if r.status_code == 503]
    assert len(succeeded) == 5, [(r.status_code, r.text) for r in results]
    assert len(rejected) == 7
    assert all(r.json()["error"]["code"] == "capacity_exceeded" for r in rejected)

    ips = [r.json()["session"]["ip"] for r in succeeded]
    assert len(set(ips)) == 5


def test_concurrent_connects_same_device_one_active(client, make_ap, make_device):
    ap = make_ap(cidr="10.99.0.0/24", capacity=100)
    device = make_device(ap["tenant"], name="solo")

    n = 8
    results: list = [None] * n
    # Different idempotency keys => each call is an independent reconnect.
    threads = [
        _post_async(client, results, i, device["token"], ap["id"], key=f"race-{i}")
        for i in range(n)
    ]
    for t in threads:
        t.join()

    assert all(r.status_code == 201 for r in results), [(r.status_code, r.text) for r in results]

    # Database invariant: exactly one active session for the device.
    from app.db import SessionLocal
    from app.models import Session as SessionModel
    from sqlalchemy import func, select

    with SessionLocal() as db:
        active = db.execute(
            select(func.count()).select_from(SessionModel).where(
                SessionModel.device_id == _device_id(device), SessionModel.status == "active"
            )
        ).scalar_one()
        total = db.execute(
            select(func.count()).select_from(SessionModel).where(
                SessionModel.device_id == _device_id(device)
            )
        ).scalar_one()
    assert active == 1
    assert total == n  # every request created its own generation

    # Exactly one termination per superseded generation, all releases once.
    from app.db import SessionLocal
    from app.models import LeaseTermination, PoolAddress
    with SessionLocal() as db:
        terminations = db.execute(
            select(func.count()).select_from(LeaseTermination).join(
                SessionModel, LeaseTermination.session_id == SessionModel.id
            ).where(SessionModel.device_id == _device_id(device))
        ).scalar_one()
        allocated = db.execute(
            select(func.count()).select_from(PoolAddress).where(PoolAddress.status == "allocated")
        ).scalar_one()
    assert terminations == n - 1
    assert allocated == 1


def _device_id(device):
    import uuid

    return uuid.UUID(device["id"])


def test_idempotent_concurrent_retry_single_lease(client, make_ap, make_device):
    ap = make_ap(cidr="10.98.0.0/24", capacity=10)
    device = make_device(ap["tenant"], name="retry")

    n = 6
    results: list = [None] * n
    threads = [
        _post_async(client, results, i, device["token"], ap["id"], key="same-key-123")
        for i in range(n)
    ]
    for t in threads:
        t.join()

    assert all(r.status_code == 201 for r in results), [(r.status_code, r.text) for r in results]
    session_ids = {r.json()["session"]["id"] for r in results}
    assert len(session_ids) == 1
    ips = {r.json()["session"]["ip"] for r in results}
    assert len(ips) == 1

    from app.db import SessionLocal
    from app.models import Session as SessionModel
    from sqlalchemy import func, select

    with SessionLocal() as db:
        total = db.execute(
            select(func.count()).select_from(SessionModel).where(
                SessionModel.device_id == _device_id(device)
            )
        ).scalar_one()
    assert total == 1


def test_concurrent_closes_release_exactly_once(client, make_ap, make_device, api_connect):
    ap = make_ap(cidr="10.77.0.0/24", capacity=5)
    device = make_device(ap["tenant"], name="closer")
    lease = api_connect(device, ap).json()

    n = 5
    results: list = [None] * n

    def close_one(i):
        results[i] = client.post(
            "/api/device/close",
            headers={"X-Device-Token": device["token"], "X-Lease-Token": lease["lease_token"]},
            json={"session_id": lease["session"]["id"], "generation": lease["session"]["generation"]},
        )

    threads = [threading.Thread(target=close_one, args=(i,)) for i in range(n)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()

    assert all(r.status_code == 200 for r in results)

    from app.db import SessionLocal
    from app.models import LeaseTermination
    from sqlalchemy import func, select

    with SessionLocal() as db:
        terms = db.execute(
            select(func.count()).select_from(LeaseTermination).where(
                LeaseTermination.session_id == lease["session"]["id"]
            )
        ).scalar_one()
    assert terms == 1
