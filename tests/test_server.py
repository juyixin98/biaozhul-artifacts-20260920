"""End-to-end HTTP tests against the real stdlib server (bound to a port)."""
import json
import threading
import urllib.request
import urllib.error

import numpy as np
import pytest

from drift_monitor import server
from drift_monitor.synthetic import make_dataset, FEATURE_NAMES


@pytest.fixture()
def http_server():
    httpd = server.ThreadingHTTPServer(("127.0.0.1", 0), server.Handler)
    port = httpd.server_address[1]
    thread = threading.Thread(target=httpd.serve_forever, daemon=True)
    thread.start()
    server.STATE.monitor = None
    yield f"http://127.0.0.1:{port}"
    httpd.shutdown()
    httpd.server_close()
    thread.join(timeout=2)


def _request(base, method, path, payload=None):
    data = None
    headers = {}
    if payload is not None:
        data = json.dumps(payload).encode("utf-8")
        headers["Content-Type"] = "application/json"
    req = urllib.request.Request(base + path, data=data, headers=headers,
                                 method=method)
    try:
        with urllib.request.urlopen(req, timeout=5) as resp:
            return resp.status, json.loads(resp.read())
    except urllib.error.HTTPError as exc:
        return exc.code, json.loads(exc.read())


def _table(data: dict[str, np.ndarray]) -> dict:
    return {
        "data": {
            name: [None if np.isnan(v) else float(v) for v in arr]
            for name, arr in data.items()
        }
    }


def test_health_before_and_after_fit(http_server):
    status, body = _request(http_server, "GET", "/health")
    assert status == 200 and body["fitted"] is False

    ds = make_dataset("same", seed=42, n_baseline=1000, n_current=400)
    status, body = _request(http_server, "POST", "/fit", _table(ds.X_base))
    assert status == 200 and body["fitted"] is True
    assert [f["name"] for f in body["baseline"]["features"]] == FEATURE_NAMES

    status, body = _request(http_server, "GET", "/health")
    assert body["fitted"] is True and body["features"] == FEATURE_NAMES


def test_score_same_distribution_low_psi(http_server):
    ds = make_dataset("same", seed=42, n_baseline=3000, n_current=1500)
    _request(http_server, "POST", "/fit", _table(ds.X_base))
    status, body = _request(http_server, "POST", "/score", _table(ds.X_cur))
    assert status == 200
    assert body["summary"]["n_features"] == 4
    assert body["summary"]["max_psi"] < 0.10
    for f in body["features"]:
        assert f["metrics"]["psi_band"] == "stable"
        assert len(f["buckets"]["baseline_counts"]) == \
            len(f["buckets"]["current_counts"]) == 13


def test_score_mean_shift_high_psi_and_threshold_note(http_server):
    ds = make_dataset("mean_shift", seed=42)
    _request(http_server, "POST", "/fit", _table(ds.X_base))
    _, body = _request(http_server, "POST", "/score", _table(ds.X_cur))
    flagged = set(body["summary"]["features_flagged"])
    assert {"income", "age_z", "risk_score"} <= flagged
    note = body["summary"]["psi_thresholds"]["note"]
    assert "not statistical significance" in note


def test_score_all_missing_window(http_server):
    ds = make_dataset("all_missing", seed=42)
    _request(http_server, "POST", "/fit", _table(ds.X_base))
    _, body = _request(http_server, "POST", "/score", _table(ds.X_cur))
    risk = next(f for f in body["features"] if f["feature"] == "risk_score")
    assert risk["flags"]["all_missing_current"] is True
    assert risk["metrics"]["wasserstein_1"] is None
    assert isinstance(risk["metrics"]["psi"], float)


def test_score_before_fit_is_409(http_server):
    status, body = _request(http_server, "POST", "/score",
                            _table({"a": [1.0, 2.0]}))
    assert status == 409 and "not fitted" in body["error"]


def test_rejects_unequal_column_lengths(http_server):
    status, body = _request(http_server, "POST", "/fit",
                            {"data": {"a": [1.0, 2.0], "b": [1.0]}})
    assert status == 400 and "equal length" in body["error"]


def test_rejects_non_numeric_and_non_finite(http_server):
    status, body = _request(http_server, "POST", "/fit",
                            {"data": {"a": [1.0, "x"]}})
    assert status == 400 and "number or null" in body["error"]

    status, body = _request(http_server, "POST", "/fit",
                            {"data": {"a": [1.0, float("inf")]}})
    # json.dumps writes Infinity; the server's json.loads accepts it but
    # the column validator must reject the non-finite value explicitly.
    assert status == 400 and "non-finite" in body["error"]


def test_demo_endpoint_reports_drift_and_auc(http_server):
    status, body = _request(http_server, "POST", "/demo/synthesize",
                            {"scenario": "mean_shift"})
    assert status == 200
    assert body["scenario"] == "mean_shift"
    assert 0.0 < body["model_auc"]["baseline"] <= 1.0
    assert "model_auc" in body and "current" in body["model_auc"]


def test_demo_unknown_scenario_400(http_server):
    status, body = _request(http_server, "POST", "/demo/synthesize",
                            {"scenario": "earthquake"})
    assert status == 400


