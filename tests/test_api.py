"""End-to-end HTTP API tests (FastAPI TestClient + real SQLite)."""

from __future__ import annotations

POOL_BODY = {
    "pool_id": "eth-usdc-demo",
    "token0": "USDC",
    "token1": "WETH",
    "current_tick": 1000,
    "fee_numerator": 3000,
    "fee_denominator": 1_000_000,
    "positions": [
        {"lower_tick": 900, "upper_tick": 1000, "liquidity": "1000000000000000000000000000000"},
        {"lower_tick": 1000, "upper_tick": 1100, "liquidity": "1000000000000000000000000000000"},
    ],
}


def _create(client, body=None):
    return client.post("/pools", json=body or POOL_BODY)


def test_health(client) -> None:
    r = client.get("/health")
    assert r.status_code == 200
    data = r.json()
    assert data["status"] == "ok"
    assert data["tick_range"] == [-100000, 100000]
    assert data["sqrt_decimals"] == 38


def test_create_pool_and_get_detail(client) -> None:
    r = _create(client)
    assert r.status_code == 201, r.text
    detail = r.json()
    assert detail["pool_digest"]
    assert detail["fee_bps"] == 30
    # Price map is documented with the rounding direction.
    pm = detail["price_map"]
    assert pm["rounding"].startswith("floor")
    assert pm["sqrt_price_decimals"] == 38
    # Intervals are materialized (the gap edges 900/1000/1100 plus universe edges).
    ticks = [(row["lower_tick"], row["upper_tick"]) for row in detail["interval_table"]]
    assert (900, 1000) in ticks and (1000, 1100) in ticks

    r2 = client.get("/pools/eth-usdc-demo")
    assert r2.status_code == 200
    assert r2.json()["pool_digest"] == detail["pool_digest"]


def test_duplicate_pool_rejected(client) -> None:
    assert _create(client).status_code == 201
    assert _create(client).status_code == 409


def test_quote_zero_for_one_with_reference_verification(client) -> None:
    _create(client)
    r = client.post("/pools/eth-usdc-demo/quote", json={
        "zero_for_one": True, "amount_in": "1000000000000000000",
    })
    assert r.status_code == 201, r.text
    body = r.json()
    result = body["result"]
    assert body["verification"]["ok"] is True
    assert body["verification"]["discrepancies"] == []
    assert int(result["amount_out"]) > 0
    assert int(result["fee_paid"]) > 0
    # Conservation across the evidence chain.
    gross = sum(int(s["gross_input"]) for s in result["segments"])
    assert gross + int(result["unspent_input"]) == int(result["amount_in"])


def test_quote_one_for_zero_and_persistence(client) -> None:
    _create(client)
    r = client.post("/pools/eth-usdc-demo/quote", json={
        "zero_for_one": False, "amount_in": 5 * 10**17,
    })
    assert r.status_code == 201, r.text
    quote_id = r.json()["quote_id"]

    fetched = client.get(f"/quotes/{quote_id}")
    assert fetched.status_code == 200
    assert fetched.json()["result"]["quote_digest"] == r.json()["result"]["quote_digest"]


def test_quote_unknown_pool_404(client) -> None:
    r = client.post("/pools/nope/quote", json={"zero_for_one": True, "amount_in": "1"})
    assert r.status_code == 404


def test_quote_rejects_bad_amounts(client) -> None:
    _create(client)
    for bad in ["0", "-1", "abc"]:
        r = client.post("/pools/eth-usdc-demo/quote", json={
            "zero_for_one": True, "amount_in": bad,
        })
        assert r.status_code == 422, bad


def test_quote_against_empty_pool_stops_and_returns_input(client) -> None:
    client.post("/pools", json={
        "pool_id": "empty", "token0": "A", "token1": "B",
        "current_tick": 0, "positions": [],
    })
    r = client.post("/pools/empty/quote", json={"zero_for_one": True, "amount_in": "999"})
    assert r.status_code == 201, r.text
    result = r.json()["result"]
    assert result["stop_reason"] == "empty_range"
    assert result["amount_out"] == "0"
    assert result["unspent_input"] == "999"


def test_verify_endpoint_recomputes_digests(client) -> None:
    _create(client)
    q = client.post("/pools/eth-usdc-demo/quote", json={
        "zero_for_one": True, "amount_in": "1234567890",
    }).json()
    v = client.post(f"/quotes/{q['quote_id']}/verify")
    assert v.status_code == 200, v.text
    data = v.json()
    assert data["verified"] is True
    assert data["quote_digest_ok"] is True
    assert data["pool_snapshot_digest_ok"] is True
    assert data["engine_matches_reference"] is True
    assert data["tick_map_check"]["match"] is True


def test_reserves_unchanged_after_quotes(client) -> None:
    _create(client)
    before = client.get("/pools/eth-usdc-demo").json()
    for zfo in (True, False):
        client.post("/pools/eth-usdc-demo/quote", json={
            "zero_for_one": zfo, "amount_in": "10000000000000000000",
        })
    after = client.get("/pools/eth-usdc-demo").json()
    assert after["current_tick"] == before["current_tick"]
    assert after["positions"] == before["positions"]
    assert after["pool_digest"] == before["pool_digest"]


def test_invalid_tick_and_positions_rejected(client) -> None:
    bad_bodies = [
        {**POOL_BODY, "pool_id": "bad1", "current_tick": 100001},
        {**POOL_BODY, "pool_id": "bad2",
         "positions": [{"lower_tick": 200, "upper_tick": 100, "liquidity": 1}]},
        {**POOL_BODY, "pool_id": "bad3", "token0": "X", "token1": "X"},
        {**POOL_BODY, "pool_id": "bad4",
         "positions": [{"lower_tick": 0, "upper_tick": 10, "liquidity": -1}]},
    ]
    for body in bad_bodies:
        assert client.post("/pools", json=body).status_code in (400, 422), body["pool_id"]
