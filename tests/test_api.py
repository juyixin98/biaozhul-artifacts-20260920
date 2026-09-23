"""API-level tests via FastAPI TestClient."""

import pytest
from fastapi.testclient import TestClient

from app.main import app

client = TestClient(app)

BALANCED = {
    "reserves": ["1000000000000", "1000000000000000000000000"],  # 1e6 each
    "decimals": [6, 18],
    "amplification": 100,
    "token_in": 0,
    "amount_in": "1000000",  # 1.0 of token0
}


def test_health():
    r = client.get("/v1/health")
    assert r.status_code == 200
    assert r.json()["status"] == "ok"


def test_metadata_documents_invariant_and_ranges():
    m = client.get("/v1/metadata").json()
    assert "invariant" in m and "Ann" in m["invariant"]
    assert m["amplification_range"] == [1, 5000]
    assert m["solvers"]["reference"] == "bisection (independent root-finder)"


def test_quote_balanced_near_one_to_one():
    r = client.post("/v1/quote", json=BALANCED)
    assert r.status_code == 200
    body = r.json()
    assert body["status"] == "ok"
    assert body["executable"] is True
    q = body["quote"]
    assert q["token_out"] == 1
    out = int(q["amount_out"])
    # Balanced pool: output is slightly below input (slippage), never above.
    assert 0 < out <= 10**18
    assert out > 99 * 10**16  # > 0.99 for a 1-unit trade in a 1e6 pool
    # Solver diagnostics are present and converged.
    assert body["newton"]["converged"] is True
    assert body["bisection_reference"]["converged"] is True
    assert body["newton"]["iterations"] > 0
    # Newton and bisection agree on the normalised output.
    diff = abs(int(q["amount_out_normalized"])
               - int(body["reference_amount_out_normalized"]))
    assert diff <= 4


def test_quote_extreme_imbalance():
    req = {
        "reserves": ["1", "1000000000000"],  # 1 wei vs 1e6 units
        "decimals": [18, 6],
        "amplification": 100,
        "token_in": 1,
        "amount_in": "1000",
    }
    body = client.post("/v1/quote", json=req).json()
    assert body["status"] == "ok"
    assert body["executable"] is True
    assert int(body["quote"]["amount_out"]) >= 0


def test_quote_small_input_one_wei():
    req = dict(BALANCED, amount_in="1")
    body = client.post("/v1/quote", json=req).json()
    assert body["status"] == "ok"
    assert int(body["quote"]["amount_out"]) >= 0


def test_quote_zero_reserves_not_executable():
    req = dict(BALANCED, reserves=["0", "0"])
    body = client.post("/v1/quote", json=req).json()
    assert body["status"] == "invalid_pool"
    assert body["executable"] is False
    assert body["quote"] is None

    req = dict(BALANCED, reserves=["0", "1000000000000000000000000"])
    body = client.post("/v1/quote", json=req).json()
    assert body["status"] == "invalid_pool"
    assert body["quote"] is None


def test_quote_amplification_boundaries():
    assert client.post("/v1/quote", json=dict(BALANCED, amplification=1)
                       ).json()["executable"] is True
    assert client.post("/v1/quote", json=dict(BALANCED, amplification=5000)
                       ).json()["executable"] is True
    # Outside the supported range -> schema rejection.
    assert client.post("/v1/quote", json=dict(BALANCED, amplification=0)).status_code == 422
    assert client.post("/v1/quote", json=dict(BALANCED, amplification=5001)).status_code == 422


def test_quote_iteration_limit_never_executable():
    req = dict(BALANCED, max_iter=1)
    body = client.post("/v1/quote", json=req).json()
    assert body["status"] == "not_converged"
    assert body["executable"] is False
    assert body["quote"] is None
    assert body["newton"]["converged"] is False


def test_quote_max_iter_hard_cap_enforced():
    assert client.post("/v1/quote", json=dict(BALANCED, max_iter=1001)).status_code == 422
    assert client.post("/v1/quote", json=dict(BALANCED, max_iter=0)).status_code == 422


def test_quote_rejects_malformed_amounts():
    assert client.post("/v1/quote", json=dict(BALANCED, amount_in="0")).status_code == 422
    assert client.post("/v1/quote", json=dict(BALANCED, amount_in="-5")).status_code == 422
    assert client.post("/v1/quote", json=dict(BALANCED, amount_in="1.5")).status_code == 422
    assert client.post("/v1/quote", json=dict(BALANCED, amount_in=100)).status_code == 422
    assert client.post("/v1/quote", json=dict(BALANCED, token_in=2)).status_code == 422
    assert client.post("/v1/quote", json=dict(BALANCED, decimals=[6, 19])).status_code == 422


def test_quote_direction_symmetry():
    fwd = client.post("/v1/quote", json=BALANCED).json()
    rev_req = dict(BALANCED, token_in=1, amount_in="1000000000000000000")
    rev = client.post("/v1/quote", json=rev_req).json()
    assert fwd["executable"] and rev["executable"]
    # Symmetric pool: swapping 1.0 in either direction gives ~equal outputs
    # in the other token's units (1e18 normalised ~ 1e6 of the 6-decimal token).
    assert abs(int(fwd["quote"]["amount_out_normalized"])
               - int(rev["quote"]["amount_out_normalized"])) <= 10**10
