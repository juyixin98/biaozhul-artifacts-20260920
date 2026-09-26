"""JSON 入口（process_request）与 CLI 测试。"""

import json
import subprocess
import sys

import pytest

from imu_integrator.cli import process_request, DEMO_REQUESTS


def test_process_inline_samples():
    req = {
        "action": "integrate",
        "samples": [
            {"t": 0.0, "gyro": [0, 0, 0], "accel": [0, 0, 9.81]},
            {"t": 0.1, "gyro": [0, 0, 0], "accel": [0, 0, 9.81]},
        ],
        "gravity": [0, 0, -9.81],
        "include_trajectory": False,
    }
    resp = process_request(req)
    assert resp["ok"] is True
    summary = resp["result"]["summary"]
    assert summary["samples"] == 2
    assert summary["intervals"] == 1
    assert summary["final_position_m"] == pytest.approx([0, 0, 0], abs=1e-12)
    assert "trajectory" not in resp["result"]


def test_process_includes_trajectory_by_default():
    req = {
        "samples": [
            {"t": 0.0, "gyro": [0, 0, 0], "accel": [0, 0, 9.81]},
            {"t": 0.1, "gyro": [0, 0, 0], "accel": [0, 0, 9.81]},
        ],
    }
    resp = process_request(req)
    assert resp["ok"] is True
    traj = resp["result"]["trajectory"]
    assert len(traj) == 2
    # orientation_xyzw 为 x,y,z,w 顺序
    assert traj[0]["orientation_xyzw"] == pytest.approx([0, 0, 0, 1], abs=1e-12)


def test_process_validate_action():
    req = {
        "action": "validate",
        "samples": [
            {"t": 0.0, "gyro": [0, 0, 0], "accel": [0, 0, 9.81]},
            {"t": 0.0, "gyro": [0, 0, 0], "accel": [0, 0, 9.81]},
        ],
    }
    resp = process_request(req)
    assert resp["ok"] is False
    assert resp["error"]["code"] == "duplicate_timestamp"


def test_process_scenario_stationary():
    req = {
        "scenario": {"name": "stationary", "duration": 0.3, "dt": 0.1,
                     "gyro_bias": [0.01, 0, 0]},
        "gyro_bias": [0.01, 0, 0],
        "include_trajectory": False,
    }
    resp = process_request(req)
    assert resp["ok"] is True
    s = resp["result"]["summary"]
    assert s["samples"] == 4
    assert s["final_position_m"] == pytest.approx([0, 0, 0], abs=1e-10)


def test_process_scenario_rotation():
    req = {
        "scenario": {"name": "rotation", "duration": 1.0, "dt": 0.01,
                     "angular_velocity": [0, 0, 1.0]},
        "include_trajectory": False,
    }
    resp = process_request(req)
    assert resp["ok"] is True
    qx, qy, qz, qw = resp["result"]["summary"]["final_orientation_xyzw"]
    assert qz == pytest.approx(0.4794255386, abs=1e-6)  # sin(0.5)
    assert qw == pytest.approx(0.8775825619, abs=1e-6)  # cos(0.5)


def test_process_scenario_acceleration_variable_dt():
    req = {
        "scenario": {
            "name": "acceleration",
            "duration": 1.0,
            "dt": [0.1, 0.2, 0.3, 0.4],
            "acceleration_world": [2.0, 0, 0],
        },
        "include_trajectory": False,
    }
    resp = process_request(req)
    assert resp["ok"] is True
    s = resp["result"]["summary"]
    # v = a t = 2; p = 0.5 * 2 * 1 = 1
    assert s["final_velocity_mps"] == pytest.approx([2, 0, 0], abs=1e-10)
    assert s["final_position_m"] == pytest.approx([1, 0, 0], abs=1e-10)


