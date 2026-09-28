"""Certification kernel tests: concrete outcomes, not interface smoke tests.

Each scenario asserts the exact classification (unique / none / undecided),
the theorem that proves it, and that the certified enclosure contains a root
computed by an INDEPENDENT oracle (tests/conftest.py), never by the SUT.
"""

from __future__ import annotations

from decimal import Decimal

import pytest

from tests.conftest import (
    assert_enclosure_contains,
    verify_monotone_evidence,
    verify_newton_uniqueness_evidence,
)


def _root_payloads(payload: dict) -> list[dict]:
    return payload["certified_roots"]


def _undecided_payloads(payload: dict) -> list[dict]:
    return payload["undecided_regions"]


# ---------------------------------------------------------------------------
# Simple roots
# ---------------------------------------------------------------------------
@pytest.mark.kernel
def test_simple_root_sqrt2_newton_uniqueness(run_payload, oracle) -> None:
    payload = run_payload("x^2 - 2", "1", "2")
    assert payload["status"] == "certified"
    roots = _root_payloads(payload)
    assert len(roots) == 1
    root = roots[0]
    assert root["certification"]["theorem"] == "interval_newton_uniqueness"
    assert root["certification"]["status"] == "certified"
    # Independently computed ground truth is enclosed.
    assert_enclosure_contains(root["enclosure"], oracle.SQRT2)
    # Enclosure is genuinely tight (certification actually converged).
    width = Decimal(root["enclosure"]["upper"]) - Decimal(
        root["enclosure"]["lower"]
    )
    assert width < Decimal("1e-30")
    # Residual enclosure contains zero.
    res = root["residual_range_enclosure"]
    assert Decimal(res["lower"]) <= 0 <= Decimal(res["upper"])
    # The theorem hypotheses are independently re-checked.
    verify_newton_uniqueness_evidence(root["evidence"])


@pytest.mark.kernel
def test_simple_root_log2_via_exp(run_payload, oracle) -> None:
    payload = run_payload("exp(x) - 2", "0", "1")
    roots = _root_payloads(payload)
    assert len(roots) == 1
    assert_enclosure_contains(roots[0]["enclosure"], oracle.LN2)
    verify_newton_uniqueness_evidence(roots[0]["evidence"])


@pytest.mark.kernel
def test_simple_root_transcendental_sin(run_payload) -> None:
    payload = run_payload("sin(x)", "-0.5", "0.5")
    roots = _root_payloads(payload)
    assert len(roots) == 1
    enc = roots[0]["enclosure"]
    assert Decimal(enc["lower"]) <= Decimal(0) <= Decimal(enc["upper"])


@pytest.mark.kernel
def test_multiple_distinct_roots_all_certified(run_payload) -> None:
    # sin(x) over [0, 2pi+]: roots at 0, pi, 2pi.
    payload = run_payload("sin(x)", "0", "6.2832")
    roots = _root_payloads(payload)
    assert len(roots) == 3
    # Independently sorted; check pi using a decimal constant.
    mids = sorted(Decimal(r["midpoint"]) for r in roots)
    assert abs(mids[0]) < Decimal("1e-18")
    assert abs(mids[1] - Decimal("3.14159265358979323846264338327950")) < Decimal(
        "1e-18"
    )
    assert abs(mids[2] - Decimal("6.283185307179586476925286766559")) < Decimal(
        "1e-18"
    )


@pytest.mark.kernel
def test_three_close_roots_of_cubic(run_payload) -> None:
    # x^3 - 2x + 1e-6 has three distinct real roots, two very close to
    # +/-sqrt(2) and one near 0. All must be isolated.
    payload = run_payload("x^3 - 2*x + 0.000001", "-2", "2")
    roots = _root_payloads(payload)
    assert len(roots) == 3
    mids = sorted(Decimal(r["midpoint"]) for r in roots)
    assert mids[0] < Decimal("-1.4")
    assert abs(mids[1] - Decimal("0.0000005")) < Decimal("1e-8")
    assert mids[2] > Decimal("1.4")
    # Enclosures are pairwise disjoint (roots genuinely isolated).
    for a, b in zip(roots, roots[1:]):
        assert Decimal(a["enclosure"]["upper"]) <= Decimal(
            b["enclosure"]["lower"]
        ) or Decimal(b["enclosure"]["upper"]) <= Decimal(
            a["enclosure"]["lower"]
        )


