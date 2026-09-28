"""Kernel acceptance tests.

Every test asserts concrete certified results (or concrete failure
categories), never just "the call succeeded". Reference roots are analytic
constants (sqrt(2), pi, e, ln 2) or mpmath high-precision solves at 80 dps
— independent of the interval Newton kernel under test.
"""

import pytest
from mpmath import mp, mpf

from interval_cert.errors import InputValidationError, StateConflictError
from interval_cert.kernel import KernelConfig, certify_roots
from interval_cert.intervals import scalar

REFERENCE_DPS = 80


def _reference_root(expr: str, lo: float, hi: float) -> mpf:
    """Independent oracle: mpmath.findroot at 80 digits (not the kernel)."""
    with mp.workdps(REFERENCE_DPS):
        f = lambda t: eval(expr, {"x": t, "sin": mp.sin, "cos": mp.cos,
                                  "exp": mp.exp, "log": mp.log, "sqrt": mp.sqrt,
                                  "pi": mp.pi, "e": mp.e})
        return mp.findroot(f, (mpf(lo), mpf(hi)))


def _assert_contains(interval, value: mpf):
    assert scalar(interval.a) <= value <= scalar(interval.b), (
        f"certified interval [{interval.a}, {interval.b}] does not contain {value}")


# ---------------------------------------------------------------------------
# Simple roots
# ---------------------------------------------------------------------------


def test_simple_root_sqrt2_certified_with_evidence():
    result = certify_roots("x^2 - 2", "x", 0, 2, KernelConfig(tol=1e-12))
    assert result.status == "completed"
    assert len(result.certified) == 1
    assert result.undecided == []

    with mp.workdps(REFERENCE_DPS):
        sqrt2 = mp.sqrt(2)
    root = result.certified[0]
    _assert_contains(root.interval, sqrt2)
    assert scalar(root.interval.b) - scalar(root.interval.a) <= mpf("1e-12")

    # The certification evidence must carry the full theorem witness.
    evidence = root.to_dict()["evidence"]
    assert evidence["theorem"] == "interval_newton_contraction"
    assert len(root.witnesses) >= 1
    witness = root.witnesses[0]
    assert mpf(witness.containment_margin_lo) >= 0
    assert mpf(witness.containment_margin_hi) >= 0
    assert "derivative_interval" in witness.to_dict()

    # Numerical approximations exist but are strictly separated from
    # certified enclosures and flagged as not certified.
    assert result.approximations, "expected a numerical approximation"
    approx = result.approximations[0]
    assert approx["certified"] is False
    with mp.workdps(REFERENCE_DPS):
        sqrt2 = mp.sqrt(2)
        approx_value = mpf(approx["value"])
        assert abs(approx_value - sqrt2) < mpf("1e-20")


def test_transcendental_roots_pi_e_ln2():
    with mp.workdps(REFERENCE_DPS):
        cases = [
            ("sin(x)", 3.0, 3.5, mp.pi),
            ("exp(x) - 2", 0.0, 2.0, mp.log(2)),
            ("log(x) - 1", 1.0, 4.0, mp.e),
        ]
        for expr, lo, hi, expected in cases:
            result = certify_roots(expr, "x", lo, hi, KernelConfig())
            assert result.status == "completed", expr
            assert len(result.certified) == 1, expr
            _assert_contains(result.certified[0].interval, expected)


def test_polynomial_root_matches_independent_oracle():
    # Wallis' classic cubic; reference from mpmath.findroot at 80 dps.
    expected = _reference_root("x**3 - 2*x - 5", 2, 3)
    result = certify_roots("x^3 - 2*x - 5", "x", 2, 3, KernelConfig())
    assert result.status == "completed"
    assert len(result.certified) == 1
    _assert_contains(result.certified[0].interval, expected)


# ---------------------------------------------------------------------------
# Multiple roots (distinct simple roots in one search interval)
# ---------------------------------------------------------------------------


def test_multiple_simple_roots_all_certified():
    result = certify_roots("sin(x)", "x", -4, 4, KernelConfig(tol=1e-12))
    assert result.status == "completed"
    assert len(result.certified) == 3
    assert result.undecided == []
    with mp.workdps(REFERENCE_DPS):
        for expected in (-mp.pi, mpf(0), mp.pi):
            assert any(
                scalar(c.interval.a) <= expected <= scalar(c.interval.b)
                for c in result.certified
            ), f"no certified interval contains {mp.nstr(expected, 10)}"


def test_root_exactly_on_bisection_boundary_is_certified():
    # x = 0 is exactly the midpoint of [-1, 1]: naive strict-containment
    # Newton can never certify this; epsilon inflation must.
    result = certify_roots("x", "x", -1, 1, KernelConfig())
    assert result.status == "completed"
    assert len(result.certified) == 1
    _assert_contains(result.certified[0].interval, mpf(0))


