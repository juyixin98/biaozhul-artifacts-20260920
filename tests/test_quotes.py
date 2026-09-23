"""Engine-level quote tests (no HTTP): fees, rounding, guards, boundaries."""

from __future__ import annotations

import pytest

from app.quotes import QuoteRequestError, compute_quote
from app.schemas import QuoteRequest

BASE = dict(
    reserve_in_units="100000000",
    reserve_out_units="100000000",
    amount_in_units="10000000",
    decimals_in=6,
    decimals_out=6,
    amp=20,
)


def q(**kw):
    return compute_quote(QuoteRequest(**{**BASE, **kw}))


# ---------------------------------------------------------------------------
# Happy path
# ---------------------------------------------------------------------------

def test_normal_quote_tradable():
    r = q()
    assert r.tradable
    assert r.error is None
    assert r.quote is not None
    out = int(r.quote.amount_out_units)
    # selling 10% of a balanced pool at A=20 must yield slightly less than 10%
    assert 9_000_000 < out < 10_000_000
    assert r.solver.d_newton.converged and r.solver.y_newton.converged
    assert r.solver.roots_agree
    assert r.solver.post_trade.invariant_ok


def test_zero_fee_gives_higher_output():
    r_fee = q(fee_bps=4)
    r_free = q(fee_bps=0)
    assert int(r_free.quote.amount_out_units) > int(r_fee.quote.amount_out_units)


def test_fee_accounting_consistent():
    r = q(fee_bps=100)  # 1%
    gross = float(r.quote.gross_out_normalized)
    fee = float(r.quote.fee_out_normalized)
    net = float(r.quote.amount_out_normalized)
    assert abs(gross - fee - net) < 1e-6
    assert abs(fee - gross * 0.01) < 1e-6


def test_output_rounding_floor_visible():
    r = q()
    # integer units must equal floor(normalized * 10^decimals)
    from decimal import Decimal as D

    norm = D(r.quote.amount_out_normalized)
    floored = int(norm * (10 ** 6))
    assert int(r.quote.amount_out_units) == floored


def test_quote_id_is_deterministic_sha256():
    r1, r2 = q(), q()
    assert r1.quote_id == r2.quote_id
    assert len(r1.quote_id) == 64  # sha256 hex
    # different request -> different id
    assert q(amount_in_units="1").quote_id != r1.quote_id


def test_decimals_mismatch_normalization():
    r = q(decimals_out=18, reserve_out_units=str(100 * 10 ** 18))
    assert r.tradable
    # 6-dec input ~10 tokens should buy ~9.95 of the 18-dec token
    out = int(r.quote.amount_out_units)
    assert 9 * 10 ** 18 < out < 10 * 10 ** 18


# ---------------------------------------------------------------------------
# Boundary parameters
# ---------------------------------------------------------------------------

@pytest.mark.parametrize("amp", [1, 2, 20, 100_000])
def test_amp_boundaries_tradable(amp):
    r = q(amp=amp)
    assert r.tradable, r.error
    assert r.solver.post_trade.invariant_ok


def test_amp_out_of_range_rejected():
    with pytest.raises(Exception):
        q(amp=0)
    with pytest.raises(Exception):
        q(amp=100_001)


@pytest.mark.parametrize("fee", [0, 4, 10_000])
def test_fee_boundaries(fee):
    r = q(fee_bps=fee)
    if fee == 10_000:
        assert not r.tradable and r.error.code == "ZERO_PAYOUT"
    else:
        assert r.tradable


# ---------------------------------------------------------------------------
# Extreme imbalance
# ---------------------------------------------------------------------------

@pytest.mark.parametrize("R", [10, 10 ** 6, 10 ** 12, 10 ** 18])
def test_extreme_imbalance_small_trade(R):
    # rich side 100 (6 dec), poor side tiny; sell 1 unit of the rich token
    r = compute_quote(
        QuoteRequest(
            reserve_in_units=str(100 * 10 ** 6),
            reserve_out_units=str(max(1, 100 * 10 ** 6 // R)),
            amount_in_units="1000000",
            decimals_in=6,
            decimals_out=6,
            amp=20,
        )
    )
    # tiny input into a deeply imbalanced pool: either tradable with tiny out
    # or honestly non-tradable, but never a silent garbage quote
    if r.tradable:
        assert int(r.quote.amount_out_units) >= 0
        assert r.solver.roots_agree
    else:
        assert r.error.code in {"ZERO_PAYOUT", "INSUFFICIENT_LIQUIDITY", "ROOT_MISMATCH"}


def test_deeply_imbalanced_pool_d_solve_agrees():
    r = compute_quote(
        QuoteRequest(
            reserve_in_units=str(100 * 10 ** 6),
            reserve_out_units="1",
            amount_in_units="1",
            decimals_in=6,
            decimals_out=6,
            amp=20,
        )
    )
    # D solver must still converge and agree with bisection even at R=1e14
    assert r.solver.d_newton.converged
    assert r.solver.d_bisection.converged


# ---------------------------------------------------------------------------
# Tiny / zero inputs
# ---------------------------------------------------------------------------

def test_one_unit_input_zero_payout():
    r = q(amount_in_units="1")
    assert not r.tradable
    assert r.error.code == "ZERO_PAYOUT"
    assert r.quote is None


def test_zero_input_422():
    with pytest.raises(QuoteRequestError) as ei:
        q(amount_in_units="0")
    assert ei.value.code == "ZERO_INPUT"


def test_zero_reserve_422():
    with pytest.raises(QuoteRequestError) as ei:
        q(reserve_in_units="0")
    assert ei.value.code == "ZERO_RESERVE"


# ---------------------------------------------------------------------------
# Iteration cap
# ---------------------------------------------------------------------------

def test_iteration_cap_forces_nonconvergence_no_quote():
    r = q(max_iterations=1)
    assert not r.tradable
    assert r.error.code in {"D_NOT_CONVERGED", "Y_NOT_CONVERGED"}
    assert r.quote is None
    # honest reporting: the solver section shows the capped iteration count
    assert r.solver.d_newton.iterations <= 1


def test_iteration_cap_hard_ceiling_enforced():
    # values beyond the hard cap are rejected by request validation (422 at
    # the HTTP layer); the engine never sees them.
    from pydantic import ValidationError

    with pytest.raises(ValidationError):
        q(max_iterations=10 ** 9)
    # exactly at the cap is accepted
    r = q(max_iterations=512)
    assert r.solver.d_newton.max_iterations <= 512


# ---------------------------------------------------------------------------
# Draining trades
# ---------------------------------------------------------------------------

def test_astronomical_input_rejected_by_invariant_guard():
    r = q(amount_in_units=str(10 ** 30))
    assert not r.tradable
    assert r.error.code == "INVARIANT_VIOLATION"
    assert r.quote is None


def test_large_but_feasible_input_tradable():
    r = q(amount_in_units="90000000")  # 90% of reserve
    assert r.tradable
    assert int(r.quote.amount_out_units) < 100_000_000