# ---------------------------------------------------------------------------
# No-root cases (must NOT be inferred merely from "0 in f(X)")
# ---------------------------------------------------------------------------
@pytest.mark.kernel
def test_no_root_positive_definite(run_payload) -> None:
    payload = run_payload("x^2 + 1", "-2", "2")
    assert payload["status"] == "root_free"
    assert _root_payloads(payload) == []
    assert _undecided_payloads(payload) == []
    assert payload["summary"]["excluded_leaves"] >= 1


@pytest.mark.kernel
def test_no_root_constant_nonzero(run_payload) -> None:
    payload = run_payload("1", "0", "1")
    assert payload["status"] == "root_free"


@pytest.mark.kernel
def test_interval_crossing_zero_but_no_root(run_payload) -> None:
    # f(x) = x^2 - x on [0.1, 0.4]: the ENCLOSURE of f crosses zero (its
    # natural range evaluation straddles 0) yet no root lives in the
    # interval. This directly checks behaviour contract #1.
    payload = run_payload("x^2 - x", "0.1", "0.4")
    assert payload["status"] == "root_free"
    assert _root_payloads(payload) == []


# ---------------------------------------------------------------------------
# Repeated / even-multiplicity roots -> undecided, never mis-certified
# ---------------------------------------------------------------------------
@pytest.mark.kernel
def test_double_root_is_undecided_not_certified(run_payload) -> None:
    payload = run_payload("x^2", "-1", "1")
    assert _root_payloads(payload) == []
    undecided = _undecided_payloads(payload)
    assert len(undecided) >= 1
    assert all(
        u["reason"] == "tangent_or_even_multiplicity" for u in undecided
    )
    # Undecided region must actually contain the true root 0.
    for u in undecided:
        lo = Decimal(u["interval"]["lower"])
        hi = Decimal(u["interval"]["upper"])
        assert lo <= 0 <= hi


@pytest.mark.kernel
def test_double_root_interior_is_undecided(run_payload) -> None:
    payload = run_payload("(x-1)^2", "0", "2")
    assert _root_payloads(payload) == []
    undecided = _undecided_payloads(payload)
    assert len(undecided) >= 1
    for u in undecided:
        lo = Decimal(u["interval"]["lower"])
        hi = Decimal(u["interval"]["upper"])
        assert lo <= Decimal(1) <= hi


@pytest.mark.kernel
def test_quadruple_root_is_undecided(run_payload) -> None:
    payload = run_payload("(x+1)^4", "-2", "0")
    assert _root_payloads(payload) == []
    assert len(_undecided_payloads(payload)) >= 1


@pytest.mark.kernel
def test_identically_zero_is_single_undecided_region(run_payload) -> None:
    payload = run_payload("x - x", "-1", "1")
    assert _root_payloads(payload) == []
    undecided = _undecided_payloads(payload)
    assert len(undecided) == 1
    assert undecided[0]["reason"] == (
        "function_identically_zero_not_isolable"
    )


# ---------------------------------------------------------------------------
# Very-near-root samples
# ---------------------------------------------------------------------------
@pytest.mark.kernel
def test_very_near_root_boundary_still_certified(run_payload, oracle) -> None:
    # Upper bound sits ~4.6e-17 above sqrt(2) but is not exactly a root; the
    # root is interior to [0, bound], and must still be isolated and tight.
    bound = "1.4142135623730951"
    payload = run_payload("x^2 - 2", "0", bound)
    roots = _root_payloads(payload)
    assert len(roots) == 1
    root = roots[0]
    assert_enclosure_contains(root["enclosure"], oracle.SQRT2)
    # Despite the near boundary, refinement tightens well below 1e-15.
    width = Decimal(root["enclosure"]["upper"]) - Decimal(
        root["enclosure"]["lower"]
    )
    assert width < Decimal("1e-15")


@pytest.mark.kernel
def test_root_exactly_on_boundary_certified(run_payload) -> None:
    # f(x)=x on [0,1]: root exactly on the boundary; Newton uniqueness cannot
    # apply (not strictly interior), monotone IVT must still prove it.
    payload = run_payload("x", "0", "1")
    roots = _root_payloads(payload)
    assert len(roots) == 1
    root = roots[0]
    assert root["certification"]["theorem"] == "monotone_intermediate_value"
    enc = root["enclosure"]
    assert Decimal(enc["lower"]) <= 0 <= Decimal(enc["upper"])
    verify_monotone_evidence(root["evidence"])


