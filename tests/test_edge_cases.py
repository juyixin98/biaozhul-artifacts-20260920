"""边界与错误分支补充测试：把覆盖率补到 80% 以上，同时锁定输入校验行为。"""

import json
from pathlib import Path

import numpy as np
import pytest

from transform_tree.errors import (
    InvalidKeyframeError,
    InvalidRequestError,
    InvalidTransformError,
    UnknownFrameError,
)
from transform_tree.json_io import (
    build_tree,
    load_request,
    parse_pose,
    parse_query,
    parse_rotation,
)
from transform_tree.main import main, process_request
from transform_tree.timed_sequence import (
    Keyframe,
    StaticTransformProvider,
    TimedTransformSequence,
    interpolate,
)
from transform_tree.transform import Transform
from transform_tree.tree import TransformTree


# ---------- Transform 未覆盖分支 ----------


def test_from_matrix_rejects_bad_shape():
    with pytest.raises(InvalidTransformError):
        Transform.from_matrix(np.eye(3))


def test_from_matrix_rejects_non_finite():
    m = np.eye(4)
    m[0, 3] = np.inf
    with pytest.raises(InvalidTransformError):
        Transform.from_matrix(m)


def test_from_matrix_rejects_bad_last_row():
    m = np.eye(4)
    m[3] = [1, 0, 0, 1]
    with pytest.raises(InvalidTransformError):
        Transform.from_matrix(m)


def test_bad_rotation_shape_rejected():
    with pytest.raises(InvalidTransformError):
        Transform(np.eye(2), np.zeros(3))


def test_quaternion_conversion_all_shepperd_branches():
    # 迹为负时的三个分支：构造主轴旋转并往返转换。
    angles_axes = [
        (np.pi, [1, 0, 0]),
        (np.pi, [0, 1, 0]),
        (np.pi, [0, 0, 1]),
        (2.0, [1, 1, 0]),
    ]
    for angle, axis in angles_axes:
        ax = np.asarray(axis, dtype=float)
        ax /= np.linalg.norm(ax)
        q = np.concatenate([[np.cos(angle / 2)], np.sin(angle / 2) * ax])
        t = Transform.from_quaternion(q)
        q_back = t.to_quaternion()
        np.testing.assert_allclose(Transform.from_quaternion(q_back).rotation, t.rotation, atol=1e-10)
        assert q_back[0] >= 0.0


def test_repr_and_equality_with_non_transform():
    t = Transform.identity()
    assert "Transform" in repr(t)
    assert (t == 42) is False  # type: ignore[comparison-overlap]


def test_matmul_operator_matches_multiply():
    a = Transform.from_quaternion([np.cos(0.2), 0, 0, np.sin(0.2)], [1, 0, 0])
    b = Transform.from_quaternion([np.cos(0.3), 0, np.sin(0.3), 0], [0, 1, 0])
    assert (a @ b).is_close(a.multiply(b))


# ---------- TimedTransformSequence 未覆盖分支 ----------


def test_sequence_properties_and_covers():
    seq = TimedTransformSequence(
        [Keyframe(1.0, Transform.identity()), Keyframe(3.0, Transform.identity())], max_gap=2.5
    )
    assert seq.start_time == 1.0
    assert seq.end_time == 3.0
    assert seq.max_gap == 2.5
    assert seq.covers(2.0) and seq.covers(1.0) and seq.covers(3.0)
    assert not seq.covers(0.9) and not seq.covers(3.2)
    assert len(seq.timestamps) == 2


def test_sequence_rejects_non_positive_max_gap():
    with pytest.raises(InvalidKeyframeError):
        TimedTransformSequence([Keyframe(0.0, Transform.identity())], max_gap=0.0)


def test_sequence_lookup_rejects_non_finite_time():
    seq = TimedTransformSequence([Keyframe(0.0, Transform.identity())])
    with pytest.raises(InvalidKeyframeError):
        seq.lookup(np.nan)


def test_static_provider_rejects_non_finite_time():
    provider = StaticTransformProvider(Transform.identity())
    with pytest.raises(InvalidKeyframeError):
        provider.lookup(np.inf)


def test_interpolate_rejects_alpha_out_of_range():
    t = Transform.identity()
    with pytest.raises(InvalidKeyframeError):
        interpolate(t, t, 1.1)


