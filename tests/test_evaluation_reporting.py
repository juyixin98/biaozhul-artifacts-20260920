"""评估与报告输出测试。"""

import csv
import json

import numpy as np

from burst_detector.detector import Decision, DetectorConfig
from burst_detector.evaluation import LabeledEvent, evaluate
from burst_detector.processing import detect_signal
from burst_detector.reporting import (
    build_summary,
    write_marks_pcm,
    write_points_csv,
    write_summary_json,
)
from burst_detector.signal_io import make_scenario


def test_spike_scenario_detection_delay_zero():
    cfg = DetectorConfig()
    x = make_scenario("spike", spike_positions=(600, 1200, 1700), spike_amp=8.0)
    r = detect_signal(x, cfg)
    events = [LabeledEvent("spike", p, p + 5) for p in (600, 1200, 1700)]
    report = evaluate(r, events)
    assert report.n_detected == 3
    assert report.delays == (0, 0, 0)
    assert report.n_missed == 0


def test_step_scenario_detects_within_window():
    cfg = DetectorConfig()
    x = make_scenario("step", step_at=1000, step_amp=6.0)
    r = detect_signal(x, cfg)
    report = evaluate(r, [LabeledEvent("step", 1000, 1200)])
    assert report.n_detected == 1
    assert 0 <= report.delays[0] < 200


def test_missed_event_reported():
    # 极小阶跃（0.5 sigma）在 6 阈值下应当漏报或延迟，事件窗内无报警即漏报。
    cfg = DetectorConfig()
    x = make_scenario("step", step_at=500, step_amp=0.5, n=1000)
    r = detect_signal(x, cfg)
    report = evaluate(r, [LabeledEvent("tiny_step", 500, 520)])
    # 明确语义：窗内未报警必须计为漏报而不是静默忽略。
    if report.n_missed == 0:
        assert report.delays[0] >= 0
    else:
        assert report.events[0].detected is False
        assert report.events[0].delay == -1


def test_clean_signal_false_alarm_rate():
    cfg = DetectorConfig(threshold=8.0)
    r = detect_signal(make_scenario("clean", n=2000, seed=11), cfg)
    report = evaluate(r, [])
    assert report.false_alarm_points == 0
    assert report.false_alarm_rate == 0.0
    # 可判决点排除了预热区与缺样。
    assert report.eligible_points <= 2000


def test_invalid_event_window_rejected():
    import pytest
    with pytest.raises(ValueError):
        LabeledEvent("x", 100, 90)


def test_points_csv_roundtrip(tmp_path):
    cfg = DetectorConfig()
    r = detect_signal(make_scenario("spike", n=400), cfg)
    path = write_points_csv(tmp_path / "p.csv", r)
    with open(path) as f:
        rows = list(csv.DictReader(f))
    assert len(rows) == 400
    assert rows[0]["decision"] == "warmup"
    spike_rows = [row for row in rows if row["decision"] == "anomaly"]
    assert {int(row["index"]) for row in spike_rows} == set(
        r.anomaly_indices.tolist()
    )


def test_marks_pcm_is_equivalent_to_anomaly_mask(tmp_path):
    cfg = DetectorConfig()
    r = detect_signal(make_scenario("step", n=300), cfg)
    path = write_marks_pcm(tmp_path / "m.pcm", r)
    marks = np.fromfile(path, dtype=np.uint8)
    np.testing.assert_array_equal(marks, r.is_anomaly.astype(np.uint8))


def test_summary_json_structure(tmp_path):
    cfg = DetectorConfig()
    r = detect_signal(make_scenario("drift", n=500), cfg)
    report = evaluate(r, [LabeledEvent("drift", 400, 500)])
    path = write_summary_json(
        tmp_path / "s.json", r, cfg, source="synthetic:drift",
        eval_report=report,
    )
    data = json.loads((tmp_path / "s.json").read_text())
    assert data["n_samples"] == 500
    assert data["config"]["window_size"] == 200
    assert data["config"]["missing_policy"] == "skip"
    assert "evaluation" in data
    assert data["evaluation"]["n_events"] == 1
    summary = build_summary(r, cfg)  # 无评估时也可序列化
    assert "evaluation" not in summary