@pytest.mark.kernel
def test_decreasing_root_on_each_boundary(run_payload) -> None:
    # f(x) = 1 - x is strictly DECREASING with root 1 on the right boundary.
    payload = run_payload("1 - x", "0", "1")
    roots = _root_payloads(payload)
    assert len(roots) == 1
    enc = roots[0]["enclosure"]
    assert Decimal(enc["lower"]) <= Decimal(1) <= Decimal(enc["upper"])
    assert roots[0]["evidence"]["monotonicity"] == "decreasing"
    verify_monotone_evidence(roots[0]["evidence"])
    # And on the left boundary: 1 - x on [1, 2].
    payload = run_payload("1 - x", "1", "2")
    roots = _root_payloads(payload)
    assert len(roots) == 1
    enc = roots[0]["enclosure"]
    assert Decimal(enc["lower"]) <= Decimal(1) <= Decimal(enc["upper"])
    verify_monotone_evidence(roots[0]["evidence"])


@pytest.mark.kernel
def test_decreasing_no_root_above_and_below(run_payload) -> None:
    # Decreasing 1 - x: on [2,3] it is strictly negative (root lies left);
    # on [-1,0] strictly positive (root lies right).
    assert run_payload("1 - x", "2", "3")["status"] == "root_free"
    assert run_payload("1 - x", "-1", "0")["status"] == "root_free"


@pytest.mark.kernel
def test_nonlinear_decreasing_boundary_root_cos(run_payload, oracle) -> None:
    # cos is strictly decreasing on [0.01, pi/2]; root pi/2 on right boundary.
    half_pi = oracle.PI / 2
    payload = run_payload("cos(x)", "0.01", str(half_pi))
    roots = _root_payloads(payload)
    assert len(roots) == 1
    assert roots[0]["evidence"]["monotonicity"] == "decreasing"
    assert_enclosure_contains(roots[0]["enclosure"], half_pi)
    verify_monotone_evidence(roots[0]["evidence"])


# ---------------------------------------------------------------------------
# Derivative crossing zero -> conservative subdivision (contract #2)
# ---------------------------------------------------------------------------
@pytest.mark.kernel
def test_derivative_crossing_zero_drives_bisection(run_payload) -> None:
    payload = run_payload("x^2 - 2", "0", "2")
    summary = payload["summary"]
    # f'=2x crosses zero inside [0,2]; progress must come via bisection.
    assert summary["bisections"] >= 1
    assert len(_root_payloads(payload)) == 1


# ---------------------------------------------------------------------------
# Domain errors preserve position (contract #4)
# ---------------------------------------------------------------------------
@pytest.mark.kernel
def test_domain_error_preserves_source_position() -> None:
    from app.core.config import CertConfig
    from app.core.errors import DomainEvaluationError
    from app.core.tracer import Tracer
    from app.services import certify_service

    expr = "sqrt(x - 1)"
    with pytest.raises(DomainEvaluationError) as exc_info:
        with Tracer.create(None) as tracer:
            certify_service.certify_expression(
                expression=expr,
                lower="0",
                upper="2",
                config=CertConfig(),
                tracer=tracer,
                include_approximation=False,
            )
    err = exc_info.value
    assert err.code.value == "DOMAIN_ERROR"
    assert err.category.value == "domain"
    # Position points at the offending argument span "x - 1" [5,10],
    # preserving the location of the range error inside the expression.
    assert err.position.start == 5
    assert err.position.end == 10
    assert expr[err.position.start:err.position.end] == "x - 1"


@pytest.mark.kernel
def test_log_of_interval_reaching_zero_is_domain_error() -> None:
    from app.core.config import CertConfig
    from app.core.errors import DomainEvaluationError
    from app.core.tracer import Tracer
    from app.services import certify_service

    with pytest.raises(DomainEvaluationError):
        with Tracer.create(None) as tracer:
            certify_service.certify_expression(
                expression="log(x)",
                lower="0",
                upper="1",
                config=CertConfig(),
                tracer=tracer,
                include_approximation=False,
            )
