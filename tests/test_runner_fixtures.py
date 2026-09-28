"""Runner/evidence tests driven by the concrete synthetic fixtures.

These assert exact values, exact classifications and failure *categories* -
not merely that endpoints can be called.  Expected values in fixtures.json
were produced by hand and cross-checked against tests/oracle.py.
"""
from __future__ import annotations

from fractions import Fraction

import pytest

from rationalsvc import numeric_input, runner
from rationalsvc.errors import BudgetExhausted, ErrorCode


def solve(case_dict, log=None, float_diag=True):
    parsed = numeric_input.parse_request(case_dict)
    return runner.solve_system(
        parsed, runner.RunLogger(None),
        run_id="unit-" + case_dict.get("_id", "x"),
        include_float_diagnosis=float_diag,
    )


def test_big_common_factor_exact_solution_and_determinant(case):
    c = case("big_common_factor")
    res = solve(c)
    assert res["exact"] is True
    assert res["rank"]["rank_A"] == 2
    sol = res["solutions"][0]
    assert sol["classification"] == "unique"
    assert sol["particular"] == ["2", "1"]
    assert res["rank"]["determinant"] == "-480"
    assert res["rank"]["content_divisors_removed"] == ["6", "10"]
    # no floats anywhere in the exact blocks
    assert res["verification"]["all_residuals_zero"] is True


def test_huge_common_factor_stays_within_tiny_growth(case):
    c = case("huge_common_factor")
    res = solve(c)
    sol = res["solutions"][0]
    assert sol["particular"] == ["2", "1"]
    assert res["rank"]["determinant"] == "-480" + "0" * 36
    # growth control: the biggest Bareiss integer is only a handful of digits
    assert res["budget"]["observed_max_digits"] <= 6
    assert res["budget"]["within_budget"] is True


def test_rank_deficient_infinite_parametric_solution(case):
    res = solve(case("rank_deficient_infinite"))
    assert res["rank"]["rank_A"] == 2
    assert res["rank"]["nullity"] == 1
    assert res["rank"]["free_columns"] == [2]
    sol = res["solutions"][0]
    assert sol["classification"] == "infinite"
    assert sol["particular"] == ["1", "0", "0"]
    pf = sol["parametric_form"]
    assert pf["free_columns"] == [2]
    assert pf["nullspace_basis"] == [["-1", "0", "1"]]
    assert pf["parameters"] == ["t_0"]
    # residual of the particular solution is exactly zero
    assert res["verification"]["particular_residuals"][0] == ["0", "0", "0"]
    # null vector residual is exactly zero
    assert res["verification"]["nullspace_residuals"][0][0] == ["0", "0", "0"]


def test_inconsistent_systems_return_left_null_evidence(case):
    res = solve(case("rank_deficient_inconsistent"))
    assert res["rank"]["rank_A"] == 2
    sol = res["solutions"][0]
    assert sol["classification"] == "inconsistent"
    w = sol["contradiction_witness"]
    assert w["y"] == ["1", "-1", "0"]
    assert w["y_dot_b"] == "-2"
    # independently re-derived evidence inside the response
    assert res["verification"]["witness_y_dot_A"][0] == ["0", "0", "0"]
    assert res["verification"]["witness_y_dot_b"][0] == "-2"


def test_near_float_indistinguishable_is_exact_but_confuses_float64(case):
    res = solve(case("near_float_indistinguishable"))
    assert res["rank"]["rank_A"] == 3
    sol = res["solutions"][0]
    assert sol["classification"] == "unique"
    assert sol["particular"] == ["1", "1", "1"]
    assert res["rank"]["determinant"] == "-3/1000000000000000"
    diag = res["float_diagnosis"]
    assert diag["labelled_approximate"] is True
    assert diag["rank_float64"] == 2
    assert diag["rank_exact"] == 3
    assert diag["double_confuses_exact_structure"] is True
    # mpmath at 80 digits resolves it
    assert diag["mpmath"]["resolved_at_80dps"] is True


def test_swap_preserves_sign(case):
    res = solve(case("swap_preserves_sign"))
    assert res["rank"]["row_swaps"] == [[1, 0]]
    assert res["rank"]["determinant"] == "-2"
    assert res["solutions"][0]["particular"] == ["-5", "7"]


def test_fraction_inputs_are_exact_end_to_end(case):
    res = solve(case("fraction_inputs_exact"))
    assert res["solutions"][0]["particular"] == ["5/2", "-1"]
    assert res["rank"]["determinant"] == "1/4"


def test_multi_rhs_mixed_consistency_per_right_hand_side(case):
    res = solve(case("multi_rhs_mixed"))
    s0, s1 = res["solutions"]
    assert s0["classification"] == "infinite"
    assert s0["particular"] == ["1", "0", "0"]
    assert s1["classification"] == "inconsistent"
    assert s1["contradiction_witness"]["y"] == ["2", "-1", "0"]
    assert s1["contradiction_witness"]["y_dot_b"] == "-1"


def test_budget_exhaustion_returns_diagnosable_progress(case):
    parsed = numeric_input.parse_request(case("budget_too_tight"))
    with pytest.raises(BudgetExhausted) as ei:
        runner.solve_system(
            parsed, runner.RunLogger(None), run_id="budget-unit",
            include_float_diagnosis=False,
        )
    err = ei.value
    assert err.category is ErrorCode.BUDGET_EXHAUSTED
    assert err.http_status == 422
    p = err.details["progress"]
    # replay/diagnosis contract
    assert p["phase"] in {"forward", "normalize"}
    assert isinstance(p["matrix"], list) and p["matrix"]
    assert "pivots" in p and "swaps" in p
    assert "reason" in p and "budget" in p["reason"]
    assert "exact" in p["reason"]  # explicitly: no float fallback
    assert p["observed_max_digits"] >= err.details["limit_digits"]


def test_no_silent_float_fallback_under_tight_budget(case, tmp_path):
    """Even when the budget is exhausted the run never converts to float."""
    log = tmp_path / "log.jsonl"
    parsed = numeric_input.parse_request(case("budget_too_tight"))
    with pytest.raises(BudgetExhausted):
        runner.solve_system(
            parsed, runner.RunLogger(str(log)), run_id="nofloat",
            include_float_diagnosis=False,
        )
    import json
    events = [json.loads(line) for line in log.read_text().splitlines()]
    kinds = [e["event"] for e in events]
    assert "budget_exhausted" in kinds
    # every checkpoint matrix entry is an exact integer literal
    for e in events:
        if e["event"] == "checkpoint":
            assert "max_decimal_digits" in e
    exhausted = next(e for e in events if e["event"] == "budget_exhausted")
    for row in exhausted["progress"]["matrix"]:
        for v in row:
            assert "." not in v and "e" not in v.lower()


def test_run_log_records_run_id_and_key_intermediate_state(case, tmp_path):
    log = tmp_path / "log.jsonl"
    parsed = numeric_input.parse_request(case("big_common_factor"))
    rid = "run-log-123"
    res = runner.solve_system(
        parsed, runner.RunLogger(str(log)), run_id=rid,
        include_float_diagnosis=False,
    )
    assert res["run_id"] == rid
    import json
    events = [json.loads(line) for line in log.read_text().splitlines()]
    ids = {e["run_id"] for e in events}
    assert ids == {rid}
    kinds = [e["event"] for e in events]
    assert kinds[0] == "started"
    assert "denominators_cleared" in kinds
    assert "checkpoint" in kinds
    assert kinds[-1] == "completed"
    # exact input is retained for replay
    started = next(e for e in events if e["event"] == "started")
    assert started["A"] == [["12", "18"], ["20", "-10"]]
