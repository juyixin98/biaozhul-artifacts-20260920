import numpy as np
import pytest

from abseq.analysis import VALIDITY_WARNING, analyze, analyze_groups
from abseq.synthetic import make_synthetic


class TestAnalyze:
    def test_basic_result_fields(self):
        result = analyze([1.0, 2.0, 3.0], [2.0, 3.0, 4.0])
        assert result["mean_difference"] == pytest.approx(1.0)
        assert result["n_control"] == 3
        assert result["n_treatment"] == 3
        assert result["ci_lower"] < 1.0 < result["ci_upper"]
        assert result["validity_warning"] == VALIDITY_WARNING
        assert result["assumptions"]

    def test_missing_raise_by_default(self):
        with pytest.raises(ValueError):
            analyze([1.0, float("nan"), 3.0], [1.0, 2.0, 3.0])

    def test_missing_drop_counts(self):
        result = analyze(
            [1.0, float("nan"), 3.0], [1.0, 2.0, 3.0], missing_strategy="drop"
        )
        assert result["n_control"] == 2
        assert result["n_missing_control"] == 1

    def test_impute_mean_adds_transparency_note(self):
        result = analyze(
            [1.0, float("nan"), 3.0, 4.0],
            [1.0, 2.0, 3.0, 4.0],
            missing_strategy="impute_mean",
        )
        assert "imputation_note" in result

    def test_highly_imbalanced_groups(self):
        rng = np.random.default_rng(3)
        control = rng.normal(0.0, 1.0, size=20)
        treatment = rng.normal(0.0, 1.0, size=2000)
        result = analyze(control, treatment)
        assert result["n_control"] == 20
        assert result["n_treatment"] == 2000
        assert np.isfinite(result["ci_lower"])
        assert np.isfinite(result["ci_upper"])


class TestAnalyzeGroups:
    def test_splits_by_label(self):
        groups, observations = make_synthetic(50, 50, effect=0.5, seed=11)
        result = analyze_groups(groups, observations)
        assert result["control_label"] == "control"
        assert result["treatment_label"] == "treatment"
        assert result["n_control"] == 50
        assert result["n_treatment"] == 50

    def test_rejects_wrong_label_count(self):
        with pytest.raises(ValueError):
            analyze_groups(["a", "a"], [1.0, 2.0])
        with pytest.raises(ValueError):
            analyze_groups(["a", "b", "c"], [1.0, 2.0, 3.0])

    def test_rejects_length_mismatch(self):
        with pytest.raises(ValueError):
            analyze_groups(["a", "b"], [1.0])

    def test_seed_reproducibility(self):
        g1, o1 = make_synthetic(30, 30, seed=99, missing_rate=0.1)
        g2, o2 = make_synthetic(30, 30, seed=99, missing_rate=0.1)
        np.testing.assert_array_equal(g1, g2)
        np.testing.assert_array_equal(o1, o2)
