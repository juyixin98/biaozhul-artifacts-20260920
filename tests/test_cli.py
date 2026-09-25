"""CLI（python -m mospp）端到端测试。"""

import json
import subprocess
import sys
from pathlib import Path

from .helpers import req


REPO_ROOT = Path(__file__).resolve().parents[1]


def _run(args, input_text=None):
    return subprocess.run(
        [sys.executable, "-m", "mospp", *args],
        input=input_text,
        capture_output=True,
        text=True,
        cwd=REPO_ROOT,
    )


def test_cli_from_stdin():
    r = req(
        [
            (0, 1, 1, 4), (1, 3, 1, 1),   # 0-1-3: (2,5)
            (0, 3, 5, 0),                 # 直达:   (5,0)
        ],
        0, 3,
    )
    proc = _run([], input_text=json.dumps(r))
    assert proc.returncode == 0, proc.stderr
    resp = json.loads(proc.stdout)
    assert resp["status"] == "ok"
    assert len(resp["paths"]) == 2


def test_cli_from_file(tmp_path):
    r = req([(0, 1, 1, 1), (1, 2, 1, 1)], 0, 2)
    f = tmp_path / "r.json"
    f.write_text(json.dumps(r), encoding="utf-8")
    proc = _run([str(f)])
    assert proc.returncode == 0, proc.stderr
    resp = json.loads(proc.stdout)
    assert resp["pareto"] == [{"time": 2, "cost": 2}]


def test_cli_unreachable_exit_zero():
    r = req([(0, 1, 1, 1), (2, 3, 1, 1)], 0, 3)
    proc = _run([], input_text=json.dumps(r))
    assert proc.returncode == 0
    assert json.loads(proc.stdout)["status"] == "unreachable"


def test_cli_invalid_exit_one():
    proc = _run([], input_text='{"edges": []}')
    assert proc.returncode == 1
    out = json.loads(proc.stderr) if proc.stderr.strip().startswith("{") else json.loads(
        proc.stdout or "{}"
    )
    assert out.get("status") in ("invalid_request", None)


def test_cli_bad_json():
    proc = _run([], input_text="{not json")
    assert proc.returncode == 1
    assert "invalid_request" in proc.stderr
