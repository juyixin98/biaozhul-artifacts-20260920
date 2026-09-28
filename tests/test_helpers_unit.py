"""Additional unit tests covering pure helpers and the MC permutation path."""
from __future__ import annotations

import pytest

from stratblock.config import Config
from stratblock.contract import TailPolicy, build_study_config, prefix_is_balanced
from stratblock.estimator import GroupData, permutation_test
from stratblock.rng import expected_tail_report


@pytest.mark.unit
def test_prefix_is_balanced_symmetric_set() -> None:
    ratio = (1, 1)
    assert prefix_is_balanced((1, 0), 1, ratio)
    assert prefix_is_balanced((1, 1), 2, ratio)
    assert prefix_is_balanced((2, 1), 3, ratio)
    assert prefix_is_balanced((1, 2), 3, ratio)  # opposite tie-break also fine
    assert not prefix_is_balanced((2, 0), 2, ratio)
    assert not prefix_is_balanced((0, 2), 2, ratio)
    # count vector must actually sum to n
    assert not prefix_is_balanced((1, 1), 3, ratio)


@pytest.mark.unit
def test_expected_tail_report_both_policies() -> None:
    perm = build_study_config("p", ["C", "T"], ["s"], [4], [1, 1], TailPolicy.PERMUTED)
    rep = expected_tail_report(perm, (0, 2), 2)
    assert rep["balanced"] is False and rep["flag"] == "tail_imbalance"
    assert rep["balance_criterion"] == "feasible_apportionment_set"
    rep_ok = expected_tail_report(perm, (1, 2), 3)
    assert rep_ok["balanced"] is True

    bal = build_study_config("b", ["C", "T1", "T2"], ["s"], [3, 6], [1, 1, 1],
                             TailPolicy.BALANCED_PREFIX)
    rep_b = expected_tail_report(bal, (1, 1, 0), 2)
    assert rep_b["balanced"] is True
    assert rep_b["balance_criterion"] == "deterministic_hamilton_target"


@pytest.mark.unit
def test_permutation_test_uses_monte_carlo_for_large_sample() -> None:
    rng_data = [float(i) for i in range(20)]
    a = GroupData("A", rng_data[:10])
    b = GroupData("B", rng_data[10:])
    rep = permutation_test(a, b, reps=200, seed=3)
    assert rep["method"] == "monte_carlo"
    assert rep["n_permutations"] == 201
    assert 0.0 <= rep["p_value"] <= 1.0
    assert "seed=3" in rep["rng_note"]


@pytest.mark.unit
def test_config_token_roles_parsing_and_rejects_bad_entry() -> None:
    cfg = Config(db_path=":memory:", master_seed=1, api_tokens="a:enroller|b:auditor")
    assert cfg.token_roles() == {"a": "enroller", "b": "auditor"}
    bad = Config(db_path=":memory:", master_seed=1, api_tokens="no-colon")
    with pytest.raises(ValueError):
        bad.token_roles()
