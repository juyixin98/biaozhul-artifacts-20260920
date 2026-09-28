"""FastAPI 端到端接口测试（TestClient，全部本地、无外部依赖）。"""

from __future__ import annotations

import pytest
from fastapi.testclient import TestClient

from recomp_scheduler.api import create_app

from .conftest import big_chain_raw, branch_raw, linear_chain_raw


@pytest.fixture
def client(tmp_journal) -> TestClient:
    return TestClient(create_app(tmp_journal))


def test_health(client) -> None:
    body = client.get("/health").json()
    assert body["status"] == "ok"
    assert body["version"] == "1.0.0"


def test_run_returns_concrete_plan_and_checks(client) -> None:
    req = {
        "nodes": linear_chain_raw(),
        "outputs": ["l2"],
        "seed": 77,
        "budget_elements": 100000,
        "finite_difference": True,
    }
    r = client.post("/api/v1/plans/run", json=req)
    assert r.status_code == 200, r.text
    j = r.json()
    assert j["success"] is True
    assert j["run_id"].startswith("step-")
    # 具体结果：该宽松预算下选择零重算的基线（2 块方案在本小图不更优）。
    assert j["plan"]["block_count"] == 1
    assert j["profile"]["recompute_flops"] == 0
    # 运行时高水位必须与静态预测一致。
    assert j["runtime"]["peak_match"] is True
    assert j["runtime"]["predicted_peak"] == j["runtime"]["runtime_peak"]
    # 梯度校验通过，且包含独立有限差分结论。
    assert j["gradient_check"]["passed"] is True
    assert j["gradient_check"]["finite_difference"]["passed"] is True
    assert j["gradient_check"]["finite_difference"]["max_abs_err"] < 1e-6


def test_run_tight_budget_selects_checkpoint(client) -> None:
    req = {
        "nodes": big_chain_raw(),
        "outputs": ["y"],
        "seed": 1,
        "budget_elements": 8000,
    }
    r = client.post("/api/v1/plans/run", json=req)
    assert r.status_code == 200, r.text
    j = r.json()
    assert j["plan"]["block_count"] >= 2
    assert j["profile"]["peak_elements"] <= 8000
    assert j["profile"]["recompute_flops"] > 0  # 明确的额外计算量
    assert j["runtime"]["peak_match"] is True
    assert j["gradient_check"]["passed"] is True


def test_run_infeasible_budget_is_507(client) -> None:
    req = {
        "nodes": linear_chain_raw(),
        "outputs": ["l2"],
        "seed": 1,
        "budget_elements": 5,
    }
    r = client.post("/api/v1/plans/run", json=req)
    assert r.status_code == 507
    j = r.json()
    assert j["success"] is False
    assert j["error_category"] == "resource_exhausted"
    # details 必须能说明是预算不可行，而不是别的错误。
    assert j["details"]["budget"] == 5
    assert j["details"]["min_achievable_peak"] >= 5
    # 错误也有可追溯运行编号（错误记录以 error- 为前缀）。
    assert j["run_id"].startswith("error-")


def test_run_input_error_is_422(client) -> None:
    req = {
        "nodes": [{"id": "z", "op": "no-such-op"}],
        "budget_elements": 100,
    }
    r = client.post("/api/v1/plans/run", json=req)
    assert r.status_code == 422
    assert r.json()["error_category"] == "input_error"


def test_exhaustive_lists_candidates_and_min_peak(client) -> None:
    r = client.post(
        "/api/v1/plans/exhaustive",
        json={"nodes": branch_raw(), "outputs": ["l2"]},
    )
    assert r.status_code == 200, r.text
    j = r.json()
    # 分支图有 64 个合法方案。
    assert j["legal_candidate_count"] == 64
    assert len(j["candidates"]) == 64
    # 基线不检查点：零重算；穷举给出的最小峰值严格低于基线。
    assert j["baseline"]["profile"]["recompute_flops"] == 0
    assert j["min_peak"] < j["baseline"]["profile"]["peak_elements"]
    # 每个候选都带峰值与重算成本两项，供对照。
    for c in j["candidates"]:
        assert "peak_elements" in c["profile"]
        assert "recompute_flops" in c["profile"]


def test_run_log_fetchable_by_run_id(client) -> None:
    req = {
        "nodes": linear_chain_raw(),
        "outputs": ["l2"],
        "seed": 5,
        "budget_elements": 100000,
    }
    j = client.post("/api/v1/plans/run", json=req).json()
    run_id = j["run_id"]
    fetched = client.get(f"/api/v1/runs/{run_id}")
    assert fetched.status_code == 200
    rec = fetched.json()
    # 日志记录保留可重放的关键中间状态。
    assert rec["run_id"] == run_id
    assert rec["seed"] == 5
    assert rec["peak_match"] is True
    assert rec["plan"]["boundary_positions"] == j["plan"]["boundary_positions"]
    assert "reason" in rec and rec["reason"]


def test_unknown_run_id_is_404(client) -> None:
    r = client.get("/api/v1/runs/run-does-not-exist")
    assert r.status_code == 404
    assert r.json()["error_category"] == "input_error"


def test_verify_gradients_false_skips_check(client) -> None:
    req = {
        "nodes": linear_chain_raw(),
        "outputs": ["l2"],
        "seed": 3,
        "budget_elements": 100000,
        "verify_gradients": False,
        "finite_difference": True,  # 关闭校验时 FD 也不应执行
    }
    r = client.post("/api/v1/plans/run", json=req)
    assert r.status_code == 200, r.text
    j = r.json()
    assert j["gradient_check"] is None
    assert j["runtime"]["peak_match"] is True