# ---------------------------------------------------------------------------
# No root
# ---------------------------------------------------------------------------


def test_no_root_interval_is_fully_eliminated():
    result = certify_roots("x^2 + 1", "x", -2, 2, KernelConfig())
    assert result.status == "completed"
    assert result.certified == []
    assert result.undecided == []
    assert result.stats["eliminated"] >= 1


def test_exp_has_no_root_on_negative_axis():
    result = certify_roots("exp(x)", "x", -10, -1, KernelConfig())
    assert result.status == "completed"
    assert result.certified == []
    assert result.undecided == []


# ---------------------------------------------------------------------------
# Repeated / near roots: undecided must stay undecided
# ---------------------------------------------------------------------------


def test_double_root_cannot_be_certified_unique():
    # (x-1)^2 touches zero without a sign change; f' vanishes at the root,
    # so the uniqueness theorem can never apply. The kernel must say
    # "undecided", not "certified" and not "no root".
    result = certify_roots("(x-1)^2", "x", 0, 2, KernelConfig(tol=1e-12))
    assert result.status == "completed"
    assert result.certified == []
    assert len(result.undecided) >= 1
    assert any(
        scalar(u.interval.a) <= 1 <= scalar(u.interval.b) for u in result.undecided
    )
    assert all(u.reason == "min_width_derivative_straddles_zero"
               for u in result.undecided)


def test_near_roots_separated_with_fine_tolerance():
    # Roots at 1 and 1 + 1e-6: resolvable when tol << 1e-6.
    result = certify_roots("(x-1)*(x-1-1e-6)", "x", 0, 2,
                           KernelConfig(tol=1e-9))
    assert result.status == "completed"
    assert len(result.certified) == 2
    with mp.workdps(REFERENCE_DPS):
        r1, r2 = mpf(1), mpf(1) + mpf("1e-6")
    for expected in (r1, r2):
        assert any(
            scalar(c.interval.a) <= expected <= scalar(c.interval.b)
            for c in result.certified
        ), f"missing certified root near {expected}"


def test_near_roots_undecided_with_coarse_tolerance():
    # Same pair, but tol=1e-3 cannot separate roots 1e-6 apart: the kernel
    # must report undecided rather than merge them into one "root".
    result = certify_roots("(x-1)*(x-1-1e-6)", "x", 0, 2,
                           KernelConfig(tol=1e-3))
    assert result.status == "completed"
    assert result.certified == []
    assert len(result.undecided) >= 1
    assert any(
        scalar(u.interval.a) <= 1 <= scalar(u.interval.b) for u in result.undecided
    )


# ---------------------------------------------------------------------------
# Failure categories
# ---------------------------------------------------------------------------


def test_domain_error_preserves_evaluation_location():
    result = certify_roots("log(x)", "x", -1, 1, KernelConfig())
    assert result.status == "domain_error"
    assert result.error["category"] == "domain_error"
    assert result.error["details"]["location"] == "root"
    assert "interval" in result.error["details"]


def test_resource_exhaustion_is_distinguishable():
    result = certify_roots("sin(x)", "x", -100, 100, KernelConfig(max_steps=5))
    assert result.status == "resource_exhausted"
    assert len(result.undecided) >= 1
    assert all(u.reason == "resource_exhausted" for u in result.undecided)


def test_invalid_interval_is_input_error():
    with pytest.raises(InputValidationError) as excinfo:
        certify_roots("x", "x", 2, 2, KernelConfig())
    assert excinfo.value.category == "input_error"


def test_tolerance_beyond_precision_is_state_conflict():
    with pytest.raises(StateConflictError) as excinfo:
        certify_roots("x", "x", 0, 1, KernelConfig(tol=1e-60, dps=30))
    assert excinfo.value.category == "state_conflict"


# ---------------------------------------------------------------------------
# Trace / replayability
# ---------------------------------------------------------------------------


def test_trace_records_run_id_actions_and_reasons():
    result = certify_roots("x^2 - 2", "x", 0, 2, KernelConfig())
    assert result.trace, "trace must not be empty"
    for record in result.trace:
        assert record["run_id"] == result.run_id
        assert record["action"] in {
            "eliminate", "bisect", "contract", "certify", "undecided"}
        assert record["reason"]
        assert record["interval"].startswith("[")
    actions = {r["action"] for r in result.trace}
    assert "certify" in actions  # this run must have certified something


def test_run_ids_are_unique():
    r1 = certify_roots("x", "x", 0, 1, KernelConfig())
    r2 = certify_roots("x", "x", 0, 1, KernelConfig())
    assert r1.run_id != r2.run_id
