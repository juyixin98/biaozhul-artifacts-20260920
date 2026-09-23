"""PDB 计算与分批规划的纯单元测试。"""
from __future__ import annotations

import pytest

from app.planner import (
    build_plan,
    desired_healthy,
    pdb_view,
    normalize_snapshot,
    labels_match,
)
from app.models import SnapshotSpec


def pod(uid, node="n1", ready=True, phase="Running", labels=None, owner="Deployment", deleting=False):
    return {
        "name": uid,
        "uid": uid,
        "namespace": "default",
        "node": node,
        "phase": phase,
        "ready": ready,
        "labels": labels or {"app": "dep"},
        "owner_kind": owner,
        "owner_name": {"Deployment": "dep", "DaemonSet": "ds-x", "Node": node}.get(owner),
        "controller": True,
        "has_empty_dir": False,
        "deletion_timestamp": "t" if deleting else None,
    }


PDB_MIN2 = {"name": "pdb", "namespace": "default", "selector": {"app": "dep"},
            "min_available": 2, "max_unavailable": None}
PDB_MAX1 = {"name": "pdb", "namespace": "default", "selector": {"app": "dep"},
            "min_available": None, "max_unavailable": 1}


class TestDesiredHealthy:
    def test_integer_min_available(self):
        assert desired_healthy(2, "min_available", 5) == 2

    def test_percent_min_available_rounds_up(self):
        # ceil(3*25/100)=1, ceil(4*25/100)=1, ceil(5*25/100)=2
        assert desired_healthy("25%", "min_available", 3) == 1
        assert desired_healthy("25%", "min_available", 4) == 1
        assert desired_healthy("25%", "min_available", 5) == 2
        assert desired_healthy("100%", "min_available", 3) == 3
        assert desired_healthy("0%", "min_available", 3) == 0

    def test_percent_max_unavailable_truncates(self):
        # 3 * 25% -> 0.75 -> truncate 0 => desired 3
        assert desired_healthy("25%", "max_unavailable", 3) == 3
        assert desired_healthy("25%", "max_unavailable", 4) == 3
        assert desired_healthy("50%", "max_unavailable", 4) == 2

    def test_clamped(self):
        assert desired_healthy(99, "min_available", 2) == 2
        assert desired_healthy(99, "max_unavailable", 2) == 0


class TestPdbView:
    def test_basic_counts(self):
        pods = [pod("p1"), pod("p2"), pod("p3", ready=False), pod("p4", phase="Pending", ready=False)]
        view = pdb_view(pods, PDB_MIN2)
        assert view["expected"] == 4
        assert view["healthy"] == 2
        assert view["terminating"] == 0
        assert view["desired_healthy"] == 2
        assert view["allowed_disruptions"] == 0

    def test_terminating_still_consumes_budget(self):
        # 2 non-terminating healthy + 1 terminating；minAvailable=2
        # expected=2 healthy=2 terminating=1, raw=2-2-1=-1（K8s 在途超发窗口允许为负）
        pods = [pod("p1"), pod("p2", deleting=False), pod("p3", deleting=True)]
        pods[2]["ready"] = True
        view = pdb_view(pods, PDB_MIN2)
        assert (view["expected"], view["healthy"], view["terminating"]) == (2, 2, 1)
        assert view["allowed_disruptions"] == -1

    def test_unhealthy_replicas_yield_zero_budget(self):
        # expected=3 healthy=1 desired=2 => 即使没有在途中断也为 0
        pods = [pod("p1"), pod("p2", ready=False), pod("p3", phase="Pending", ready=False)]
        view = pdb_view(pods, PDB_MIN2)
        assert view["allowed_disruptions"] == 0

    def test_selector_namespace_isolation(self):
        pods = [pod("p1"), {**pod("p2"), "namespace": "other"}]
        view = pdb_view(pods, PDB_MIN2)
        assert view["expected"] == 1

    def test_label_selector(self):
        assert labels_match({"a": "1"}, {"a": "1", "b": "2"})
        assert not labels_match({"a": "2"}, {"a": "1"})


