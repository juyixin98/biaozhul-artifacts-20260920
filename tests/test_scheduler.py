"""调度引擎单元测试：硬约束、软约束、平局规则逐项验证。"""

import json
from pathlib import Path

from app.models import AnalyzeRequest
from app.scheduler import analyze

EXAMPLES = Path(__file__).resolve().parent.parent / "examples"


def load_example(name: str) -> AnalyzeRequest:
    return AnalyzeRequest.model_validate(json.loads((EXAMPLES / name).read_text()))


def result_for(response, node_name):
    return next(r for r in response.results if r.node == node_name)


# --- 夹具 1：资源不足（按 request，不是利用率） -------------------------------


def test_insufficient_resources_fixture():
    resp = analyze(load_example("insufficient_resources.json"))
    small = result_for(resp, "node-small")
    big = result_for(resp, "node-big")
    assert not small.feasible
    # 1500m(pod) + 500m(existing) = 2000m > 1800m allocatable -> cpu 不足
    assert any("insufficient cpu" in r for r in small.filter_reasons)
    assert resp.winner == "node-big"
    assert big.feasible and big.rank == 1


def test_resource_boundary_exact_fit_is_feasible():
    """request 之和恰好等于 allocatable 时必须可行（边界）。"""
    req = AnalyzeRequest.model_validate(
        {
            "pod": {"name": "p", "requests": {"cpu": "1500m", "memory": "1Gi"}},
            "nodes": [
                {"name": "n1", "allocatable": {"cpu": "2", "memory": "2Gi"}}
            ],
            "existing_pods": [
                {
                    "name": "e",
                    "node_name": "n1",
                    "requests": {"cpu": "500m", "memory": "1Gi"},
                }
            ],
        }
    )
    resp = analyze(req)
    assert result_for(resp, "n1").feasible


def test_resource_one_milli_over_is_rejected():
    req = AnalyzeRequest.model_validate(
        {
            "pod": {"name": "p", "requests": {"cpu": "1501m", "memory": "1Gi"}},
            "nodes": [
                {"name": "n1", "allocatable": {"cpu": "2", "memory": "2Gi"}}
            ],
            "existing_pods": [
                {
                    "name": "e",
                    "node_name": "n1",
                    "requests": {"cpu": "500m", "memory": "512Mi"},
                }
            ],
        }
    )
    resp = analyze(req)
    node = result_for(resp, "n1")
    assert not node.feasible
    assert any("insufficient cpu" in r for r in node.filter_reasons)


# --- 夹具 2：反亲和冲突 -------------------------------------------------------


def test_anti_affinity_conflict_fixture():
    resp = analyze(load_example("anti_affinity_conflict.json"))
    a = result_for(resp, "node-a")
    b = result_for(resp, "node-b")
    assert not a.feasible
    assert any("anti-affinity conflict" in r for r in a.filter_reasons)
    assert resp.winner == "node-b"
    assert b.rank == 1


def test_required_anti_affinity_missing_topology_key_fails():
    req = AnalyzeRequest.model_validate(
        {
            "pod": {
                "name": "p",
                "anti_affinity": {
                    "required_terms": [
                        {"match_labels": {"app": "x"}, "topology_key": "rack"}
                    ]
                },
            },
            "nodes": [{"name": "n1", "allocatable": {"cpu": "2", "memory": "2Gi"}}],
        }
    )
    resp = analyze(req)
    node = result_for(resp, "n1")
    assert not node.feasible
    assert any("lacks topology key" in r for r in node.filter_reasons)


# --- 夹具 3：zone 偏斜（软约束，不得淘汰节点） --------------------------------


def test_zone_skew_fixture_prefers_emptier_zone():
    resp = analyze(load_example("zone_skew.json"))
    # 三个节点都必须可行：zone 偏斜是软约束
    assert all(r.feasible for r in resp.results)
    assert resp.winner == "node-b1"
    b1 = result_for(resp, "node-b1")
    a1 = result_for(resp, "node-a1")
    assert b1.scores.zone_spread == 100.0
    assert a1.scores.zone_spread == 0.0


# --- 夹具 4：容忍度匹配 -------------------------------------------------------


def test_toleration_fixture_untolerated_taint_filters_node():
    resp = analyze(load_example("toleration_match.json"))
    gpu = result_for(resp, "node-gpu")
    assert not gpu.feasible
    assert any("untolerated taint" in r for r in gpu.filter_reasons)
    assert resp.winner == "node-normal"


def test_toleration_allows_tainted_node():
    req = AnalyzeRequest.model_validate(
        {
            "pod": {
                "name": "p",
                "requests": {"cpu": "1", "memory": "1Gi"},
                "tolerations": [
                    {"key": "dedicated", "operator": "Equal", "value": "gpu",
                     "effect": "NoSchedule"}
                ],
            },
            "nodes": [
                {
                    "name": "n1",
                    "allocatable": {"cpu": "2", "memory": "2Gi"},
                    "taints": [
                        {"key": "dedicated", "value": "gpu", "effect": "NoSchedule"}
                    ],
                }
            ],
        }
    )
    resp = analyze(req)
    assert result_for(resp, "n1").feasible


