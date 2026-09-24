"""FastAPI 端到端测试：审计、验签、校验错误、MC 自检。"""

import numpy as np
import pytest
from fastapi.testclient import TestClient

from app.main import app
from app.se3 import se3_exp


@pytest.fixture(scope="module")
def client():
    with TestClient(app) as c:
        yield c


def _edge_json(eid, p, ch, T, C=None, version="v1.0"):
    return {
        "id": eid,
        "parent": p,
        "child": ch,
        "transform": {
            "translation": T[:3, 3].tolist(),
            "rotation": T[:3, :3].tolist(),
        },
        "covariance": None if C is None else C.tolist(),
        "version": version,
    }


def _triangle(C, close=True, versions=("v1.0", "v1.0", "v1.0"), extra=None):
    Tab = se3_exp(np.array([1.0, 0, 0, 0, 0, 0]))
    Tbc = se3_exp(np.array([0.0, 1, 0, 0, 0, 0]))
    tx = -1.0 if close else -1.02
    Tca = se3_exp(np.array([tx, -1.0, 0, 0, 0, 0]))
    edges = [
        _edge_json("ab", "A", "B", Tab, C, versions[0]),
        _edge_json("bc", "B", "C", Tbc, C, versions[1]),
        _edge_json("ca", "C", "A", Tca, C, versions[2]),
    ]
    if extra:
        edges += extra
    return edges


def _body(edges, **kw):
    return {
        "root_frame": "A",
        "convention": "right",
        "edges": edges,
        "correlation_policy": "independent",
        **kw,
    }


def test_health(client):
    r = client.get("/health")
    assert r.status_code == 200
    assert r.json()["status"] == "ok"
    assert r.json()["key_id"].startswith("ed25519-")


def test_audit_closed_loop_and_signature(client):
    C = np.diag([1e-8] * 6)
    r = client.post("/audit", json=_body(_triangle(C)))
    assert r.status_code == 200
    env = r.json()
    result = env["result"]
    assert result["overall"]["status"] == "ok"
    loop = result["loops"][0]
    assert not loop["conflict"] and loop["p_value"] > 0.01
    # 签名信封完整
    assert env["algorithm"] == "ed25519"
    assert len(env["signature"]) > 0
    v = client.post(
        "/verify",
        json={
            "public_key": env["public_key"],
            "signature": env["signature"],
            "result": result,
        },
    )
    assert v.json()["valid"] is True


def test_tampered_signature_rejected(client):
    C = np.diag([1e-8] * 6)
    env = client.post("/audit", json=_body(_triangle(C))).json()
    tampered = dict(env["result"])
    tampered["root_frame"] = "FAKE"
    r = client.post(
        "/verify",
        json={
            "public_key": env["public_key"],
            "signature": env["signature"],
            "result": tampered,
        },
    )
    assert r.status_code == 400 and r.json()["valid"] is False


def test_loop_conflict(client):
    C = np.diag([1e-8] * 6)
    r = client.post("/audit", json=_body(_triangle(C, close=False)))
    result = r.json()["result"]
    assert result["overall"]["status"] == "conflict"
    loop = result["loops"][0]
    assert loop["conflict"] and loop["p_value"] < 0.01


def test_illegal_rotation_422(client):
    C = np.diag([1e-8] * 6)
    bad = np.eye(4)
    bad[:3, :3] = np.eye(3) * 2
    body = _body([_edge_json("ab", "A", "B", bad, C)])
    r = client.post("/audit", json=body)
    assert r.status_code == 422
    codes = {i["code"] for i in r.json()["error"]["issues"]}
    assert "ILLEGAL_ROTATION" in codes


def test_non_psd_covariance_422(client):
    C = np.diag([1e-8] * 6)
    C[0, 0] = -1e-7
    Tab = se3_exp(np.array([1.0, 0, 0, 0, 0, 0]))
    r = client.post("/audit", json=_body([_edge_json("ab", "A", "B", Tab, C)]))
    assert r.status_code == 422
    issue = r.json()["error"]["issues"][0]
    assert issue["code"] == "COVARIANCE_NOT_PSD"
    assert issue["evidence_path"] == ["edge:ab"]
    assert issue["details"]["eigenvalue_min"] < 0


def test_missing_covariance_no_false_conflict(client):
    C = np.diag([1e-8] * 6)
    Tab = se3_exp(np.array([1.0, 0, 0, 0, 0, 0]))
    Tbc = se3_exp(np.array([0.0, 1, 0, 0, 0, 0]))
    Tca = se3_exp(np.array([-1.1, -1.0, 0, 0, 0, 0]))  # 10cm 误差
    edges = [
        _edge_json("ab", "A", "B", Tab, C),
        _edge_json("bc", "B", "C", Tbc, None),  # 缺失
        _edge_json("ca", "C", "A", Tca, C),
    ]
    result = client.post("/audit", json=_body(edges)).json()["result"]
    loop = result["loops"][0]
    assert loop["covariance_status"] == "unknown"
    assert loop["p_value"] is None and not loop["conflict"]
    assert result["overall"]["n_loops_untestable_missing_covariance"] == 1


