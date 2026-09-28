"""Unit tests for Monte-Carlo evidence and reproducibility primitives."""
from __future__ import annotations

import logging

import pytest

from ssp.contracts import (
    Alternative,
    BinomialEffectScale,
    BinomialSpec,
    NormalEffectScale,
    NormalSpec,
    Allocation,
)
from ssp.evidence import (
    simulate_binomial_power_asymptotic,
    simulate_binomial_power_exact,
    simulate_normal_power,
)
from ssp.repro import derive_seed


class TestNormalSimulation:
    @pytest.mark.unit
    @pytest.mark.slow
    def test_simulated_z_power_matches_analytic_inside_ci(self, logger):
        spec = NormalSpec(
            Alternative.TWO_SIDED, 0.05, 0.8, 0.5,
            NormalEffectScale.STANDARDIZED_D, False, known_sigma=True,
        ).validate()
        ev = simulate_normal_power(spec, Allocation(n0=32, n1=0), 12_000, seed=42, logger=logger)
        # Analytic 0.8074 must be inside the simulation 95% CI.
        assert ev.ci95_low <= 0.80743 <= ev.ci95_high
        assert ev.estimated_power == pytest.approx(0.80743, abs=0.02)

    @pytest.mark.unit
    @pytest.mark.slow
    def test_simulated_t_power_matches_noncentral_t(self, logger):
        spec = NormalSpec(
            Alternative.TWO_SIDED, 0.05, 0.8, 0.5,
            NormalEffectScale.STANDARDIZED_D, False, known_sigma=False,
        ).validate()
        ev = simulate_normal_power(spec, Allocation(n0=34, n1=0), 12_000, seed=7, logger=logger)
        assert ev.ci95_low <= 0.80778 <= ev.ci95_high

    @pytest.mark.unit
    @pytest.mark.slow
    def test_simulated_two_sample_direction_greater(self, logger):
        spec = NormalSpec(
            Alternative.GREATER, 0.05, 0.9, 0.3,
            NormalEffectScale.STANDARDIZED_D, True, known_sigma=True,
            allocation_ratio=1.0,
        ).validate()
        ev = simulate_normal_power(spec, Allocation(n0=191, n1=191), 10_000, seed=9, logger=logger)
        # Around 0.9 at n0=191 (analytic 0.90093).
        assert ev.estimated_power == pytest.approx(0.9, abs=0.03)

    @pytest.mark.unit
    def test_seed_makes_output_reproducible(self, logger):
        spec = NormalSpec(
            Alternative.TWO_SIDED, 0.05, 0.8, 0.5,
            NormalEffectScale.STANDARDIZED_D, False, known_sigma=True,
        ).validate()
        a = simulate_normal_power(spec, Allocation(n0=32, n1=0), 3_000, seed=123, logger=logger)
        b = simulate_normal_power(spec, Allocation(n0=32, n1=0), 3_000, seed=123, logger=logger)
        assert a.rejections == b.rejections
        assert a.estimated_power == b.estimated_power

    @pytest.mark.unit
    def test_different_seeds_can_differ(self, logger):
        spec = NormalSpec(
            Alternative.TWO_SIDED, 0.05, 0.8, 0.5,
            NormalEffectScale.STANDARDIZED_D, False, known_sigma=True,
        ).validate()
        a = simulate_normal_power(spec, Allocation(n0=32, n1=0), 3_000, seed=1, logger=logger)
        b = simulate_normal_power(spec, Allocation(n0=32, n1=0), 3_000, seed=2, logger=logger)
        # Not a logical requirement to differ, but with these seeds they do.
        assert a.rejections != b.rejections


class TestBinomialSimulation:
    @pytest.mark.unit
    @pytest.mark.slow
    def test_asymptotic_simulation_matches_operational_score_enumeration(self, logger):
        # The simulation executes the random-denominator score statistic, so it
        # must agree with the independent exact enumeration of THAT statistic
        # (0.80445 at n=295), not with the fixed-SE approximation (0.80089).
        from reference_oracle import score_statistic_power_two

        operational = score_statistic_power_two(295, 295, 0.2, 0.3, 0.05, "two_sided")
        spec = BinomialSpec(
            Alternative.TWO_SIDED, 0.05, 0.8, 0.2, 0.3,
            BinomialEffectScale.PROPORTIONS, True,
        ).validate()
        ev = simulate_binomial_power_asymptotic(
            spec, Allocation(n0=295, n1=295), 20_000, seed=5, logger=logger
        )
        assert ev.ci95_low <= operational <= ev.ci95_high
        assert ev.estimated_power == pytest.approx(operational, abs=0.015)

    @pytest.mark.unit
    @pytest.mark.slow
    def test_exact_simulation_matches_enumeration_low_base_rate(self, logger):
        spec = BinomialSpec(
            Alternative.GREATER, 0.05, 0.8, 0.01, 0.03,
            BinomialEffectScale.PROPORTIONS, False,
        ).validate()
        ev = simulate_binomial_power_exact(
            spec, Allocation(n0=301, n1=0), 16_000, seed=11, logger=logger
        )
        assert ev.ci95_low <= 0.80009 <= ev.ci95_high

    @pytest.mark.unit
    @pytest.mark.slow
    def test_exact_fisher_simulation_matches_mixed_power(self, logger):
        spec = BinomialSpec(
            Alternative.GREATER, 0.05, 0.8, 0.05, 0.15,
            BinomialEffectScale.PROPORTIONS, True,
        ).validate()
        ev = simulate_binomial_power_exact(
            spec, Allocation(n0=126, n1=126), 12_000, seed=13, logger=logger
        )
        assert ev.ci95_low <= 0.80141 <= ev.ci95_high

    @pytest.mark.unit
    def test_evidence_reports_mcse_and_bounded_ci(self, logger):
        spec = BinomialSpec(
            Alternative.GREATER, 0.05, 0.8, 0.5, 0.7,
            BinomialEffectScale.PROPORTIONS, False,
        ).validate()
        ev = simulate_binomial_power_exact(
            spec, Allocation(n0=60, n1=0), 4_000, seed=3, logger=logger
        )
        assert 0.0 <= ev.ci95_low <= ev.estimated_power <= ev.ci95_high <= 1.0
        assert ev.mc_standard_error > 0


class TestSeedDerivation:
    @pytest.mark.unit
    def test_seed_is_deterministic_and_distinct_by_purpose(self):
        assert derive_seed("run-1", "normal") == derive_seed("run-1", "normal")
        assert derive_seed("run-1", "normal") != derive_seed("run-1", "binomial")
        assert 0 <= derive_seed("x", "y") < 2 ** 63


class TestProgressLogging:
    @pytest.mark.unit
    def test_simulation_emits_progress_steps(self, logger, caplog):
        spec = NormalSpec(
            Alternative.TWO_SIDED, 0.05, 0.8, 0.5,
            NormalEffectScale.STANDARDIZED_D, False, known_sigma=True,
        ).validate()
        with caplog.at_level(logging.INFO, logger=f"ssp.{logger.run_id}"):
            simulate_normal_power(spec, Allocation(n0=32, n1=0), 4_500, seed=1, logger=logger)
        events = [r.getMessage() for r in caplog.records]
        joined = " ".join(events)
        assert "simulation_start" in joined
        assert "simulation_progress" in joined
        assert "simulation_complete" in joined
        # Progress shows the run id correlation fields.
        assert all(getattr(r, "run_id", None) == logger.run_id for r in caplog.records)
