"""JSON 入口的端到端验证。"""

import json
import subprocess
import sys
from pathlib import Path

import numpy as np
import pytest

from transform_tree.json_api import handle_request

SQRT2_2 = float(np.sqrt(2.0) / 2.0)

REQUEST = {
    "transforms": [
        {"parent": "world", "child": "base", "time": 0.0,
         "translation": [1, 0, 0], "rotation_xyzw": [0, 0, 0, 1]},
        {"parent": "world", "child": "base", "time": 10.0,
         "translation": [1, 0, 0], "rotation_xyzw": [0, 0, 0, 1]},
        {"parent": "base", "child": "arm", "time": 0.0,
         "translation": [0, 1, 0], "rotation_xyzw": [0, 0, SQRT2_2, SQRT2_2]},
        {"parent": "base", "child": "arm", "time": 10.0,
         "translation": [0, 1, 0], "rotation_xyzw": [0, 0, SQRT2_2, SQRT2_2]},
        {"parent": "arm", "child": "tool", "time": 0.0,
         "translation": [0, 0, 1], "rotation_xyzw": [0, 0, 0, 1]},
        {"parent": "arm", "child": "tool", "time": 10.0,
         "translation": [0, 0, 1], "rotation_xyzw": [0, 0, 0, 1]},
    ],
    "queries": [
        {"target": "world", "source": "tool", "time": 5.0},
        {"target": "tool", "source": "world", "time": 5.0},
        {"target": "world", "source": "tool", "time": 99.0},
    ],
}


def test_handle_request_success_and_per_query_error():
    resp = handle_request(REQUEST)
    assert resp["ok"] is True
    assert resp["frames"] == ["arm", "base", "tool", "world"]
    fwd, inv, late = resp["results"]

    assert fwd["ok"] is True
    np.testing.assert_allclose(fwd["translation"], [1, 1, 1], atol=1e-12)
    np.testing.assert_allclose(
        fwd["rotation_xyzw"], [0, 0, SQRT2_2, SQRT2_2], atol=1e-12
    )
    # 4x4 齐次矩阵末行应为 [0,0,0,1]
    np.testing.assert_allclose(fwd["matrix"][3], [0, 0, 0, 1], atol=1e-12)

    assert inv["ok"] is True
    np.testing.assert_allclose(inv["translation"], [-1, 1, -1], atol=1e-12)

    assert late["ok"] is False
    assert late["error"]["type"] == "ExtrapolationError"


def test_cycle_in_transforms_fails_whole_request():
    bad = {
        "transforms": [
            {"parent": "a", "child": "b", "time": 0.0,
             "translation": [0, 0, 0], "rotation_xyzw": [0, 0, 0, 1]},
            {"parent": "b", "child": "a", "time": 0.0,
             "translation": [0, 0, 0], "rotation_xyzw": [0, 0, 0, 1]},
        ],
        "queries": [],
    }
    resp = handle_request(bad)
    assert resp["ok"] is False
    assert resp["error"]["type"] == "CycleError"


def test_multi_parent_in_transforms_fails_whole_request():
    bad = {
        "transforms": [
            {"parent": "a", "child": "b", "time": 0.0,
             "translation": [0, 0, 0], "rotation_xyzw": [0, 0, 0, 1]},
            {"parent": "c", "child": "b", "time": 0.0,
             "translation": [0, 0, 0], "rotation_xyzw": [0, 0, 0, 1]},
        ],
        "queries": [],
    }
    resp = handle_request(bad)
    assert resp["ok"] is False
    assert resp["error"]["type"] == "MultiParentError"


def test_cli_roundtrip(tmp_path: Path):
    req_file = tmp_path / "req.json"
    req_file.write_text(json.dumps(REQUEST), encoding="utf-8")
    proc = subprocess.run(
        [sys.executable, "-m", "transform_tree", str(req_file)],
        capture_output=True, text=True, check=False,
    )
    assert proc.returncode == 0
    resp = json.loads(proc.stdout)
    assert resp["ok"] is True
    assert resp["results"][0]["ok"] is True


def test_cli_bad_json_exits_nonzero(tmp_path: Path):
    req_file = tmp_path / "bad.json"
    req_file.write_text("{not json", encoding="utf-8")
    proc = subprocess.run(
        [sys.executable, "-m", "transform_tree", str(req_file)],
        capture_output=True, text=True, check=False,
    )
    assert proc.returncode == 1
    assert json.loads(proc.stdout)["ok"] is False
