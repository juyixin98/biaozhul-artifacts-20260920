"""Tests for DriftMonitor: end-to-end scenarios, flags, serialization."""
import numpy as np

from drift_monitor import DriftMonitor, make_dataset
from drift_monitor.monitor import SMALL_SAMPLE_N


def test_same_distribution_scenario_is_stable():
    ds = make_dataset("same", seed=42)
    mon = DriftMonitor(n_bins=10).fit(ds.X_base)
    report = mon.score(ds.X_cur)
    for f in report.features:
        assert f.psi < 0.10, f"{f.feature} PSI={f.psi} unexpectedly high"
        assert f.psi_band == "stable"


def test_mean_shift_scenario_triggers_significant_psi():
    ds = make_dataset("mean_shift", seed=42)
    mon = DriftMonitor(n_bins=10).fit(ds.X_base)
    report = mon.score(ds.X_cur)
    by_name = {f.feature: f for f in report.features}
    for shifted in ("income", "age_z", "risk_score"):
        assert by_name[shifted].psi >= 0.25, (
            f"{shifted} PSI={by_name[shifted].psi} should signal a shift")
    # tx_count was untouched
    assert by_name["tx_count"].psi_band == "stable"


def test_scale_change_detected_on_tx_count():
    ds = make_dataset("scale_change", seed=42)
    mon = DriftMonitor(n_bins=10).fit(ds.X_base)
    report = mon.score(ds.X_cur)
    by_name = {f.feature: f for f in report.features}
    assert by_name["tx_count"].psi_band != "stable"


def test_missing_spike_reports_missing_rate_shift():
    ds = make_dataset("missing_spike", seed=42)
    mon = DriftMonitor(n_bins=10).fit(ds.X_base)
    f = next(x for x in mon.score(ds.X_cur).features if x.feature == "income")
    assert f.missing_rate_baseline < 0.05
    assert f.missing_rate_current > 0.30
    assert f.missing_rate_current - f.missing_rate_baseline > 0.25


def test_small_sample_is_flagged():
    ds = make_dataset("small_sample", seed=42)
    mon = DriftMonitor(n_bins=10).fit(ds.X_base)
    f = mon.score(ds.X_cur).features[0]
    assert f.small_sample
    assert f.non_missing_current < SMALL_SAMPLE_N
    assert any("noisy" in w for w in f.warnings)


def test_all_missing_current_window_is_scored_and_flagged():
    ds = make_dataset("all_missing", seed=42)
    mon = DriftMonitor(n_bins=10).fit(ds.X_base)
    f = next(x for x in mon.score(ds.X_cur).features
             if x.feature == "risk_score")
    assert f.all_missing_current
    assert f.missing_rate_current == 1.0
    assert np.isnan(f.wasserstein_1)
    # PSI stays finite thanks to Laplace smoothing and is reported as JSON-null
    payload = f.to_dict()
    assert np.isfinite(f.psi)
    assert payload["metrics"]["wasserstein_1"] is None
    assert any("entirely missing" in w for w in f.warnings)


def test_empty_window_does_not_crash():
    rng = np.random.default_rng(0)
    base = {"a": rng.normal(size=500)}
    mon = DriftMonitor(n_bins=5).fit(base)
    f = mon.score({"a": np.array([])}).features[0]
    assert f.psi_band == "undefined"
    assert any("empty" in w for w in f.warnings)


def test_empty_buckets_produce_warning_and_finite_psi():
    # Discrete feature with few levels vs. 10 bins -> zero-width/empty
    # buckets are routine and must not break anything.
    rng = np.random.default_rng(1)
    base = {"d": rng.integers(0, 3, size=1000).astype(float)}
    cur = {"d": rng.integers(0, 3, size=500).astype(float)}
    f = DriftMonitor(n_bins=10).fit(base).score(cur).features[0]
    assert np.isfinite(f.psi)
    assert any("empty buckets" in w for w in f.warnings)


def test_score_checks_feature_set():
    mon = DriftMonitor().fit({"a": np.arange(10.0), "b": np.arange(10.0)})
    try:
        mon.score({"a": np.arange(5.0)})
    except ValueError as exc:
        assert "missing" in str(exc)
    else:  # pragma: no cover
        raise AssertionError("expected ValueError for missing feature")
    try:
        mon.score({"a": np.arange(5.0), "b": np.arange(5.0),
                   "c": np.arange(5.0)})
    except ValueError as exc:
        assert "unknown" in str(exc)
    else:  # pragma: no cover
        raise AssertionError("expected ValueError for unknown feature")


def test_report_dict_is_json_serializable_with_no_nan_token():
    import json
    ds = make_dataset("mixed", seed=7)
    report = DriftMonitor(n_bins=10).fit(ds.X_base).score(ds.X_cur).to_dict()
    raw = json.dumps(report)
    assert "NaN" not in raw
    summary = report["summary"]
    assert summary["psi_thresholds"]["stable_below"] == 0.10
    assert "not statistical significance" in summary["psi_thresholds"]["note"]


def test_monitor_round_trips_and_scores_identically(tmp_path):
    import json
    ds = make_dataset("mean_shift", seed=3)
    mon = DriftMonitor(n_bins=8, strategy="quantile", alpha=0.5).fit(ds.X_base)
    before = mon.score(ds.X_cur).to_dict()

    path = tmp_path / "monitor.json"
    path.write_text(json.dumps(mon.to_dict()))
    restored = DriftMonitor.from_dict(json.loads(path.read_text()))

    after = restored.score(ds.X_cur).to_dict()
    assert after == before


def test_metrics_ordering_shift_greater_than_same():
    same = make_dataset("same", seed=11)
    shifted = make_dataset("mean_shift", seed=11)
    # Baselines are identical for a fixed seed by construction.
    mon = DriftMonitor(n_bins=10).fit(same.X_base)
    psi_same = {f.feature: f.psi for f in mon.score(same.X_cur).features}
    psi_shift = {f.feature: f.psi for f in mon.score(shifted.X_cur).features}
    for name in ("income", "age_z", "risk_score"):
        assert psi_shift[name] > psi_same[name]