def test_process_duplicate_timestamp_error_payload():
    req = {
        "samples": [
            {"t": 0.0, "gyro": [0, 0, 0], "accel": [0, 0, 9.81]},
            {"t": 0.0, "gyro": [0, 0, 0], "accel": [0, 0, 9.81]},
        ],
        "options": {"max_dt": 0.1},
    }
    resp = process_request(req)
    assert resp["ok"] is False
    err = resp["error"]
    assert err["code"] == "duplicate_timestamp"
    assert err["index"] == 1
    # JSON 可序列化
    json.dumps(resp, ensure_ascii=False)


def test_process_missing_samples_error_payload():
    req = {
        "samples": [
            {"t": 0.0, "gyro": [0, 0, 0], "accel": [0, 0, 9.81]},
            {"t": 0.5, "gyro": [0, 0, 0], "accel": [0, 0, 9.81]},
        ],
        "options": {"max_dt": 0.05},
    }
    resp = process_request(req)
    assert resp["ok"] is False
    assert resp["error"]["code"] == "missing_samples"


def test_process_gyro_unit_error_payload():
    req = {
        "samples": [
            {"t": 0.0, "gyro": [0, 0, 0], "accel": [0, 0, 9.81]},
            {"t": 0.01, "gyro": [500.0, 0, 0], "accel": [0, 0, 9.81]},
        ],
    }
    resp = process_request(req)
    assert resp["ok"] is False
    assert resp["error"]["code"] == "gyro_unit_error"
    # 声明 deg/s 后通过
    req["options"] = {"gyro_unit": "deg/s"}
    resp = process_request(req)
    assert resp["ok"] is True


def test_process_unknown_scenario():
    resp = process_request({"scenario": {"name": "flying"}})
    assert resp["ok"] is False
    assert resp["error"]["code"] == "invalid_request"


def test_process_bad_json_shape():
    assert process_request([1, 2, 3])["ok"] is False
    assert process_request({"action": "explode"})["ok"] is False


def _run_cli(*args, stdin=None):
    return subprocess.run(
        [sys.executable, "-m", "imu_integrator.cli", *args],
        input=stdin, capture_output=True, text=True, check=False,
    )


def test_cli_demo_scenarios():
    for name in DEMO_REQUESTS:
        proc = _run_cli("demo", "--scenario", name)
        assert proc.returncode == 0, proc.stderr
        resp = json.loads(proc.stdout)
        assert resp["ok"] is True


def test_cli_run_file(tmp_path):
    req_path = tmp_path / "req.json"
    out_path = tmp_path / "out.json"
    req_path.write_text(json.dumps({
        "scenario": {"name": "stationary", "duration": 0.2, "dt": 0.1}
    }), encoding="utf-8")
    proc = _run_cli("run", str(req_path), "-o", str(out_path))
    assert proc.returncode == 0, proc.stderr
    resp = json.loads(out_path.read_text(encoding="utf-8"))
    assert resp["ok"] is True


def test_cli_run_stdin():
    req = json.dumps({
        "samples": [
            {"t": 0.0, "gyro": [0, 0, 0], "accel": [0, 0, 9.81]},
            {"t": 0.1, "gyro": [0, 0, 0], "accel": [0, 0, 9.81]},
        ],
        "include_trajectory": False,
    })
    proc = _run_cli("run", "-", stdin=req)
    assert proc.returncode == 0, proc.stderr
    assert json.loads(proc.stdout)["ok"] is True


def test_cli_validate_detects_duplicate(tmp_path):
    req_path = tmp_path / "req.json"
    req_path.write_text(json.dumps({
        "samples": [
            {"t": 0.0, "gyro": [0, 0, 0], "accel": [0, 0, 9.81]},
            {"t": 0.0, "gyro": [0, 0, 0], "accel": [0, 0, 9.81]},
        ],
    }), encoding="utf-8")
    proc = _run_cli("validate", str(req_path))
    assert proc.returncode == 1
    resp = json.loads(proc.stdout)
    assert resp["error"]["code"] == "duplicate_timestamp"


def test_cli_bad_json_returns_exit_code_2(tmp_path):
    req_path = tmp_path / "bad.json"
    req_path.write_text("{not json", encoding="utf-8")
    proc = _run_cli("run", str(req_path))
    assert proc.returncode == 2