def test_correlation_policy_required(client):
    C = np.diag([1e-8] * 6)
    body = _body(_triangle(C))
    del body["correlation_policy"]
    assert client.post("/audit", json=body).status_code == 422


def test_bounded_requires_rho(client):
    C = np.diag([1e-8] * 6)
    r = client.post(
        "/audit", json=_body(_triangle(C), correlation_policy="bounded")
    )
    assert r.status_code == 422


def test_explicit_requires_unspecified_policy(client):
    C = np.diag([1e-8] * 6)
    r = client.post(
        "/audit",
        json=_body(_triangle(C), correlation_policy="explicit"),
    )
    assert r.status_code == 422


def test_bounded_is_conservative(client):
    C = np.diag([1e-8] * 6)
    edges = _triangle(C)
    p_ind = client.post("/audit", json=_body(edges)).json()["result"]["loops"][0]["p_value"]
    p_b1 = client.post(
        "/audit", json=_body(edges, correlation_policy="bounded", rho_max=1.0)
    ).json()["result"]["loops"][0]["p_value"]
    # 更大的协方差 -> 更不显著（p 更大），同样残差更难判冲突
    assert p_b1 >= p_ind


def test_explicit_cross_covariance(client):
    C = np.diag([1e-8] * 6)
    edges = _triangle(C)
    body = _body(
        edges,
        correlation_policy="explicit",
        unspecified_policy="independent",
        cross_covariances=[
            {"edge_a": "ab", "edge_b": "bc", "matrix": (np.eye(6) * 1e-9).tolist()}
        ],
    )
    r = client.post("/audit", json=body)
    assert r.status_code == 200
    decl = r.json()["result"]["correlation_declaration"]
    assert ["ab", "bc"] in decl["explicit_pairs"]
    # 其余两对被显式列为独立（不沉默）
    assert decl["declared_independent_pairs"]


def test_joint_non_psd_cross_covariance_rejected(client):
    C = np.diag([1e-8] * 6)
    edges = _triangle(C)
    # 互协方差远大于对角块（相关系数远超 1）-> 联合矩阵不定
    huge = (np.eye(6) * 1e-3).tolist()
    body = _body(
        edges,
        correlation_policy="explicit",
        unspecified_policy="independent",
        cross_covariances=[
            {"edge_a": "ab", "edge_b": "bc", "matrix": huge}
        ],
    )
    r = client.post("/audit", json=body)
    assert r.status_code == 422
    codes = {i["code"] for i in r.json()["error"]["issues"]}
    assert "JOINT_COVARIANCE_NOT_PSD" in codes
    issue = next(i for i in r.json()["error"]["issues"] if i["code"] == "JOINT_COVARIANCE_NOT_PSD")
    assert issue["evidence_path"]  # 边组证据
    assert issue["details"]["eigenvalue_min"] < 0


def test_version_mismatch_reported(client):
    C = np.diag([1e-8] * 6)
    edges = _triangle(C, versions=("v1", "v2", "v1"))
    result = client.post("/audit", json=_body(edges)).json()["result"]
    assert result["overall"]["n_version_mismatches"] >= 1
    mm = result["version_mismatches"][0]
    assert set(mm["versions"]) == {"v1", "v2"}
    assert mm["evidence_path"]  # 带证据


def test_duplicate_edge_ids_rejected(client):
    C = np.diag([1e-8] * 6)
    edges = _triangle(C)
    edges[1]["id"] = "ab"  # 重复
    assert client.post("/audit", json=_body(edges)).status_code == 422


def test_unknown_root(client):
    C = np.diag([1e-8] * 6)
    body = _body(_triangle(C), root_frame="Z")
    r = client.post("/audit", json=body)
    assert r.status_code == 422
    assert any(i["code"] == "UNKNOWN_ROOT" for i in r.json()["error"]["issues"])


def test_montecarlo_selfcheck(client):
    r = client.post("/montecarlo/selfcheck?n_samples=6000&seed=1")
    assert r.status_code == 200
    body = r.json()
    assert body["summary"]["passed"] is True
    names = {s["scenario"] for s in body["scenarios"]}
    assert {"small_angle_right", "missing_covariance", "large_noise_breakdown"} <= names
    # 缺失协方差场景必须 unknown
    miss = next(s for s in body["scenarios"] if s["scenario"] == "missing_covariance")
    assert miss["covariance_status"] == "unknown"