class TestModelValidation:
    def test_pdb_requires_exactly_one_strategy(self):
        from pydantic import ValidationError
        with pytest.raises(ValidationError):
            SnapshotSpec(
                snapshot_id="x",
                nodes=[],
                pdbs=[{"name": "p", "selector": {"a": "1"},
                       "min_available": 1, "max_unavailable": 1}],
            )
        with pytest.raises(ValidationError):
            SnapshotSpec(snapshot_id="x", nodes=[], pdbs=[{"name": "p", "selector": {}}])

    def test_bad_percent(self):
        from pydantic import ValidationError
        with pytest.raises(ValidationError):
            SnapshotSpec(
                snapshot_id="x", nodes=[],
                pdbs=[{"name": "p", "selector": {}, "min_available": "125%"}],
            )


def make_snapshot(pods_by_node, deployments=None, pdbs=None, nodes_meta=None):
    nodes = []
    for i, (node_name, pods_) in enumerate(pods_by_node):
        meta = (nodes_meta or {}).get(node_name, {})
        nodes.append({
            "name": node_name,
            "schedulable": meta.get("schedulable", True),
            "capacity": meta.get("capacity", 10),
            "pods": [{**p, "node": node_name} if p.get("node", node_name) == node_name else p for p in pods_],
        })
    return {
        "snapshot_id": "unit",
        "nodes": nodes,
        "deployments": deployments if deployments is not None else [
            {"name": "dep", "namespace": "default", "replicas": 3, "ready_replicas": 3,
             "selector": {"app": "dep"}}
        ],
        "pdbs": pdbs or [],
    }


