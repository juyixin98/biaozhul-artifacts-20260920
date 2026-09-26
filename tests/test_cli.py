"""CLI 入口测试。"""

import json
import subprocess
import sys

import pytest

from odometry.cli import main

ROBOT = {
    "wheel_diameter_left": 0.1,
    "wheel_diameter_right": 0.1,
    "track_width": 0.4,
    "ticks_per_revolution": 1000,
    "encoder_modulus": 1000,
}

SAMPLES = [
    {"t": i * 0.1, "left": 100 * i, "right": 100 * i} for i in range(11)
]


def test_cli_file_to_file(tmp_path):
    src = tmp_path / "in.json"
    dst = tmp_path / "out.json"
    src.write_text(
        json.dumps({"robot": ROBOT, "samples": SAMPLES}), encoding="utf-8"
    )
    assert main([str(src), "-o", str(dst)]) == 0
    result = json.loads(dst.read_text(encoding="utf-8"))
    assert result["summary"]["sample_count"] == 11
    assert result["summary"]["final_pose"]["x"] == pytest.approx(
        0.1 * 3.141592653589793, abs=1e-9
    )


def test_cli_stdin_to_stdout(tmp_path, capsys, monkeypatch):
    import io

    monkeypatch.setattr(
        sys, "stdin", io.StringIO(json.dumps({"robot": ROBOT, "samples": SAMPLES}))
    )
    assert main([]) == 0
    out = json.loads(capsys.readouterr().out)
    assert out["summary"]["sample_count"] == 11


def test_cli_invalid_json_exits_2(tmp_path, capsys):
    src = tmp_path / "bad.json"
    src.write_text("{not json", encoding="utf-8")
    assert main([str(src)]) == 2
    assert "error" in capsys.readouterr().err


def test_cli_invalid_request_exits_2(tmp_path, capsys):
    src = tmp_path / "bad2.json"
    src.write_text(json.dumps({"samples": []}), encoding="utf-8")
    assert main([str(src)]) == 2
    err = json.loads(capsys.readouterr().err)
    assert "error" in err


def test_cli_module_invocation(tmp_path):
    src = tmp_path / "in.json"
    src.write_text(
        json.dumps({"robot": ROBOT, "samples": SAMPLES}), encoding="utf-8"
    )
    # 在仓库根目录运行（pytest 从根目录触发，子进程继承可导入环境）
    proc = subprocess.run(
        [sys.executable, "-m", "odometry", str(src)],
        capture_output=True,
        text=True,
    )
    assert proc.returncode == 0
    result = json.loads(proc.stdout)
    assert result["summary"]["sample_count"] == 11
