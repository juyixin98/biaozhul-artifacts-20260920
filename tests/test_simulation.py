import pytest

from abseq.simulation import coverage_simulation, peeking_type1_simulation


class TestCoverageSimulation:
    def test_coverage_near_nominal_under_null(self):
        result = coverage_simulation(
            n_control=200, n_treatment=200, effect=0.0, reps=1000, seed=1
        )
        # Monte Carlo SE at 95% nominal with 1000 reps is ~0.7pp.
        assert result["empirical_coverage"] == pytest.approx(0.95, abs=0.03)

    def test_imbalanced_groups_still_cover(self):
        result = coverage_simulation(
            n_control=30, n_treatment=3000, effect=0.0, reps=1000, seed=2
        )
        assert result["empirical_coverage"] == pytest.approx(0.95, abs=0.04)

    def test_missing_drop_strategy_covers(self):
        result = coverage_simulation(
            n_control=200, n_treatment=200, effect=0.0, reps=1000,
            missing_strategy="drop", missing_rate=0.2, seed=3,
        )
        assert result["empirical_coverage"] == pytest.approx(0.95, abs=0.03)

    def test_fixed_seed_is_reproducible(self):
        kwargs = dict(n_control=100, n_treatment=100, reps=200, seed=123)
        first = coverage_simulation(**kwargs)
        second = coverage_simulation(**kwargs)
        assert first == second

    def test_different_seeds_differ(self):
        kwargs = dict(n_control=100, n_treatment=100, reps=200)
        assert coverage_simulation(seed=1, **kwargs) != coverage_simulation(
            seed=2, **kwargs
        )


class TestPeekingSimulation:
    def test_peeking_inflates_type1_error(self):
        result = peeking_type1_simulation(
            n_per_group=500, looks=5, reps=1000, alpha=0.05, seed=4
        )
        # One look is valid; five looks should clearly exceed 5%.
        assert result["ever_significant_rate"] > 0.08

    def test_single_look_is_near_nominal(self):
        result = peeking_type1_simulation(
            n_per_group=500, looks=1, reps=2000, alpha=0.05, seed=5
        )
        assert result["ever_significant_rate"] == pytest.approx(0.05, abs=0.03)

    def test_reproducible_with_fixed_seed(self):
        kwargs = dict(n_per_group=100, looks=3, reps=200, seed=9)
        assert peeking_type1_simulation(**kwargs) == peeking_type1_simulation(**kwargs)
