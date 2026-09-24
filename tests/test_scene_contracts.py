"""全部合成场景的端到端指标契约。

这些阈值同时是 README 中承诺的验收标准。
"""

import pytest

from groundseg import GroundSegParams, evaluate, segment_points
from groundseg import scenes

P = GroundSegParams()


def _run(name):
    pts, truth, _ = scenes.SCENES[name]()
    res = segment_points(pts, P)
    return pts, res, evaluate(res.labels, truth.tolist())


def test_scene_registry_contains_required_difficult_cases():
    # 需求点名的五类困难情形必须齐全
    for name in ("sloped_ground", "ground_with_wall", "sparse_ground",
                 "diffuse_noise", "ground_with_noise", "duplicate_points"):
        assert name in scenes.SCENES


def test_flat_metrics_contract():
    _, _, m = _run("flat_ground")
    assert m["ground"]["precision"] >= 0.99
    assert m["ground"]["recall"] >= 0.99


def test_slope_metrics_contract():
    _, _, m = _run("sloped_ground")
    assert m["ground"]["precision"] >= 0.99
    assert m["ground"]["recall"] >= 0.98
    assert m["non_ground"]["recall"] >= 0.90


def test_steep_slope_abstains():
    pts, res, m = _run("steep_slope")
    assert m["ground"]["undecided_rate"] == pytest.approx(1.0)
    assert res.stats["ground"] == 0


def test_wall_metrics_contract():
    _, _, m = _run("ground_with_wall")
    assert m["ground"]["precision"] >= 0.99
    assert m["ground"]["recall"] >= 0.99
    assert m["non_ground"]["precision"] >= 0.95
    assert m["non_ground"]["recall"] >= 0.90


def test_sparse_abstains():
    pts, res, m = _run("sparse_ground")
    assert m["ground"]["undecided_rate"] == 1.0
    assert res.stats["ground"] == 0


def test_noise_structured_metrics_contract():
    _, _, m = _run("ground_with_noise")
    assert m["ground"]["precision"] >= 0.99
    assert m["ground"]["recall"] >= 0.99
    assert m["non_ground"]["recall"] >= 0.90


def test_pure_noise_abstains():
    pts, res, m = _run("diffuse_noise")
    assert m["ground"]["undecided_rate"] == 1.0
    assert res.stats["ground"] == 0


def test_duplicates_metrics_contract():
    _, _, m = _run("duplicate_points")
    assert m["ground"]["precision"] == 1.0
    assert m["ground"]["recall"] == 1.0
    assert m["ground"]["undecided_rate"] == 0.0