def test_monitor_export_and_load_roundtrip(http_server):
    ds = make_dataset("mixed", seed=5)
    _request(http_server, "POST", "/fit", _table(ds.X_base))
    _, scored_before = _request(http_server, "POST", "/score", _table(ds.X_cur))
    _, exported = _request(http_server, "POST", "/monitor/export")

    # A fresh server state: load the payload, then expect identical scores.
    server.STATE.monitor = None
    status, body = _request(http_server, "POST", "/monitor/load", exported)
    assert status == 200 and body["loaded"] is True
    _, scored_after = _request(http_server, "POST", "/score", _table(ds.X_cur))
    assert scored_after == scored_before


def test_unknown_route_404(http_server):
    status, body = _request(http_server, "GET", "/nope")
    assert status == 404 and "error" in body


def test_unknown_post_route_404(http_server):
    status, body = _request(http_server, "POST", "/nope", {})
    assert status == 404


def test_baseline_endpoint_reports_frozen_edges(http_server):
    status, body = _request(http_server, "GET", "/baseline")
    assert status == 409
    ds = make_dataset("same", seed=42, n_baseline=500, n_current=100)
    _request(http_server, "POST", "/fit", _table(ds.X_base))
    status, body = _request(http_server, "GET", "/baseline")
    assert status == 200
    feat = body["baseline"]["features"][0]
    assert len(feat["edges"]) == 11
    assert len(feat["bucket_labels"]) == 13


def test_fit_validation_errors(http_server):
    # non-array column
    status, body = _request(http_server, "POST", "/fit",
                            {"data": {"a": "not-an-array"}})
    assert status == 400
    # missing data object
    status, _ = _request(http_server, "POST", "/fit", {})
    assert status == 400
    # bad n_bins / strategy / alpha
    ds = make_dataset("same", seed=1, n_baseline=200, n_current=50)
    table = _table(ds.X_base)
    status, _ = _request(http_server, "POST", "/fit", {**table, "n_bins": 0})
    assert status == 400
    status, _ = _request(http_server, "POST", "/fit",
                         {**table, "strategy": "magic"})
    assert status == 400
    status, _ = _request(http_server, "POST", "/fit",
                         {**table, "alpha": 99.0})
    assert status == 400
    # all-missing baseline cannot define quantiles
    status, body = _request(http_server, "POST", "/fit",
                            {"data": {"a": [None, None, None]}})
    assert status == 400 and "fit failed" in body["error"]


def test_boolean_and_oversized_columns_rejected(http_server):
    status, body = _request(http_server, "POST", "/fit",
                            {"data": {"a": [True, False]}})
    assert status == 400
    long_col = [1.0] * (server.MAX_COLUMN_LEN + 1)
    status, _ = _request(http_server, "POST", "/fit", {"data": {"a": long_col}})
    assert status == 400


def test_malformed_json_and_empty_body(http_server):
    req = urllib.request.Request(http_server + "/fit",
                                 data=b"{not json", method="POST",
                                 headers={"Content-Type": "application/json"})
    with pytest.raises(urllib.error.HTTPError) as ei:
        urllib.request.urlopen(req, timeout=5)
    assert ei.value.code == 400

    req = urllib.request.Request(http_server + "/score", data=b"",
                                 method="POST")
    with pytest.raises(urllib.error.HTTPError) as ei:
        urllib.request.urlopen(req, timeout=5)
    assert ei.value.code == 400


def test_non_object_json_body_rejected(http_server):
    req = urllib.request.Request(http_server + "/fit", data=b"[1,2,3]",
                                 method="POST",
                                 headers={"Content-Type": "application/json"})
    with pytest.raises(urllib.error.HTTPError) as ei:
        urllib.request.urlopen(req, timeout=5)
    assert ei.value.code == 400


def test_score_feature_mismatch_400(http_server):
    ds = make_dataset("same", seed=1, n_baseline=300, n_current=80)
    _request(http_server, "POST", "/fit", _table(ds.X_base))
    partial = _table({"income": ds.X_cur["income"]})
    status, body = _request(http_server, "POST", "/score", partial)
    assert status == 400 and "missing" in body["error"]


def test_demo_default_scenario_and_load_validation(http_server):
    status, body = _request(http_server, "POST", "/demo/synthesize", {})
    assert status == 200 and body["scenario"] == "same"

    status, body = _request(http_server, "POST", "/monitor/load", {})
    assert status == 400
    status, body = _request(http_server, "POST", "/monitor/load",
                            {"monitor": {"n_bins": 1}})
    assert status == 400


def test_export_requires_fit(http_server):
    req = urllib.request.Request(http_server + "/monitor/export",
                                 data=b"", method="POST")
    with pytest.raises(urllib.error.HTTPError) as ei:
        urllib.request.urlopen(req, timeout=5)
    assert ei.value.code == 409


def test_server_main_runs_and_shuts_down(monkeypatch):
    # Drive main() with serve_forever interrupted immediately.
    monkeypatch.setattr("sys.argv", ["server", "--host", "127.0.0.1",
                                     "--port", "0"])
    calls = {"n": 0}
    class FakeServer:
        def __init__(self, addr, handler):
            self.server_address = ("127.0.0.1", 0)
        def serve_forever(self):
            calls["n"] += 1
            raise KeyboardInterrupt
        def server_close(self):
            pass
    monkeypatch.setattr(server, "ThreadingHTTPServer", FakeServer)
    server.main()
    assert calls["n"] == 1