class TestPlanClassification:
    def test_blocked_pod_types(self):
        pods = [
            {**pod("bare", owner=None)},
            {**pod("ds", owner="DaemonSet"), "owner_kind": "DaemonSet", "owner_name": "ds-x"},
            {**pod("ed"), "has_empty_dir": True},
            {**pod("mirror", owner="Node"), "owner_kind": "Node", "owner_name": "n1"},
        ]
        snap = normalize_snapshot(make_snapshot([("n1", pods)], pdbs=[PDB_MIN2]))
        plan = build_plan(snap, ["n1"])
        reasons = {b["reason"] for b in plan["blocked"]}
        assert "bare_pod" in reasons
        assert "daemonset_pod" in reasons
        assert "local_storage_empty_dir" in reasons
        assert "mirror_pod" in reasons
        assert plan["status"] == "blocked"
        assert not any(s["kind"] == "evict" for s in plan["steps"])

    def test_force_skips_blocked_and_drains_rest(self):
        pods = [{**pod("bare", owner=None)}, pod("d1")]
        snap = normalize_snapshot(make_snapshot([("n1", pods), ("n2", [])]))
        plan = build_plan(snap, ["n1"], force=True)
        assert plan["status"] == "ready"
        assert any(b["reason"] == "bare_pod" for b in plan["skipped"])
        evict_steps = [s for s in plan["steps"] if s["kind"] == "evict"]
        assert len(evict_steps) == 1

    def test_terminal_pod_separate_wave(self):
        pods = [pod("done", ready=False, phase="Succeeded", owner=None)]
        snap = normalize_snapshot(make_snapshot([("n1", pods)]))
        plan = build_plan(snap, ["n1"])
        kinds = [(s["kind"], s["wave"]) for s in plan["steps"]]
        assert ("cordon", -2) in kinds
        assert ("delete_terminal", -1) in kinds

    def test_waves_split_by_pdb_budget(self):
        # 4 healthy，minAvailable=3 => 每波最多 1 个，共 4 波
        pods = [pod(f"p{i}") for i in range(4)]
        snap = normalize_snapshot(
            make_snapshot([("n1", pods)], pdbs=[{**PDB_MIN2, "min_available": 3}])
        )
        plan = build_plan(snap, ["n1"])
        waves = sorted(s["wave"] for s in plan["steps"] if s["kind"] == "evict")
        assert waves == [0, 1, 2, 3]

    def test_two_pod_min_available_allows_nothing(self):
        # 2 healthy，minAvailable=2 => allowed=0，计划 blocked
        pods = [pod("p1"), pod("p2")]
        snap = normalize_snapshot(make_snapshot([("n1", pods), ("n2", [])], pdbs=[PDB_MIN2]))
        plan = build_plan(snap, ["n1"])
        assert plan["status"] == "blocked"
        assert all(b["reason"] == "pdb_allows_zero_disruptions" for b in plan["blocked"])

    def test_multi_pdb_intersection_uses_tighter_one(self):
        # maxUnavailable=2 => 每波至多 2；minAvailable=3 (4 healthy) => 每波至多 1
        pods = [
            {**pod(f"p{i}"), "labels": {"app": "dep", "tier": "crit"}} for i in range(4)
        ]
        pdbs = [
            {"name": "a", "namespace": "default", "selector": {"app": "dep"},
             "min_available": None, "max_unavailable": 2},
            {"name": "b", "namespace": "default", "selector": {"tier": "crit"},
             "min_available": 3, "max_unavailable": None},
        ]
        dep = [{"name": "dep", "namespace": "default", "replicas": 4, "ready_replicas": 4,
                "selector": {"app": "dep"}, "template_labels": {"app": "dep", "tier": "crit"}}]
        snap = normalize_snapshot(make_snapshot([("n1", pods)], deployments=dep, pdbs=pdbs))
        plan = build_plan(snap, ["n1"])
        waves = sorted(s["wave"] for s in plan["steps"] if s["kind"] == "evict")
        assert waves == [0, 1, 2, 3]
        assert all(set(s["covering_pdbs"]) == {"default/a", "default/b"}
                   for s in plan["steps"] if s["kind"] == "evict")

    def test_two_nodes_share_pdb_budget_in_same_wave(self):
        # 两个节点同时排空：4 healthy 分布两节点，minAvailable=3 => 每波全局只能 1 个
        pods_a = [pod("a1", node="n1"), pod("a2", node="n1")]
        pods_b = [pod("b1", node="n2"), pod("b2", node="n2")]
        snap = normalize_snapshot(make_snapshot([("n1", pods_a), ("n2", pods_b), ("n3", [])],
                                                pdbs=[{**PDB_MIN2, "min_available": 3}]))
        plan = build_plan(snap, ["n1", "n2"])
        assert plan["drain_nodes"] == ["n1", "n2"]
        per_wave: dict[int, int] = {}
        for s in plan["steps"]:
            if s["kind"] == "evict":
                per_wave[s["wave"]] = per_wave.get(s["wave"], 0) + 1
        assert per_wave == {0: 1, 1: 1, 2: 1, 3: 1}

    def test_every_evict_step_has_preconditions(self):
        # 3 healthy、minAvailable=2 => allowed=1，有两步可规划
        snap = normalize_snapshot(make_snapshot([("n1", [pod("p1"), pod("p2"), pod("p3")]), ("n2", [])],
                                                pdbs=[PDB_MIN2]))
        plan = build_plan(snap, ["n1"])
        assert plan["status"] == "ready"
        for s in plan["steps"]:
            assert s["preconditions"], "每一步都必须输出前置条件"
            assert s["completion"]

    def test_digest_changes_with_snapshot(self):
        s1 = normalize_snapshot(make_snapshot([("n1", [pod("p1")])]))
        s2 = normalize_snapshot(make_snapshot([("n1", [pod("p1", ready=False)])]))
        assert build_plan(s1, ["n1"])["snapshot_digest"] != build_plan(s2, ["n1"])["snapshot_digest"]