# ---------- TransformTree 未覆盖方法 ----------


def _two_frame_tree_static():
    tree = TransformTree()
    tree.add_edge("a", "b", StaticTransformProvider(Transform(np.eye(3), np.array([1.0, 0, 0]))))
    return tree


def test_tree_has_frame_and_frames_snapshot():
    tree = _two_frame_tree_static()
    assert tree.has_frame("a") and tree.has_frame("b")
    assert not tree.has_frame("c")
    snapshot = tree.frames
    snapshot.add("c")  # 改快照不应影响内部状态
    assert not tree.has_frame("c")


def test_edges_property_reflects_provider():
    tree = _two_frame_tree_static()
    edges = tree.edges
    assert len(edges) == 1
    assert edges[0].parent == "a" and edges[0].child == "b"
    np.testing.assert_allclose(edges[0].provider.lookup(0.0).translation, [1, 0, 0])


def test_lookup_edge_transform():
    tree = _two_frame_tree_static()
    t = tree.lookup_edge_transform("a", "b", 9.0)
    np.testing.assert_allclose(t.translation, [1, 0, 0])
    with pytest.raises(UnknownFrameError):
        tree.lookup_edge_transform("b", "a", 9.0)


def test_coverage_intersection_dynamic_and_static():
    tree = TransformTree()
    tree.add_edge(
        "a",
        "b",
        TimedTransformSequence([Keyframe(0.0, Transform.identity()), Keyframe(10.0, Transform.identity())]),
    )
    tree.add_edge(
        "b",
        "c",
        TimedTransformSequence([Keyframe(2.0, Transform.identity()), Keyframe(8.0, Transform.identity())]),
    )
    tree.add_edge("a", "s", StaticTransformProvider(Transform.identity()))
    assert tree.coverage_intersection() == (2.0, 8.0)
    # 只选静态边时返回 None（静态不限制时间）。
    assert tree.coverage_intersection(["s"]) is None
    # 只选一棵动态子树。
    assert tree.coverage_intersection(["b"]) == (0.0, 10.0)


def test_add_frame_rejects_blank_name():
    tree = TransformTree()
    with pytest.raises(UnknownFrameError):
        tree.add_frame("")


# ---------- json_io 校验分支 ----------


def test_build_tree_root_must_be_object():
    with pytest.raises(InvalidRequestError):
        build_tree([1, 2, 3])  # type: ignore[arg-type]


def test_build_tree_edges_must_be_list():
    with pytest.raises(InvalidRequestError):
        build_tree({"edges": {}})


def test_build_tree_missing_parent():
    with pytest.raises(InvalidRequestError):
        build_tree({"edges": [{"child": "b", "type": "static", "transform": {}}]})


def test_build_tree_parent_must_be_string():
    with pytest.raises(InvalidRequestError):
        build_tree({"edges": [{"parent": 1, "child": "b", "type": "static", "transform": {}}]})


def test_build_tree_frames_names_must_be_strings():
    with pytest.raises(InvalidRequestError):
        build_tree({"frames": [3]})


def test_build_tree_bad_default_max_gap():
    with pytest.raises(InvalidRequestError):
        build_tree({"default_max_gap": -1, "edges": []})


def test_build_tree_bad_edge_max_gap():
    with pytest.raises(InvalidRequestError):
        build_tree(
            {"edges": [{"parent": "a", "child": "b", "type": "static", "transform": {}, "max_gap": "x"}]}
        )


def test_build_tree_timed_requires_keyframes():
    with pytest.raises(InvalidRequestError):
        build_tree({"edges": [{"parent": "a", "child": "b", "type": "timed"}]})


def test_build_tree_keyframes_must_be_nonempty_list():
    with pytest.raises(InvalidRequestError):
        build_tree({"edges": [{"parent": "a", "child": "b", "type": "timed", "keyframes": []}]})


def test_build_tree_keyframe_time_must_be_finite():
    with pytest.raises(InvalidRequestError):
        build_tree(
            {"edges": [{"parent": "a", "child": "b", "type": "timed", "keyframes": [{"t": "now"}]}]}
        )


def test_parse_rotation_non_dict():
    with pytest.raises(InvalidRequestError):
        parse_rotation([1, 0, 0, 0], "ctx")


