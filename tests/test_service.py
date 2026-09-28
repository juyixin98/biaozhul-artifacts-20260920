"""Service-level tests: accept / reject / undetermined classification.

These assert specific failure codes and statuses, the zero-polynomial special
case, request-id propagation, and that sensitive coefficient values never
appear in diagnostics.
"""
from __future__ import annotations

from app.errors import FailureCode
from app.service import isolate_coefficients


def test_zero_polynomial_has_special_status_and_no_root_list(settings):
    response = isolate_coefficients([0, 0, 0], settings)
    assert response.status == "zero_polynomial"
    assert response.is_zero_polynomial is True
    assert response.roots == []
    assert response.degree is None
    assert response.failure is None
    assert "every point is a root" in response.diagnostics["reason"]


def test_nonzero_constant_is_ok_with_empty_roots(settings):
    response = isolate_coefficients([5], settings)
    assert response.status == "ok"
    assert response.degree == 0
    assert response.roots == []


def test_simple_case_accepted_with_evidence(settings):
    response = isolate_coefficients([-6, 11, -6, 1], settings,
                                    target_width="1/100000")
    assert response.status == "ok"
    assert len(response.roots) == 3
    assert response.evidence is not None
    assert response.evidence["accepted"] is True
    assert response.evidence["cross_check"]["distinct_real_roots"] == 3


def test_no_real_roots_accepted_empty(settings):
    coeffs = [1, 0, 1, 0, 1, 0, 1]
    response = isolate_coefficients(coeffs, settings)
    assert response.status == "ok"
    assert response.roots == []
    assert response.evidence["cross_check"]["distinct_real_roots"] == 0


def test_invalid_coefficient_token_is_rejected(settings):
    response = isolate_coefficients(["not-a-number"], settings)
    assert response.status == "rejected"
    assert response.failure["code"] == FailureCode.INVALID_COEFFICIENTS.value
    assert response.diagnostics["decision"] == "rejected"


def test_empty_array_is_rejected_as_empty_polynomial(settings):
    response = isolate_coefficients([], settings)
    assert response.status == "rejected"
    assert response.failure["code"] == FailureCode.EMPTY_POLYNOMIAL.value


def test_degree_budget_rejection(settings):
    import dataclasses
    from app.settings import KernelSettings
    # Degree 10 with max_degree 4, coefficient cap kept permissive so the
    # degree limit (not the coefficient-count limit) is what fires.
    low_degree = KernelSettings(
        precision_dps=60, max_degree=4, max_coefficients=201,
        max_bisections=20000, max_sturm_length=202, max_coeff_bits=20000,
    )
    narrowed = dataclasses.replace(settings, kernel=low_degree)
    response = isolate_coefficients([1] * 11, narrowed)  # degree 10 > 4
    assert response.status == "rejected"
    assert response.failure["code"] == FailureCode.DEGREE_EXCEEDED.value
    assert response.failure["state"]["limit"] == 4


def test_bad_interval_ordering_rejected(settings):
    response = isolate_coefficients(
        [-6, 11, -6, 1], settings,
        interval_lo="5", interval_hi="1",
    )
    assert response.status == "rejected"
    assert response.failure["code"] == FailureCode.INVALID_INTERVAL.value


def test_half_interval_rejected(settings):
    response = isolate_coefficients(
        [-6, 11, -6, 1], settings, interval_lo="0", interval_hi=None,
    )
    assert response.status == "rejected"
    assert response.failure["code"] == FailureCode.INVALID_INTERVAL.value


def test_bad_target_width_rejected(settings):
    response = isolate_coefficients(
        [-6, 11, -6, 1], settings, target_width="not-a-number",
    )
    assert response.status == "rejected"
    assert response.failure["code"] == FailureCode.INVALID_PRECISION.value


def test_non_positive_width_rejected(settings):
    response = isolate_coefficients(
        [-6, 11, -6, 1], settings, target_width="0",
    )
    assert response.status == "rejected"
    assert response.failure["code"] == FailureCode.INVALID_PRECISION.value


def test_bisection_budget_returns_undetermined(settings):
    import dataclasses
    from app.settings import KernelSettings
    tight = KernelSettings(
        precision_dps=60, max_degree=200, max_coefficients=201,
        max_bisections=4, max_sturm_length=202, max_coeff_bits=20000,
    )
    narrowed = dataclasses.replace(settings, kernel=tight)
    response = isolate_coefficients(
        [-6, 11, -6, 1], narrowed, target_width="1/1000000000000000",
    )
    assert response.status == "undetermined"
    assert response.failure["code"] == \
        FailureCode.BISECTION_BUDGET_EXCEEDED.value
    assert response.failure["state"]["bisection_limit"] == 4
    assert response.diagnostics["decision"] == "undetermined"


def test_request_id_is_echoed_and_generated_when_absent(settings):
    response = isolate_coefficients(
        [-6, 11, -6, 1], settings, request_id="corr-123",
    )
    assert response.request_id == "corr-123"
    assert response.diagnostics["request_id"] == "corr-123"

    auto = isolate_coefficients([-6, 11, -6, 1], settings)
    assert auto.request_id and auto.request_id != "corr-123"


def test_diagnostics_never_contain_coefficient_values(settings):
    secretish = "-123456789/987654321"
    response = isolate_coefficients([secretish, 0, 1], settings,
                                    target_width="1/1000000000000000000")
    blob = repr(response.diagnostics) + repr(response.failure)
    assert "123456789" not in blob
    assert response.status in ("ok", "undetermined")


def test_proof_payload_carries_variation_evidence(settings):
    response = isolate_coefficients([-6, 11, -6, 1], settings,
                                    target_width="1/100000")
    for root in response.roots:
        proof = root["proof"]
        assert proof["method"] == "sturm"
        assert proof["chain_length"] == 4
        if not root["exact"]:
            assert proof["root_count_in_cell"] == 1
            assert proof["variations_left"] - proof["variations_right"] == 1


def test_multiplicity_summary_present_for_repeated_case(settings):
    # (x-1)^2 (x-2) (x-3)^3
    coeffs = [54, -189, 261, -182, 68, -13, 1]
    response = isolate_coefficients(coeffs, settings,
                                    target_width="1/1000000")
    assert response.status == "ok"
    mults = {m["multiplicity"]: m for m in response.multiplicities}
    assert mults[1]["distinct_real_roots"] == 1
    assert mults[2]["distinct_real_roots"] == 1
    assert mults[3]["distinct_real_roots"] == 1
    multiplicities = {r["multiplicity"] for r in response.roots}
    assert multiplicities == {1, 2, 3}
