"""FastAPI 接口测试：TestClient 同步请求 + httpx AsyncClient 异步并发采样。"""

from __future__ import annotations

import asyncio

import numpy as np
import pytest
from fastapi.testclient import TestClient
from scipy.spatial.transform import Rotation

from tf_cache import app as app_module
from tf_cache.app import app
from tf_cache.se3 import SE3Transform


@pytest.fixture(autouse=True)
def _clear_tree():
    # 每个用例前清空全局树
    app_module.tree._edges.clear()
    app_module.tree._edge_orientation.clear()
    app_module.tree._frames.clear()
    yield


@pytest.fixture
def client():
    return TestClient(app)


def qz(angle_deg: float) -> list[float]:
    return Rotation.from_euler("z", angle_deg, degrees=True).as_quat().tolist()


def _add(client, parent, child, t, translation=(0, 0, 0), quaternion=None):
    quaternion = quaternion or [0, 0, 0, 1]
    return client.post(
        "/transforms",
        json={
            "parent": parent,
            "child": child,
            "t": t,
            "translation": list(translation),
            "quaternion": quaternion,
        },
    )


# --------------------------------------------------------------------------
# 基本读写
# --------------------------------------------------------------------------
def test_health(client):
    r = client.get("/health")
    assert r.status_code == 200
    assert r.json() == {"status": "ok"}


def test_add_and_lookup_90deg(client):
    r = _add(client, "world", "gripper", 0.0, (1, 2, 3), qz(90))
    assert r.status_code == 201, r.text
    # child->parent 返回写入值：点 [1,0,0]_gripper 在 world 中为 [0,1,0]+t
    r = client.post("/lookup", json={"source": "gripper", "target": "world", "t": 0.0})
    assert r.status_code == 200, r.text
    body = r.json()
    m = np.array(body["matrix"])
    p = m[:3, :3] @ np.array([1.0, 0, 0]) + m[:3, 3]
    np.testing.assert_allclose(p, [1, 3, 3], atol=1e-9)
    np.testing.assert_allclose(body["translation"], [1, 2, 3], atol=1e-12)

    # parent->child 返回逆变换：平移为 -R^T·t = [-2,1,-3]
    r = client.post("/lookup", json={"source": "world", "target": "gripper", "t": 0.0})
    assert r.status_code == 200, r.text
    np.testing.assert_allclose(
        r.json()["translation"], [-2, 1, -3], atol=1e-10
    )


def test_tree_endpoint_lists_frames_and_edges(client):
    _add(client, "w", "a", 0.0)
    _add(client, "w", "a", 1.0, (5, 5, 5))
    _add(client, "a", "b", 0.0)
    r = client.get("/tree")
    assert r.status_code == 200
    info = r.json()
    assert info["frames"] == ["a", "b", "w"]
    edges = {(e["parent"], e["child"]): e for e in info["edges"]}
    assert edges[("w", "a")]["samples"] == 2
    assert edges[("w", "a")]["t_min"] == 0.0
    assert edges[("w", "a")]["t_max"] == 1.0


def test_path_endpoint(client):
    _add(client, "w", "a", 0.0)
    _add(client, "a", "b", 0.0)
    r = client.get("/path/w/b")
    assert r.status_code == 200
    assert r.json()["path"] == ["w", "a", "b"]


# --------------------------------------------------------------------------
# 多边组合 + 逆变换数值误差（通过 HTTP）
# --------------------------------------------------------------------------
def test_compose_then_inverse_over_http(client):
    _add(client, "w", "a", 0.5, (1, 0, 0), qz(30))
    _add(client, "a", "b", 0.5, (0, 2, 0), qz(60))
    _add(client, "b", "c", 0.5, (0, 0, -1), qz(-45))

    r1 = client.post("/lookup", json={"source": "c", "target": "w", "t": 0.5})
    r2 = client.post("/lookup", json={"source": "w", "target": "c", "t": 0.5})
    assert r1.status_code == r2.status_code == 200
    m1 = np.array(r1.json()["matrix"])
    m2 = np.array(r2.json()["matrix"])
    residual = m1 @ m2
    err = np.linalg.norm(residual - np.eye(4))
    assert err < 1e-10, f"HTTP 组合后逆变换残差 {err}"


# --------------------------------------------------------------------------
# 错误语义
# --------------------------------------------------------------------------
def test_cycle_returns_409(client):
    _add(client, "w", "a", 0.0)
    _add(client, "a", "b", 0.0)
    r = _add(client, "b", "w", 0.0)
    assert r.status_code == 409
    assert "环" in r.json()["detail"]


def test_missing_frame_returns_404(client):
    _add(client, "w", "a", 0.0)
    r = client.post("/lookup", json={"source": "w", "target": "ghost", "t": 0.0})
    assert r.status_code == 404


def test_disconnected_returns_404(client):
    _add(client, "w", "a", 0.0)
    _add(client, "x", "y", 0.0)
    r = client.post("/lookup", json={"source": "w", "target": "y", "t": 0.0})
    assert r.status_code == 404
    assert "连通链" in r.json()["detail"]


