"""JSON 解析、CLI 端到端、合成样例的测试。"""

import json
import subprocess
import sys
from pathlib import Path

import numpy as np
import pytest

from transform_tree.errors import (
    CycleDetectedError,
    InvalidRequestError,
    MultipleParentsError,
)
from transform_tree.json_io import build_tree, parse_pose
from transform_tree.main import process_request
from transform_tree.synthetic import build_demo_request

ROOT = Path(__file__).resolve().parent.parent


def test_parse_pose_defaults_to_identity():
    t = parse_pose({}, "x")
    np.testing.assert_allclose(t.to_matrix(), np.eye(4))


def test_parse_pose_axis_angle_matches_rotation_matrix():
    spec = {
        "translation": [1, 2, 3],
        "rotation": {"axis_angle": {"axis": [0, 0, 2], "angle": np.pi / 2}},  # 非单位轴应被归一化
    }
    t = parse_pose(spec, "x")
    rz90 = np.array([[0, -1, 0], [1, 0, 0], [0, 0, 1]], dtype=float)
    np.testing.assert_allclose(t.rotation, rz90, atol=1e-12)
    np.testing.assert_allclose(t.translation, [1, 2, 3])


def test_parse_pose_rejects_multiple_rotation_forms():
    with pytest.raises(InvalidRequestError):
        parse_pose({"rotation": {"quaternion": [1, 0, 0, 0], "matrix": np.eye(3).tolist()}}, "x")


def test_parse_pose_rejects_bad_vec_length():
    with pytest.raises(InvalidRequestError):
        parse_pose({"translation": [1, 2]}, "x")


def test_build_tree_rejects_unknown_edge_type():
    req = {
        "edges": [
            {"parent": "a", "child": "b", "type": "weird", "keyframes": []}
        ]
    }
    with pytest.raises(InvalidRequestError):
        build_tree(req)


def test_build_tree_detects_multiple_parents_in_json():
    req = {
        "edges": [
            {"parent": "a", "child": "b", "type": "static", "transform": {}},
            {"parent": "c", "child": "b", "type": "static", "transform": {}},
        ]
    }
    with pytest.raises(MultipleParentsError):
        build_tree(req)


def test_build_tree_detects_cycle_in_json():
    req = {
        "edges": [
            {"parent": "a", "child": "b", "type": "static", "transform": {}},
            {"parent": "b", "child": "c", "type": "static", "transform": {}},
            {"parent": "c", "child": "a", "type": "static", "transform": {}},
        ]
    }
    with pytest.raises(CycleDetectedError):
        build_tree(req)


def test_demo_request_results_match_expectations():
    response = process_request(build_demo_request())
    by_q = {(r["query"]["source"], r["query"]["target"], r["query"]["time"]): r for r in response["results"] if r["ok"]}
    errors = {r["query"]["time"]: r["error"]["type"] for r in response["results"] if not r["ok"]}

    # 查询 1：tool -> base @ t=2.0：三层链的逆变换，角 theta1=0.2, theta2=0，
    # 沿 z 总平移 0.10+0.20+0.15=0.45；逆方向为 -0.45。
    r1 = by_q[("tool", "base", 2.0)]
    assert r1["chain"] == ["tool", "link2", "link1", "base"]
    np.testing.assert_allclose(r1["transform"]["translation"], [0, 0, -0.45], atol=1e-12)
    # 逆链旋转为 Rz(-0.2)。
    angle = np.arctan2(r1["transform"]["rotation_matrix"][1][0], r1["transform"]["rotation_matrix"][0][0])
    assert angle == pytest.approx(-0.2, abs=1e-10)

    # 查询 2：base -> tool @ t=1.25：插值角 0.125 rad。
    r2 = by_q[("base", "tool", 1.25)]
    angle2 = np.arctan2(r2["transform"]["rotation_matrix"][1][0], r2["transform"]["rotation_matrix"][0][0])
    assert angle2 == pytest.approx(0.125, abs=1e-10)

    # 查询 3：sensor -> tool @ t=0。
    r3 = by_q[("sensor", "tool", 0.0)]
    assert r3["chain"] == ["sensor", "base", "link1", "link2", "tool"]

    # 错误查询。
    assert errors[5.0] == "TimeGapError"
    assert errors[-0.1] == "TimeNotCoveredError"
    assert errors[10.5] == "TimeNotCoveredError"
    # 未知帧在错误条目里。
    unknown = next(r for r in response["results"] if not r["ok"] and r["query"]["target"] == "gripper")
    assert unknown["error"]["type"] == "UnknownFrameError"

    # 存在失败查询，整体 ok=False。
    assert response["ok"] is False


def test_cli_end_to_end_with_sample_file(tmp_path: Path):
    result_path = tmp_path / "result.json"
    proc = subprocess.run(
        [
            sys.executable,
            "-m",
            "transform_tree",
            str(ROOT / "examples" / "request.json"),
            "-o",
            str(result_path),
        ],
        cwd=ROOT,
        env={**__import__("os").environ, "PYTHONPATH": str(ROOT / "src")},
        capture_output=True,
        text=True,
    )
    # 样例中故意包含失败查询（缺口/越界/未知帧），退出码应为 1。
    assert proc.returncode == 1, proc.stderr
    data = json.loads(result_path.read_text(encoding="utf-8"))
    assert data["frame_count"] == 5
    assert data["edge_count"] == 4
    assert len(data["results"]) == 7


def test_cli_stdin_and_stdout():
    req = {
        "edges": [
            {
                "parent": "a",
                "child": "b",
                "type": "static",
                "transform": {"translation": [1, 0, 0]},
            }
        ],
        "queries": [{"source": "b", "target": "a", "time": 0.0}],
    }
    proc = subprocess.run(
        [sys.executable, "-m", "transform_tree", "-"],
        cwd=ROOT,
        input=json.dumps(req),
        env={**__import__("os").environ, "PYTHONPATH": str(ROOT / "src")},
        capture_output=True,
        text=True,
    )
    assert proc.returncode == 0, proc.stderr
    data = json.loads(proc.stdout)
    assert data["ok"] is True
    np.testing.assert_allclose(data["results"][0]["transform"]["translation"], [-1, 0, 0], atol=1e-12)


def test_cli_malformed_json_exits_2():
    proc = subprocess.run(
        [sys.executable, "-m", "transform_tree", "-"],
        cwd=ROOT,
        input="{not json",
        env={**__import__("os").environ, "PYTHONPATH": str(ROOT / "src")},
        capture_output=True,
        text=True,
    )
    assert proc.returncode == 2
    data = json.loads(proc.stdout)
    assert data["error"]["type"] == "InvalidRequestError"