def test_toleration_exists_operator_matches_any_value():
    req = AnalyzeRequest.model_validate(
        {
            "pod": {
                "name": "p",
                "tolerations": [{"key": "dedicated", "operator": "Exists"}],
            },
            "nodes": [
                {
                    "name": "n1",
                    "allocatable": {"cpu": "2", "memory": "2Gi"},
                    "taints": [
                        {"key": "dedicated", "value": "anything",
                         "effect": "NoSchedule"}
                    ],
                }
            ],
        }
    )
    assert result_for(analyze(req), "n1").feasible


# --- 软/硬约束不得混淆 ---------------------------------------------------------


def test_prefer_no_schedule_taint_is_soft_not_hard():
    """PreferNoSchedule 污点未被容忍：节点仍可行，仅评分降低。"""
    req = AnalyzeRequest.model_validate(
        {
            "pod": {"name": "p"},
            "nodes": [
                {
                    "name": "n1",
                    "allocatable": {"cpu": "2", "memory": "2Gi"},
                    "taints": [
                        {"key": "spot", "value": "true",
                         "effect": "PreferNoSchedule"}
                    ],
                },
                {"name": "n2", "allocatable": {"cpu": "2", "memory": "2Gi"}},
            ],
        }
    )
    resp = analyze(req)
    n1 = result_for(resp, "n1")
    assert n1.feasible  # 软约束不得淘汰
    assert n1.scores.prefer_no_schedule_taint == 0.0
    assert resp.winner == "n2"


def test_preferred_node_affinity_is_soft():
    req = AnalyzeRequest.model_validate(
        {
            "pod": {
                "name": "p",
                "node_affinity": {
                    "preferred_terms": [
                        {
                            "weight": 80,
                            "term": {
                                "match_expressions": [
                                    {"key": "ssd", "operator": "In",
                                     "values": ["true"]}
                                ]
                            },
                        }
                    ]
                },
            },
            "nodes": [
                {"name": "n1", "labels": {"ssd": "true"},
                 "allocatable": {"cpu": "2", "memory": "2Gi"}},
                {"name": "n2", "allocatable": {"cpu": "2", "memory": "2Gi"}},
            ],
        }
    )
    resp = analyze(req)
    assert all(r.feasible for r in resp.results)
    assert result_for(resp, "n1").scores.preferred_node_affinity == 100.0
    assert result_for(resp, "n2").scores.preferred_node_affinity == 0.0
    assert resp.winner == "n1"


# --- 平局与排名 ----------------------------------------------------------------


def test_tie_broken_by_node_name():
    req = AnalyzeRequest.model_validate(
        {
            "pod": {"name": "p"},
            "nodes": [
                {"name": "node-c", "allocatable": {"cpu": "2", "memory": "2Gi"}},
                {"name": "node-a", "allocatable": {"cpu": "2", "memory": "2Gi"}},
                {"name": "node-b", "allocatable": {"cpu": "2", "memory": "2Gi"}},
            ],
        }
    )
    resp = analyze(req)
    scores = {r.node: r.final_score for r in resp.results}
    assert len(set(scores.values())) == 1  # 完全平分
    assert resp.winner == "node-a"
    assert [r.node for r in resp.results] == ["node-a", "node-b", "node-c"]
    assert [r.rank for r in resp.results] == [1, 2, 3]


def test_no_feasible_node_gives_null_winner():
    req = AnalyzeRequest.model_validate(
        {
            "pod": {"name": "p", "requests": {"cpu": "100", "memory": "1Gi"}},
            "nodes": [
                {"name": "n1", "allocatable": {"cpu": "2", "memory": "2Gi"}}
            ],
        }
    )
    resp = analyze(req)
    assert resp.winner is None
    assert all(not r.feasible for r in resp.results)


def test_least_allocated_prefers_emptier_node():
    req = AnalyzeRequest.model_validate(
        {
            "pod": {"name": "p", "requests": {"cpu": "500m", "memory": "512Mi"}},
            "nodes": [
                {"name": "n1", "allocatable": {"cpu": "4", "memory": "4Gi"}},
                {"name": "n2", "allocatable": {"cpu": "4", "memory": "4Gi"}},
            ],
            "existing_pods": [
                {
                    "name": "e",
                    "node_name": "n1",
                    "requests": {"cpu": "2", "memory": "2Gi"},
                }
            ],
        }
    )
    resp = analyze(req)
    assert resp.winner == "n2"
    assert (
        result_for(resp, "n2").scores.least_allocated
        > result_for(resp, "n1").scores.least_allocated
    )
