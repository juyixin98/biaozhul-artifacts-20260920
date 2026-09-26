"""命令行入口 (__main__) 测试。"""

import json
import subprocess
import sys

import pytest

from sensor_matcher.__main__ import main


def _write(tmp_path, name: str, text: str) -> str:
    p = tmp_path / name
    p.write_text(text, encoding="utf-8")
    return str(p)


@pytest.mark.unit
def test_cli_file_success(tmp_path, capsys: pytest.CaptureFixture[str]) -> None:
    req = json.dumps(
        {
            "mode": "messages",
            "tolerance": 0.05,
            "streams": {
                "a": [{"id": "a1", "timestamp": 0.0}],
                "b": [{"id": "b1", "timestamp": 0.01}],
            },
        }
    )
    path = _write(tmp_path, "req.json", req)
    rc = main([path])
    assert rc == 0
    out = capsys.readouterr().out
    assert ("a1", "b1") in [(m["id_a"], m["id_b"]) for m in json.loads(out)["online"]["matches"]]


@pytest.mark.unit
def test_cli_missing_file(tmp_path, capsys: pytest.CaptureFixture[str]) -> None:
    rc = main([str(tmp_path / "nope.json")])
    assert rc == 2
    err = capsys.readouterr().err
    assert "无法读取请求文件" in err


@pytest.mark.unit
def test_cli_too_many_args(capsys: pytest.CaptureFixture[str]) -> None:
    rc = main(["a.json", "b.json"])
    assert rc == 2
    assert "用法" in capsys.readouterr().err


@pytest.mark.unit
def test_cli_invalid_request_file(tmp_path, capsys: pytest.CaptureFixture[str]) -> None:
    path = _write(tmp_path, "bad.json", "{broken")
    rc = main([path])
    assert rc == 2
    err = capsys.readouterr().err
    assert json.loads(err)["error"]


@pytest.mark.unit
def test_cli_as_subprocess(tmp_path) -> None:
    """确认 ``python -m sensor_matcher`` 可作为模块入口运行。"""
    req = json.dumps(
        {
            "mode": "messages",
            "tolerance": 0.1,
            "streams": {
                "a": [{"id": "x", "timestamp": 0.0}],
                "b": [{"id": "y", "timestamp": 0.05}],
            },
        }
    )
    path = _write(tmp_path, "r.json", req)
    proc = subprocess.run(
        [sys.executable, "-m", "sensor_matcher", path],
        capture_output=True,
        text=True,
    )
    assert proc.returncode == 0
    assert ("x", "y") in [
        (m["id_a"], m["id_b"]) for m in json.loads(proc.stdout)["online"]["matches"]
    ]