def test_parse_rotation_unknown_kind():
    with pytest.raises(InvalidRequestError):
        parse_rotation({"euler": [0, 0, 0]}, "ctx")


def test_parse_rotation_quaternion_bad_length():
    with pytest.raises(InvalidRequestError):
        parse_rotation({"quaternion": [1, 0, 0]}, "ctx")


def test_parse_rotation_matrix_bad_shape():
    with pytest.raises(InvalidRequestError):
        parse_rotation({"matrix": [[1, 0], [0, 1]]}, "ctx")


def test_parse_rotation_axis_angle_missing_fields():
    with pytest.raises(InvalidRequestError):
        parse_rotation({"axis_angle": {"axis": [0, 0, 1]}}, "ctx")


def test_parse_rotation_axis_angle_bad_angle():
    with pytest.raises(InvalidRequestError):
        parse_rotation({"axis_angle": {"axis": [0, 0, 1], "angle": "x"}}, "ctx")


def test_parse_rotation_axis_angle_zero_axis():
    with pytest.raises(InvalidTransformError):
        parse_rotation({"axis_angle": {"axis": [0, 0, 0], "angle": 1.0}}, "ctx")


def test_parse_pose_must_be_object():
    with pytest.raises(InvalidRequestError):
        parse_pose([1, 2], "ctx")


def test_parse_pose_bad_translation_length():
    with pytest.raises(InvalidRequestError):
        parse_pose({"translation": [1, 2]}, "ctx")


def test_parse_query_validation():
    with pytest.raises(InvalidRequestError):
        parse_query({"source": "a", "target": "b", "time": "x"}, 0, None)
    with pytest.raises(InvalidRequestError):
        parse_query({"source": 1, "target": "b", "time": 0.0}, 0, None)
    with pytest.raises(InvalidRequestError):
        parse_query("not-a-dict", 0, None)
    parsed = parse_query({"source": "a", "target": "b", "time": 1, "max_gap": 0.2}, 0, 0.9)
    assert parsed == {"source": "a", "target": "b", "time": 1.0, "max_gap": 0.2}
    parsed_default = parse_query({"source": "a", "target": "b", "time": 1}, 0, 0.9)
    assert parsed_default["max_gap"] == 0.9
    with pytest.raises(InvalidRequestError):
        parse_query({"source": "a", "target": "b", "time": 1, "max_gap": 0}, 0, None)


def test_load_request_missing_file(tmp_path: Path):
    with pytest.raises(InvalidRequestError):
        load_request(str(tmp_path / "nope.json"))


def test_load_request_bad_json(tmp_path: Path):
    p = tmp_path / "bad.json"
    p.write_text("{oops", encoding="utf-8")
    with pytest.raises(InvalidRequestError):
        load_request(str(p))


# ---------- main() 进程内直接调用（覆盖 CLI 主体） ----------


def test_main_file_to_stdout(capsys, tmp_path: Path):
    req = {
        "edges": [
            {"parent": "a", "child": "b", "type": "static", "transform": {"translation": [1, 0, 0]}}
        ],
        "queries": [{"source": "a", "target": "b", "time": 0.0}],
    }
    src = tmp_path / "req.json"
    src.write_text(json.dumps(req), encoding="utf-8")
    code = main([str(src)])
    assert code == 0
    out = json.loads(capsys.readouterr().out)
    assert out["results"][0]["ok"] is True


def test_main_failed_query_returns_1(tmp_path: Path):
    req = {
        "edges": [
            {
                "parent": "a",
                "child": "b",
                "type": "timed",
                "keyframes": [{"t": 0.0, "translation": [0, 0, 0]}],
            }
        ],
        "queries": [{"source": "a", "target": "b", "time": 5.0}],
    }
    src = tmp_path / "req.json"
    src.write_text(json.dumps(req), encoding="utf-8")
    assert main([str(src), "-o", str(tmp_path / "out.json")]) == 1


def test_main_build_failure_returns_2(tmp_path: Path):
    src = tmp_path / "bad.json"
    src.write_text("{broken", encoding="utf-8")
    assert main([str(src)]) == 2


def test_process_request_rejects_non_list_queries():
    with pytest.raises(InvalidRequestError):
        process_request({"queries": {}})
