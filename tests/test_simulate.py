"""Coverage-simulation tests: fixed seed, reproducibility, calibration.

These are the acceptance criteria: under the null (no true difference),
the empirical coverage of the 95% Welch interval must be statistically
consistent with 0.95, across balanced, imbalanced, and missing-data
scenarios.
"""

import pytest

from abtest.simulate import estimate_coverage, run_standard_scenarios

# Monte Carlo standard error for p=0.95 with n=2000 simulations is
# sqrt(0.95*0.05/2000) ~= 0.0049. A 4-sigma band gives ~0.02 tolerance.
COVERAGE_TOLERANCE = 0.02


def test_coverage_is_reproducible_with_fixed_seed():
    r1 = estimate_coverage(n_simulations=200, seed=42)
    r2 = estimate_coverage(n_simulations=200, seed=42)
    assert r1 == r2


def test_different_seed_gives_different_result():
    r1 = estimate_coverage(n_simulations=200, seed=1)
    r2 = estimate_coverage(n_simulations=200, seed=2)
    assert r1.coverage != r2.coverage or r1.mean_ci_width != r2.mean_ci_width


@pytest.mark.parametrize(
    "kwargs",
    [
        {"n_control": 500, "n_treatment": 500},
        {"n_control": 50, "n_treatment": 5000},
        {"n_control": 500, "n_treatment": 500, "missing_rate": 0.2,
         "missing_strategy": "drop"},
    ],
    ids=["balanced", "imbalanced_1_to_100", "missing_20pct_drop"],
)
def test_coverage_calibrated_under_null(kwargs):
    res = estimate_coverage(
        n_simulations=2000, confidence_level=0.95, seed=20260922, **kwargs
    )
    assert res.coverage == pytest.approx(0.95, abs=COVERAGE_TOLERANCE)


def test_standard_scenarios_run_and_report():
    results = run_standard_scenarios(n_simulations=200, seed=20260922)
    assert len(results) == 4
    for r in results:
        assert 0.0 <= r.coverage <= 1.0
        assert r.mean_ci_width > 0.0
        assert r.seed == 20260922


def test_invalid_arguments_raise():
    with pytest.raises(ValueError):
        estimate_coverage(n_simulations=0)
    with pytest.raises(ValueError):
        estimate_coverage(missing_rate=1.0)
