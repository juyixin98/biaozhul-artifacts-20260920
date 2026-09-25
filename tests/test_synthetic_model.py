"""Tests for reproducible synthetic data and the NumPy logistic model."""
import numpy as np
import pytest

from drift_monitor import make_dataset
from drift_monitor.synthetic import SCENARIOS, FEATURE_NAMES
from drift_monitor.model import LogisticModel, roc_auc


@pytest.mark.parametrize("scenario", SCENARIOS)
def test_scenarios_are_reproducible_and_well_formed(scenario):
    a = make_dataset(scenario, seed=42)
    b = make_dataset(scenario, seed=42)
    for name in FEATURE_NAMES:
        np.testing.assert_array_equal(a.X_cur[name], b.X_cur[name])
    np.testing.assert_array_equal(a.y_cur, b.y_cur)
    assert a.X_base["income"].shape == (5000,)
    assert a.X_cur["income"].shape[0] == a.y_cur.shape[0]


def test_baseline_is_identical_across_scenarios():
    ref = make_dataset("same", seed=42)
    for scenario in ("mean_shift", "missing_spike", "all_missing"):
        other = make_dataset(scenario, seed=42)
        for name in FEATURE_NAMES:
            np.testing.assert_array_equal(
                ref.X_base[name], other.X_base[name],
                err_msg=f"baseline {name} changed under {scenario}")


def test_small_sample_scenario_length():
    ds = make_dataset("small_sample", seed=1)
    assert ds.X_cur["age_z"].size == 12


def test_all_missing_scenario_is_all_nan():
    ds = make_dataset("all_missing", seed=1)
    assert np.all(np.isnan(ds.X_cur["risk_score"]))


def test_unknown_scenario_rejected():
    with pytest.raises(ValueError):
        make_dataset("not_a_scenario")


def test_roc_auc_perfect_and_random_predictions():
    y = np.array([0, 0, 1, 1])
    assert roc_auc(y, np.array([0.1, 0.2, 0.8, 0.9])) == pytest.approx(1.0)
    assert roc_auc(y, np.array([0.9, 0.8, 0.2, 0.1])) == pytest.approx(0.0)
    # Scores independent of labels but deterministic: 0.5 with a tie
    assert roc_auc(y, np.array([0.5, 0.5, 0.5, 0.5])) == pytest.approx(0.5)


def test_roc_auc_single_class_is_nan():
    assert np.isnan(roc_auc(np.zeros(5), np.arange(5.0)))


def test_logistic_model_learns_signal_and_handles_missing():
    ds = make_dataset("same", seed=42)
    model = LogisticModel(FEATURE_NAMES).fit(ds.X_base, ds.y_base)
    auc = roc_auc(ds.y_cur, model.predict_proba(ds.X_cur))
    # Labels genuinely depend on the features; expect clearly-better-than
    # random ranking (0.5), without claiming any specific production value.
    assert auc > 0.75


def test_model_auc_degrades_under_mean_shift_or_stays_informative():
    # The shift changes the feature distribution but not the label rule,
    # so AUC may stay similar; this test only pins that the pipeline runs
    # and stays in a valid range - it must not be read as a drift proof.
    ds = make_dataset("mean_shift", seed=42)
    model = LogisticModel(FEATURE_NAMES).fit(ds.X_base, ds.y_base)
    auc = roc_auc(ds.y_cur, model.predict_proba(ds.X_cur))
    assert 0.0 <= auc <= 1.0


def test_model_predict_before_fit_raises():
    with pytest.raises(RuntimeError):
        LogisticModel(FEATURE_NAMES).predict_proba({
            n: np.zeros(3) for n in FEATURE_NAMES})
