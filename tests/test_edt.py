"""距离场自动化测试：小图上与暴力参考实现逐格比较。

覆盖：随机地图、并列最近点、非方形格尺寸、地图边界、
全空地图、全障碍地图、单格地图、JSON 入口端到端与请求校验。
"""

from __future__ import annotations

import json
import math
import subprocess
import sys

import numpy as np
import pytest

from edt_field import euclidean_distance_transform
from edt_field.cli import RequestError, compute_response


def brute_force(grid: np.ndarray, dy: float, dx: float) -> np.ndarray:
    """暴力参考：每格枚举全部障碍格取最小欧氏距离。无障碍时为 inf。"""
    rows, cols = grid.shape
    obstacles = np.argwhere(grid)
    result = np.full((rows, cols), math.inf)
    for r in range(rows):
        for c in range(cols):
            for orow, ocol in obstacles:
                dist = math.hypot((r - orow) * dy, (c - ocol) * dx)
                result[r, c] = min(result[r, c], dist)
    return result


def assert_field_matches_brute_force(grid: np.ndarray, dy: float, dx: float) -> None:
    field = euclidean_distance_transform(grid, (dy, dx))
    expected = brute_force(grid, dy, dx)
    rows, cols = grid.shape

    # 距离逐格一致（精确算法 vs 暴力，允许浮点尾差）
    assert np.allclose(
        field.distances, expected, rtol=1e-12, atol=1e-12, equal_nan=False
    ), f"距离不一致:\n{field.distances}\n期望:\n{expected}"

    has_obstacle = bool(grid.any())
    for r in range(rows):
        for c in range(cols):
            sr, sc = field.source_rows[r, c], field.source_cols[r, c]
            if not has_obstacle:
                assert (sr, sc) == (-1, -1)
                continue
            # 来源必须是障碍格
            assert grid[sr, sc], f"({r},{c}) 的来源 ({sr},{sc}) 不是障碍"
            # 来源到本格的距离必须等于所报告的距离（并列时任一最近点均可）
            dist = math.hypot((r - sr) * dy, (c - sc) * dx)
            assert dist == pytest.approx(field.distances[r, c], rel=1e-12, abs=1e-12)


# ---------- 随机小图 vs 暴力参考 ----------


@pytest.mark.parametrize("seed", range(20))
@pytest.mark.parametrize(
    "shape, cell", [((4, 5), (1.0, 1.0)), ((3, 7), (0.5, 2.0)), ((6, 6), (1.5, 0.25))]
)
def test_random_maps_match_brute_force(seed: int, shape: tuple, cell: tuple) -> None:
    rng = np.random.default_rng(seed)
    grid = rng.random(shape) < 0.3
    assert_field_matches_brute_force(grid, cell[0], cell[1])


@pytest.mark.parametrize("density", [0.0, 0.1, 0.5, 0.9, 1.0])
def test_density_sweep_matches_brute_force(density: float) -> None:
    rng = np.random.default_rng(42)
    grid = rng.random((5, 6)) < density
    assert_field_matches_brute_force(grid, 1.0, 1.0)


# ---------- 并列最近点 ----------


def test_tie_between_two_equidistant_obstacles() -> None:
    # 障碍在 (0,0) 与 (0,2)，中点 (0,1) 到两者距离均为 1.0
    grid = np.zeros((1, 3), dtype=bool)
    grid[0, 0] = grid[0, 2] = True
    field = euclidean_distance_transform(grid)
    assert field.distances[0, 1] == pytest.approx(1.0)
    assert (field.source_rows[0, 1], field.source_cols[0, 1]) in {(0, 0), (0, 2)}


def test_tie_four_corners_center() -> None:
    # 四角均为障碍，中心格到四角距离并列
    grid = np.zeros((3, 3), dtype=bool)
    grid[0, 0] = grid[0, 2] = grid[2, 0] = grid[2, 2] = True
    field = euclidean_distance_transform(grid)
    assert field.distances[1, 1] == pytest.approx(math.sqrt(2.0))
    sr, sc = field.source_rows[1, 1], field.source_cols[1, 1]
    assert grid[sr, sc]
    assert_field_matches_brute_force(grid, 1.0, 1.0)


# ---------- 非方形格尺寸 ----------


def test_non_square_cell_size() -> None:
    # 单障碍在原点，dy=2.0, dx=0.5：距离必须按各向异性加权
    grid = np.zeros((3, 4), dtype=bool)
    grid[0, 0] = True
    field = euclidean_distance_transform(grid, (2.0, 0.5))
    assert field.distances[1, 0] == pytest.approx(2.0)   # 行方向一格 = 2.0
    assert field.distances[0, 1] == pytest.approx(0.5)   # 列方向一格 = 0.5
    assert field.distances[2, 3] == pytest.approx(math.hypot(4.0, 1.5))
    assert_field_matches_brute_force(grid, 2.0, 0.5)


def test_non_square_cell_size_picks_truly_nearest() -> None:
    # 格尺寸各向异性时，"格子数更近"不等于"欧氏距离更近"
    # 障碍 A 在 (0,0)，障碍 B 在 (2,3)，考察 (0,3)：到 A 为 3*dx，到 B为 hypot(2*dy,0)=2*dy
    grid = np.zeros((3, 4), dtype=bool)
    grid[0, 0] = grid[2, 3] = True
    # dy=0.1, dx=1.0：到 B 仅 0.2，远小于到 A 的 3.0
    field = euclidean_distance_transform(grid, (0.1, 1.0))
    assert field.distances[0, 3] == pytest.approx(0.2)
    assert (field.source_rows[0, 3], field.source_cols[0, 3]) == (2, 3)


