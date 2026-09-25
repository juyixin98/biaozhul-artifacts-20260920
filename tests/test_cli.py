"""CLI 端到端集成测试：detect / acceptance / generate-pcm。"""

import json
import subprocess
import sys

import numpy as np
import pytest

from burst_detector.cli import run_acceptance, run_detect, run_generate_pcm
from burst_detector.signal_io import read_pcm


def test_run_detect_synthetic_writes_artifacts(tmp_path):
    request = {
        "name": "spike_demo",
        "output_dir": str(tmp_path),
        "input": {"scenario": "spike", "n": 800,
                  "spike_positions": [300, 600], "spike_amp": 8.0},
        "config": {"window_size": 200, "threshold": 6.0},
        "events": [
            {"kind": "spike", "start": 300, "eval_end": 305},
            {"kind": "spike", "start": 600, "eval_end": 605},
        ],
        "blocks": [123, 400, 277],
    }
    out = run_detect(request)
    summary = json.loads((tmp_path / "spike_demo_summary.json").read_text())
    assert summary["evaluation"]["n_detected"] == 2
    assert summary["evaluation"]["delays"] == [0, 0]
    for key in ("points_csv", "summary_json", "marks_pcm"):
        assert (tmp_path / out["paths"][key].split("/")[-1]).exists()


def test_run_detect_pcm_input(tmp_path):
    pcm_path = tmp_path / "in.pcm"
    run_generate_pcm(str(pcm_path), kind="step", dtype="int16")
    request = {
        "name": "pcm_case",
        "output_dir": str(tmp_path / "out"),
        "input": {"pcm": str(pcm_path), "dtype": "int16"},
    }
    out = run_detect(request)
    assert (tmp_path / "out" / "pcm_case_points.csv").exists()
    sig = read_pcm(str(pcm_path), dtype="int16")
    assert sig.size == 2000


def test_run_detect_rejects_bad_input(tmp_path):
    with pytest.raises(ValueError):
        run_detect({"output_dir": str(tmp_path), "input": {}})


def test_acceptance_all_scenarios_pass_consistency(tmp_path):
    out = run_acceptance(str(tmp_path))
    report = json.loads((tmp_path / "acceptance_report.json").read_text())
    assert set(report) == {"step", "drift", "spike", "clean"}
    # 逐块与逐点一致性在 run_acceptance 内部断言，至此必然通过。
    assert report["spike"]["n_detected"] == 3
    assert report["spike"]["delays"] == [0, 0, 0]
    assert report["step"]["n_detected"] == 1
    assert report["clean"]["false_alarm_points"] == 0
    # 漂移场景：在评估窗内应当检出（参数默认 drift_rate=0.01 sigma/点）。
    assert report["drift"]["n_detected"] == 1


def test_cli_subprocess_acceptance(tmp_path):
    proc = subprocess.run(
        [sys.executable, "-m", "burst_detector", "acceptance",
         "--out", str(tmp_path)],
        cwd=__import__("pathlib").Path(__file__).resolve().parents[1],
        capture_output=True, text=True, check=True,
    )
    assert "逐块一致性: 通过" in proc.stdout
    assert (tmp_path / "acceptance_report.json").exists()


def test_cli_subprocess_detect_stdin(tmp_path):
    request = {
        "name": "stdin_case",
        "output_dir": str(tmp_path),
        "input": {"scenario": "clean", "n": 300},
        "config": {"threshold": 8.0},
    }
    proc = subprocess.run(
        [sys.executable, "-m", "burst_detector", "detect"],
        input=json.dumps(request), capture_output=True, text=True, check=True,
        cwd=__import__("pathlib").Path(__file__).resolve().parents[1],
    )
    assert "异常点 0" in proc.stdout
    assert (tmp_path / "stdin_case_summary.json").exists()
