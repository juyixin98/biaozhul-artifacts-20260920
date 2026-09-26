"""JSON 入口测试：run_request 与 CLI 主函数。"""

import json

import numpy as np
import pytest

from imu_preintegration.cli import main, run_request


def make_static_request(n=51, dt=0.01):
    t = (np.arange(n) * dt).tolist()
    return {
        "imu": {
            "timestamps": t,
            "gyro": [[0.0, 0.0, 0.0]] * n,
            "accel": [[0.0, 0.0, 9.81]] * n,
        },
        "config": {"gravity": [0.0, 0.0, -9.81]},
    }


def test_run_request_static_returns_zero_motion():
    result = run_request(make_static_request())
    assert result["validation"]["ok"]
    assert np.allclose(result["positions"], 0.0, atol=1e-9)
    assert np.allclose(result["velocities"], 0.0, atol=1e-9)
    assert len(result["orientations"]) == 51
    assert result["skipped_intervals"] == 0


def test_run_request_missing_imu_section_raises():
    with pytest.raises(ValueError, match="imu"):
        run_request({})


def test_run_request_missing_channel_raises():
    with pytest.raises(ValueError, match="gyro"):
        run_request({"imu": {"timestamps": [0.0, 0.01], "accel": [[0, 0, 9.81]] * 2}})


def test_run_request_unknown_config_field_raises():
    request = make_static_request()
    request["config"]["online_bias_estimation"] = True  # 明确不支持的功能
    with pytest.raises(ValueError, match="unknown config fields"):
        run_request(request)


def test_run_request_reports_validation_issues():
    request = make_static_request()
    request["imu"]["timestamps"][7] = request["imu"]["timestamps"][6]  # 重复时间
    result = run_request(request)
    assert not result["validation"]["ok"]
    codes = {i["code"] for i in result["validation"]["issues"]}
    assert "duplicate_timestamp" in codes
    assert result["skipped_intervals"] == 1


def test_cli_writes_output_file(tmp_path):
    request_path = tmp_path / "request.json"
    output_path = tmp_path / "result.json"
    request_path.write_text(json.dumps(make_static_request()), encoding="utf-8")
    assert main([str(request_path), "-o", str(output_path)]) == 0
    result = json.loads(output_path.read_text(encoding="utf-8"))
    assert result["validation"]["ok"]
    assert np.allclose(result["positions"], 0.0, atol=1e-9)


def test_cli_bad_request_returns_nonzero(tmp_path, capsys):
    request_path = tmp_path / "bad.json"
    request_path.write_text(json.dumps({"no_imu": True}), encoding="utf-8")
    assert main([str(request_path)]) == 1
    err = capsys.readouterr().err
    assert "imu" in err