def test_extrapolation_returns_400(client):
    _add(client, "w", "a", 0.0)
    _add(client, "w", "a", 1.0)
    before = client.post("/lookup", json={"source": "w", "target": "a", "t": -0.1})
    after = client.post("/lookup", json={"source": "w", "target": "a", "t": 1.1})
    assert before.status_code == after.status_code == 400
    assert "外推" in before.json()["detail"]


def test_duplicate_timestamp_returns_409(client):
    _add(client, "w", "a", 0.0)
    r = _add(client, "w", "a", 0.0, (9, 9, 9))
    assert r.status_code == 409


def test_nonunit_quaternion_returns_422(client):
    r = _add(client, "w", "a", 0.0, (0, 0, 0), [2, 0, 0, 0])
    assert r.status_code == 422
    assert "单位四元数" in r.json()["detail"]


def test_malformed_body_returns_422(client):
    r = client.post(
        "/transforms",
        json={"parent": "w", "child": "a", "t": 0.0, "translation": [0, 0]},
    )
    assert r.status_code == 422


# --------------------------------------------------------------------------
# 批量写入与异步并发采样
# --------------------------------------------------------------------------
def test_batch_edge_add_then_interpolate(client):
    samples = [
        {"t": 0.0, "translation": [0, 0, 0], "quaternion": [0, 0, 0, 1]},
        {"t": 1.0, "translation": [2, 4, 6], "quaternion": qz(90)},
    ]
    r = client.post(
        "/transforms/batch", json={"parent": "w", "child": "a", "samples": samples}
    )
    assert r.status_code == 201, r.text
    r = client.post("/lookup", json={"source": "a", "target": "w", "t": 0.5})
    assert r.status_code == 200
    np.testing.assert_allclose(r.json()["translation"], [1, 2, 3], atol=1e-12)

def test_batch_lookup_raise_and_null_modes(client):
    _add(client, "w", "a", 0.0, (0, 0, 0))
    _add(client, "w", "a", 1.0, (1, 1, 1))

    r = client.post(
        "/lookup/batch",
        json={"source": "w", "target": "a", "times": [0.0, 0.5, 2.0], "on_error": "raise"},
    )
    assert r.status_code == 400  # 2.0 越界 -> 整体失败

    r = client.post(
        "/lookup/batch",
        json={"source": "w", "target": "a", "times": [0.0, 0.5, 2.0], "on_error": "null"},
    )
    assert r.status_code == 200
    results = r.json()["results"]
    assert results[0] is not None
    assert results[1] is not None
    assert results[2] is None
    # w->a 为逆方向：t=0.5 时逆平移为 [-0.5,-0.5,-0.5]
    np.testing.assert_allclose(results[1]["translation"], [-0.5, -0.5, -0.5], atol=1e-12)
    assert r.json()["path"] == ["w", "a"]


def test_async_concurrent_batch_sampling():
    """使用 httpx AsyncClient 并发打 /lookup/batch，结果须一致。"""
    httpx = pytest.importorskip("httpx")

    samples = [
        {"t": float(i) / 10, "translation": [i / 10, 0, 0], "quaternion": qz(9 * i)}
        for i in range(11)
    ]

    async def scenario():
        transport = httpx.ASGITransport(app=app)
        async with httpx.AsyncClient(transport=transport, base_url="http://tf") as ac:
            r = await ac.post(
                "/transforms/batch", json={"parent": "w", "child": "a", "samples": samples}
            )
            assert r.status_code == 201, r.text

            times = [i / 20 for i in range(21)]
            requests_ = [
                ac.post(
                    "/lookup/batch",
                    json={"source": "w", "target": "a", "times": times},
                )
                for _ in range(16)
            ]
            responses = await asyncio.gather(*requests_)
            return [resp.json() for resp in responses]

    bodies = asyncio.run(scenario())
    first = bodies[0]
    assert len(first["results"]) == 21
    for body in bodies[1:]:
        assert body == first
    # 抽查中点 t=0.5；查询方向 w->a（逆方向）。写入值：t=(0.5,0,0)、Rz(45°)，
    # 逆平移 = -Rz(-45°)·(0.5,0,0) = (-0.5cos45, 0.5sin45, 0)
    c = np.cos(np.pi / 4) * 0.5
    np.testing.assert_allclose(
        first["results"][10]["translation"], [-c, c, 0], atol=1e-9
    )
    # 逆旋转矩阵为标准 Rz(-45°)
    s2 = np.sqrt(2) / 2
    m = np.array(first["results"][10]["matrix"])
    np.testing.assert_allclose(
        m[:3, :3],
        [[s2, s2, 0], [-s2, s2, 0], [0, 0, 1]],
        atol=1e-9,
    )


def test_clear_tree(client):
    _add(client, "w", "a", 0.0)
    assert client.get("/tree").json()["frames"]
    r = client.delete("/tree")
    assert r.status_code == 204
    info = client.get("/tree").json()
    assert info["frames"] == [] and info["edges"] == []