# ---------- 地图边界 ----------


def test_corner_obstacle_to_opposite_corner() -> None:
    grid = np.zeros((4, 5), dtype=bool)
    grid[0, 0] = True
    field = euclidean_distance_transform(grid, (1.0, 1.0))
    assert field.distances[3, 4] == pytest.approx(math.hypot(3.0, 4.0))
    assert (field.source_rows[3, 4], field.source_cols[3, 4]) == (0, 0)


def test_obstacles_only_on_border() -> None:
    grid = np.zeros((5, 5), dtype=bool)
    grid[0, :] = grid[-1, :] = True
    grid[:, 0] = grid[:, -1] = True
    assert_field_matches_brute_force(grid, 0.7, 1.3)


# ---------- 退化地图 ----------


def test_all_free_map() -> None:
    grid = np.zeros((3, 4), dtype=bool)
    field = euclidean_distance_transform(grid)
    assert np.isinf(field.distances).all()
    assert (field.source_rows == -1).all()
    assert (field.source_cols == -1).all()


def test_all_obstacle_map() -> None:
    grid = np.ones((3, 4), dtype=bool)
    field = euclidean_distance_transform(grid, (0.5, 2.0))
    assert (field.distances == 0.0).all()
    # 每格来源是其自身
    rr, cc = np.indices(grid.shape)
    assert (field.source_rows == rr).all()
    assert (field.source_cols == cc).all()


def test_single_cell_maps() -> None:
    free = euclidean_distance_transform(np.zeros((1, 1), dtype=bool))
    assert math.isinf(free.distances[0, 0])
    assert free.source_rows[0, 0] == -1

    occ = euclidean_distance_transform(np.ones((1, 1), dtype=bool))
    assert occ.distances[0, 0] == 0.0
    assert (occ.source_rows[0, 0], occ.source_cols[0, 0]) == (0, 0)


def test_obstacle_cell_distance_zero_source_self() -> None:
    grid = np.zeros((3, 3), dtype=bool)
    grid[1, 1] = True
    field = euclidean_distance_transform(grid)
    assert field.distances[1, 1] == 0.0
    assert (field.source_rows[1, 1], field.source_cols[1, 1]) == (1, 1)


# ---------- 输入校验 ----------


def test_invalid_cell_size_rejected() -> None:
    grid = np.zeros((2, 2), dtype=bool)
    for bad in [(0.0, 1.0), (1.0, -1.0), (math.inf, 1.0)]:
        with pytest.raises(ValueError):
            euclidean_distance_transform(grid, bad)


def test_non_2d_rejected() -> None:
    with pytest.raises(ValueError):
        euclidean_distance_transform(np.zeros((2, 2, 2), dtype=bool))


# ---------- JSON 入口 ----------


def test_compute_response_basic() -> None:
    request = {"grid": [[0, 0, 1], [0, 0, 0]], "cell_size": [1.0, 1.0]}
    resp = compute_response(request)
    assert resp["shape"] == [2, 3]
    assert resp["distances"][0][2] == 0.0
    assert resp["nearest"][0][2] == [0, 2]
    assert resp["distances"][1][0] == pytest.approx(math.sqrt(5.0))
    assert resp["nearest"][1][0] == [0, 2]


def test_compute_response_all_free_uses_null() -> None:
    resp = compute_response({"grid": [[0, 0], [0, 0]]})
    assert resp["distances"] == [[None, None], [None, None]]
    assert resp["nearest"] == [[None, None], [None, None]]
    # 响应必须可被 json.dumps 序列化（无 inf/nan）
    json.dumps(resp)


def test_compute_response_scalar_cell_size() -> None:
    resp = compute_response({"grid": [[1, 0]], "cell_size": 0.5})
    assert resp["cell_size"] == [0.5, 0.5]
    assert resp["distances"][0][1] == pytest.approx(0.5)


@pytest.mark.parametrize(
    "bad_request",
    [
        {},
        {"grid": []},
        {"grid": [[1], []]},
        {"grid": [[1, 0], [1]]},                # 非矩形
        {"grid": [[0, 2], [0, 1]]},             # 非法取值
        {"grid": [[0, 1]], "cell_size": [0, 1]},
        {"grid": [[0, 1]], "cell_size": "big"},
    ],
)
def test_compute_response_rejects_bad_requests(bad_request: dict) -> None:
    with pytest.raises(RequestError):
        compute_response(bad_request)


def test_cli_end_to_end(tmp_path) -> None:
    req_path = tmp_path / "req.json"
    out_path = tmp_path / "resp.json"
    req_path.write_text(
        json.dumps({"grid": [[0, 0, 0], [0, 1, 0]], "cell_size": [0.5, 2.0]}),
        encoding="utf-8",
    )
    proc = subprocess.run(
        [sys.executable, "-m", "edt_field", str(req_path), "-o", str(out_path)],
        capture_output=True,
        text=True,
    )
    assert proc.returncode == 0, proc.stderr
    resp = json.loads(out_path.read_text(encoding="utf-8"))
    assert resp["shape"] == [2, 3]
    assert resp["distances"][0][0] == pytest.approx(math.hypot(0.5, 2.0))
    assert resp["nearest"][0][0] == [1, 1]


def test_cli_rejects_bad_request(tmp_path) -> None:
    req_path = tmp_path / "bad.json"
    req_path.write_text(json.dumps({"grid": [[1, 2]]}), encoding="utf-8")
    proc = subprocess.run(
        [sys.executable, "-m", "edt_field", str(req_path)],
        capture_output=True,
        text=True,
    )
    assert proc.returncode == 2
    assert "请求不合法" in proc.stderr
