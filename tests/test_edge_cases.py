"""Edge cases: zero matrices, scalar systems, wide/tall shapes, signs."""
from __future__ import annotations

from rationalsvc import numeric_input, runner


def solve(payload):
    return runner.solve_system(
        numeric_input.parse_request(payload), runner.RunLogger(None),
        run_id="edge", include_float_diagnosis=False,
    )


def test_all_zero_homogeneous_is_infinite_full_nullspace():
    res = solve({"A": [[0, 0], [0, 0]], "b": [0, 0]})
    assert res["rank"]["rank_A"] == 0
    assert res["rank"]["nullity"] == 2
    sol = res["solutions"][0]
    assert sol["classification"] == "infinite"
    assert sol["particular"] == ["0", "0"]
    assert sol["parametric_form"]["free_columns"] == [0, 1]
    # two standard free-variable null vectors
    assert sol["parametric_form"]["nullspace_basis"] == [
        ["1", "0"], ["0", "1"]
    ]


def test_all_zero_with_nonzero_rhs_is_inconsistent_with_witness():
    res = solve({"A": [[0, 0], [0, 0]], "b": [1, 0]})
    sol = res["solutions"][0]
    assert sol["classification"] == "inconsistent"
    w = sol["contradiction_witness"]
    assert w["y_dot_b"] == "1"
    assert res["verification"]["witness_y_dot_A"][0] == ["0", "0"]


def test_one_by_one_fraction_solution():
    res = solve({"A": [["6"]], "b": ["3/2"]})
    assert res["solutions"][0]["particular"] == ["1/4"]
    assert res["rank"]["determinant"] == "6"


def test_wide_underdetermined_system_has_two_free_columns():
    res = solve({
        "A": [[1, 0, 2, 1], [0, 1, -1, 2]],
        "b": [3, 1],
    })
    assert res["rank"]["rank_A"] == 2
    assert res["rank"]["nullity"] == 2
    sol = res["solutions"][0]
    assert sol["classification"] == "infinite"
    assert sol["particular"] == ["3", "1", "0", "0"]
    assert sol["parametric_form"]["free_columns"] == [2, 3]
    # null vectors substitute exactly to zero
    assert res["verification"]["nullspace_residuals"][0] == [
        ["0", "0"], ["0", "0"]
    ]


def test_tall_overdetermined_consistent_system():
    res = solve({"A": [[1, 0], [0, 1], [1, 1]], "b": [2, 3, 5]})
    assert res["rank"]["rank_A"] == 2
    assert res["solutions"][0]["classification"] == "unique"
    assert res["solutions"][0]["particular"] == ["2", "3"]


def test_determinant_sign_under_an_odd_swap_is_negative():
    # [[0,0,1],[0,2,0],[3,0,0]] requires one swap; det = -6 (sign flipped).
    res = solve({"A": [[0, 0, 1], [0, 2, 0], [3, 0, 0]], "b": [1, 2, 3]})
    assert res["solutions"][0]["particular"] == ["1", "1", "1"]
    assert res["rank"]["row_swaps"] == [[2, 0]]
    assert res["rank"]["determinant"] == "-6"


def test_exact_blocks_contain_only_canonical_fraction_strings():
    import re

    res = solve({"A": [[1, 2], [3, 4]], "b": [5, 6]})
    exact = {k: v for k, v in res.items() if k != "float_diagnosis"}
    strings = []

    def walk(x):
        if isinstance(x, str):
            strings.append(x)
        elif isinstance(x, dict):
            for v in x.values():
                walk(v)
        elif isinstance(x, list):
            for v in x:
                walk(v)

    walk(exact)
    canon = re.compile(r"-?\d+(/\d+)?$")
    scalars = [
        s for s in strings
        if s and all(ch.isdigit() or ch in "-/" for ch in s)
    ]
    assert scalars, "expected exact scalar strings in the result"
    for s in scalars:
        assert canon.match(s), f"non-canonical/exponent float in exact block: {s}"
