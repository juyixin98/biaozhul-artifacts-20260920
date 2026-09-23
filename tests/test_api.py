"""HTTP-level tests through the FastAPI app (TestClient)."""

from __future__ import annotations

import pytest
from fastapi.testclient import TestClient

from app.main import app

client = TestClient(app)

BASE = dict(
    reserve_in_units="100000000",
    reserve_out_units="100000000",
    amount_in_units="10000000",
    decimals_in=6,
    decimals_out=6,
    amp=20,
)


def test_health():
    r = client.get("/health")
    assert r.status_code == 200 and r.json()["status"] == "ok"


def test_invariant_spec_documents_equation_and_amp_range():
    j = client.get("/invariant").json()
    assert j["amp_min"] == 1 and j["amp_max"] == 100000
    assert "D^3" in j["equation_normalized"]
    assert j["decimal_precision_digits"] == 80
    assert "not a byte-for-byte replica" in j["note"].lower()


def test_quote_happy_path():
    r = client.post("/quote", json=BASE)
    assert r.status_code == 200
    j = r.json()
    assert j["tradable"] is True
    assert j["quote"]["amount_out_units"].isdigit()
    assert j["solver"]["roots_agree"] is True
    assert j["solver"]["post_trade"]["invariant_ok"] is True
    assert j["precision_digits"] == 80


def test_quote_accepts_int_or_string_amounts():
    a = client.post("/quote", json=BASE).json()
    b = client.post("/quote", json={**BASE, "amount_in_units": 10000000}).json()
    assert a["quote"]["amount_out_units"] == b["quote"]["amount_out_units"]


def test_quote_id_sha256_stable():
    j1 = client.post("/quote", json=BASE).json()
    j2 = client.post("/quote", json=BASE).json()
    assert j1["quote_id"] == j2["quote_id"] and len(j1["quote_id"]) == 64


@pytest.mark.parametrize("field,value", [
    ("reserve_in_units", "0"),
    ("reserve_out_units", "0"),
    ("amount_in_units", "0"),
])
def test_zero_values_422(field, value):
    r = client.post("/quote", json={**BASE, field: value})
    assert r.status_code == 422
    assert r.json()["tradable"] is False
    assert r.json()["error"]["code"] in {"ZERO_RESERVE", "ZERO_INPUT"}


@pytest.mark.parametrize("amp", [0, -1, 100001])
def test_amp_out_of_range_422(amp):
    r = client.post("/quote", json={**BASE, "amp": amp})
    assert r.status_code == 422


def test_iteration_cap_nonconvergent_no_quote():
    r = client.post("/quote", json={**BASE, "max_iterations": 1})
    assert r.status_code == 200
    j = r.json()
    assert j["tradable"] is False
    assert j["error"]["code"] in {"D_NOT_CONVERGED", "Y_NOT_CONVERGED"}
    assert j["quote"] is None


def test_nonconvergent_never_returns_amount():
    # across caps, a non-tradable response must never carry a payout
    for cap in [1, 2, 3]:
        j = client.post("/quote", json={**BASE, "max_iterations": cap}).json()
        if not j["tradable"]:
            assert j["quote"] is None
            assert j["error"] is not None


def test_extreme_imbalance_http():
    j = client.post("/quote", json=dict(
        reserve_in_units=str(100 * 10 ** 6), reserve_out_units="100",
        amount_in_units="1000000", decimals_in=6, decimals_out=0, amp=20)).json()
    assert j["solver"]["d_newton"]["converged"] is True


def test_diagnostic_sweep_is_flagged_non_tradable():
    r = client.post("/diagnostic/sweep",
                    params=dict(reserve_in=100, reserve_out=100, amp=20, n_points=8, max_fraction=0.5))
    assert r.status_code == 200
    j = r.json()
    assert j["precision"] == "float64-diagnostic"
    assert "not a tradable quote" in j["warning"].lower()
    assert len(j["points"]) == 8


def test_diagnostic_sweep_rejects_bad_params():
    r = client.post("/diagnostic/sweep",
                    params=dict(reserve_in=0, reserve_out=100, amp=20))
    assert r.status_code == 422


def test_response_schema_complete():
    j = client.post("/quote", json=BASE).json()
    for k in ["quote_id", "tradable", "invariant", "amp_range", "precision_digits",
              "output_rounding", "pool", "quote", "solver"]:
        assert k in j
    for k in ["d_newton", "d_bisection", "y_newton", "y_bisection",
              "roots_agree", "root_agreement_rel", "post_trade"]:
        assert k in j["solver"]
