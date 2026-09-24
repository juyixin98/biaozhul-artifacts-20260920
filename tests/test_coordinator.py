"""Tests for the read-only FastAPI coordinator, including a chain outage."""

from __future__ import annotations

import pytest
from fastapi.testclient import TestClient

from htlclib import coordinator
from htlclib.chains import DELTA_SECONDS
from htlclib.contract import derive_swap_id
from htlclib.harness import lock_both, run_happy_swap, run_timeout_gap_loss, timelines


@pytest.fixture()
def client(env, monkeypatch):
    monkeypatch.setattr(coordinator, "DEPLOYMENT_FILE", env.deployment_file)
    # coordinator builds ad-hoc Web3 clients from deployment.json specs
    return TestClient(coordinator.app)


def test_health_all_up(client):
    r = client.get("/health")
    assert r.status_code == 200
    data = r.json()["chains"]
    assert data["A"]["up"] is True and data["B"]["up"] is True
    assert data["A"]["chain_id_matches"] is True
    assert data["B"]["chain_id_matches"] is True


def test_health_reports_down_chain(client, env):
    env.chain_b.stop()
    r = client.get("/health")
    assert r.status_code == 503
    data = r.json()["chains"]
    assert data["A"]["up"] is True
    assert data["B"]["up"] is False


def test_convention_endpoint(client):
    r = client.get("/convention")
    assert r.status_code == 200
    body = r.json()
    assert body["delta_seconds"] == DELTA_SECONDS
    assert "arbitrary cross-chain atomicity is NOT solved" in body["scope"]
    assert sorted(body["state_machine"]["LOCKED"]) == ["CLAIMED", "REFUNDED"]


def test_single_swap_reads(client, env):
    _, t_a, t_b = timelines(env)
    locked = lock_both(env, 10**18, t_a, t_b)
    r = client.get(f"/swaps/A/{locked['id_a'].hex()}")
    assert r.status_code == 200
    body = r.json()
    assert body["state"] == "LOCKED"
    assert body["sender"].lower() == env.alice.address.lower()
    assert body["receiver"].lower() == env.bob.address.lower()
    assert body["seconds_remaining"] >= 0


def test_swap_read_returns_502_when_chain_down(client, env):
    _, t_a, t_b = timelines(env)
    locked = lock_both(env, 10**18, t_a, t_b)
    env.chain_a.stop()
    r = client.get(f"/swaps/A/{locked['id_a'].hex()}")
    assert r.status_code == 502
    assert "unavailable" in r.json()["detail"]


def test_pair_view_happy_and_derived_ids(client, env):
    result = run_happy_swap(env)
    # query using the raw ids
    r = client.get("/pair", params={
        "swap_id_a": result["ids"]["A"],
        "swap_id_b": result["ids"]["B"],
    })
    assert r.status_code == 200
    body = r.json()
    assert body["chain_a"]["state"] == "CLAIMED"
    assert body["chain_b"]["state"] == "CLAIMED"
    assert body["assessment"]["risk_level"] == "settled"


def test_pair_view_derives_ids_from_lock_params(client, env):
    _, t_a, t_b = timelines(env)
    locked = lock_both(env, 10**18, t_a, t_b)
    r = client.get("/pair", params={
        "hash_lock": "0x" + locked["hash_lock"].hex(),
        "sender_a": env.alice.address,
        "receiver_a": env.bob.address,
        "sender_b": env.bob.address,
        "receiver_b": env.alice.address,
        "amount_wei": 10**18,
        "timelock_a": t_a,
    })
    assert r.status_code == 200
    body = r.json()
    assert body["chain_a"]["state"] == "LOCKED"
    assert body["chain_b"]["state"] == "LOCKED"
    assert body["assessment"]["risk_level"] == "in_progress"
    # cross-check the derivation matches the on-chain Locked ids
    assert body["chain_a"]["swap_id"] == locked["id_a"].hex()
    assert body["chain_b"]["swap_id"] == locked["id_b"].hex()


def test_pair_view_flags_split_outcome_as_race(env):
    # B=CLAIMED, A=REFUNDED is exactly the timeout-gap split outcome.
    assessment = coordinator._classify_pair(
        {"state": "REFUNDED"}, {"state": "CLAIMED"})
    assert assessment["risk_level"] == "race"
    assert "timeout-gap boundary" in assessment["notes"][0]


def test_pair_view_reports_unreachable_chain(client, env):
    _, t_a, t_b = timelines(env)
    locked = lock_both(env, 10**18, t_a, t_b)
    env.chain_b.stop()
    r = client.get("/pair", params={
        "swap_id_a": locked["id_a"].hex(),
        "swap_id_b": locked["id_b"].hex(),
    })
    assert r.status_code == 200
    body = r.json()
    assert body["chain_b"]["risk_level"] == "unreachable"
    assert body["assessment"]["risk_level"] == "unreachable"


def test_pair_missing_params_is_400(client):
    r = client.get("/pair")
    assert r.status_code == 400


def test_derivation_helper_matches_contract_packing(env):
    # unit-level confidence for the coordinator's id derivation
    _, t_a, t_b = timelines(env)
    locked = lock_both(env, 10**18, t_a, t_b)
    id_a = derive_swap_id(locked["hash_lock"], env.alice.address,
                          env.bob.address, 10**18, t_a)
    assert id_a == locked["id_a"]
