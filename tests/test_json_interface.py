"""JSON 接口与 CLI 测试。"""

import json
import subprocess
import sys
from pathlib import Path

import numpy as np

from kalman_missing import run_request

from helpers import cv_model, simulate_cv

ROOT = Path(__file__).resolve().parent.parent


def make_request(steps):
    F, Q, H, R = cv_model()
    return {
        "model": {"F": F.tolist(), "Q": Q.tolist(),
                  "H": H.tolist(), "R": R.tolist()},
        "initial": {"x": [0.0, 0.0], "P": [[10.0, 0.0], [0.0, 10.0]]},
        "steps": steps,
    }


def test_run_request_end_to_end():
    _, zs = simulate_cv(n_steps=50, seed=5)
    steps = [{"z": [z]} for z in zs]
    steps[10] = {"z": [None]}        # 部分缺测（此处即全缺测）
    steps[11] = {"z": None}          # 整步缺测
    steps[12] = None                 # 简写缺测
    resp = run_request(make_request(steps))

    assert resp["status"] == "ok"
    est = resp["estimates"]
    assert len(est) == 50
    assert est[10]["status"] == "predict_only"
    assert est[11]["status"] == "predict_only"
    assert est[12]["status"] == "predict_only"
    assert est[0]["n_observed"] == 1
    # 响应必须可被 JSON 序列化
    json.dumps(resp)
    # 协方差对称且半正定
    for e in est:
        P = np.array(e["P"])
        assert np.max(np.abs(P - P.T)) <= 1e-10
        assert np.linalg.eigvalsh(P)[0] >= -1e-12


def test_run_request_invalid_input_returns_error():
    req = make_request([{"z": [1.0]}])
    req["model"]["Q"] = [[1.0, 2.0], [0.0, 1.0]]   # 非对称 Q
    resp = run_request(req)
    assert resp["status"] == "error"
    assert resp["error"]["code"] == "invalid_input"
    assert "Q" in resp["error"]["message"]

    resp2 = run_request({"model": {}})             # 缺字段
    assert resp2["status"] == "error"


def test_run_request_too_many_steps():
    req = make_request([{"z": [1.0]}] * 10001)
    resp = run_request(req)
    assert resp["status"] == "error"
    assert "步数" in resp["error"]["message"]


def test_cli_roundtrip(tmp_path):
    _, zs = simulate_cv(n_steps=20, seed=6)
    req = make_request([{"z": [z]} for z in zs])
    req_file = tmp_path / "req.json"
    req_file.write_text(json.dumps(req), encoding="utf-8")

    proc = subprocess.run(
        [sys.executable, "-m", "kalman_missing", str(req_file)],
        cwd=ROOT, capture_output=True, text=True,
    )
    assert proc.returncode == 0
    resp = json.loads(proc.stdout)
    assert resp["status"] == "ok"
    assert len(resp["estimates"]) == 20


def test_cli_error_exit_code(tmp_path):
    req = make_request([{"z": [1.0]}])
    req["initial"]["x"] = [0.0]                    # 维数错误
    req_file = tmp_path / "bad.json"
    req_file.write_text(json.dumps(req), encoding="utf-8")

    proc = subprocess.run(
        [sys.executable, "-m", "kalman_missing", str(req_file)],
        cwd=ROOT, capture_output=True, text=True,
    )
    assert proc.returncode == 2
    resp = json.loads(proc.stdout)
    assert resp["status"] == "error"
