"""模拟器行为测试：PDB admission、重建、就绪流转、故障注入、代次。"""
from __future__ import annotations

import pytest

from app.simulator import Simulator, PdbAdmissionError
from app.planner import normalize_snapshot


def sim_from(pods, deployments=None, pdbs=None, nodes=("n1", "n2"), capacities=None):
    node_pods = {n: [] for n in nodes}
    for p in pods:
        node_pods[p["node"]].append(p)
    snap = {
        "snapshot_id": "sim",
        "nodes": [
            {"name": n, "schedulable": True, "capacity": (capacities or {}).get(n, 10),
             "pods": node_pods[n]}
            for n in nodes
        ],
        "deployments": deployments or [],
        "pdbs": pdbs or [],
    }
    sim = Simulator()
    sim.load_snapshot(normalize_snapshot(snap))
    return sim


def dpod(uid, node, ready=True, labels=None, dep="web"):
    return {
        "name": uid, "uid": uid, "namespace": "default", "node": node,
        "phase": "Running", "ready": ready,
        "labels": labels or {"app": dep},
        "owner_kind": "Deployment", "owner_name": dep,
    }


WEB_DEP = {"name": "web", "namespace": "default", "replicas": 3, "ready_replicas": 3,
           "selector": {"app": "web"}, "delay_ready_ticks": 1}
PDB = {"name": "pdb-web", "namespace": "default", "selector": {"app": "web"},
       "min_available": 2, "max_unavailable": None}


class TestAdmission:
    def test_denies_when_budget_zero(self):
        # 2 healthy，minAvailable=2 => allowed 0，直接拒
        sim = sim_from([dpod("w1", "n1"), dpod("w2", "n1")], [WEB_DEP], [PDB])
        assert sim.pdb_status()[0]["allowed_disruptions"] == 0
        with pytest.raises(PdbAdmissionError):
            sim.evict("default", "w1")
        assert sim.get_pod("default", "w1") is not None

    def test_second_eviction_denied_while_first_terminating(self):
        sim = sim_from([dpod("w1", "n1"), dpod("w2", "n1"), dpod("w3", "n1")], [WEB_DEP], [PDB])
        assert sim.pdb_status()[0]["allowed_disruptions"] == 1
        sim.evict("default", "w1")
        # w1 terminating 期间预算 <=0（在途中断占名额；K8s 该窗口可为 -1）
        assert sim.pdb_status()[0]["allowed_disruptions"] <= 0
        with pytest.raises(PdbAdmissionError):
            sim.evict("default", "w2")

    def test_budget_reopens_after_replacement_ready(self):
        sim = sim_from([dpod("w1", "n1"), dpod("w2", "n1"), dpod("w3", "n1")], [WEB_DEP], [PDB])
        sim.cordon("n1")
        sim.evict("default", "w1")
        sim.tick()  # w1 消失，替代 Pod 创建并调度到 n2（Running 但还没 Ready）
        assert sim.pdb_status()[0]["allowed_disruptions"] == 0
        sim.tick()  # 替代 Pod Ready
        assert sim.pdb_status()[0]["allowed_disruptions"] == 1

    def test_cordoned_node_gets_no_replacements(self):
        sim = sim_from([dpod("w1", "n1")], [{**WEB_DEP, "replicas": 1}], [])
        sim.cordon("n1")
        sim.evict("default", "w1")
        sim.tick()
        new = [p for p in sim.all_pod_dicts() if p["uid"] != "w1"]
        assert len(new) == 1
        assert new[0]["node"] != "n1"


class TestReplacement:
    def test_pending_when_no_free_node_then_schedules_when_capacity(self):
        # n2 容量已满（1/1），替代 Pod 只能 Pending；腾出槽位后下一 tick 被调度
        other = {**dpod("o1", "n2", labels={"app": "other"}, dep="other")}
        sim = sim_from(
            [dpod("w1", "n1"), other],
            [{**WEB_DEP, "replicas": 1},
             {**WEB_DEP, "name": "other", "replicas": 1, "selector": {"app": "other"}}],
            [], capacities={"n2": 1},
        )
        sim.cordon("n1")
        sim.evict("default", "w1")
        sim.tick()
        pending = [p for p in sim.all_pod_dicts() if p["phase"] == "Pending"]
        assert len(pending) == 1
        # 腾出 n2 的槽位
        sim.get_pod("default", "o1").terminate_tick = sim.tick_n
        sim.tick()  # o1 消失
        sim.tick()  # Pending 替代 Pod 被调度
        sim.tick()  # 就绪延迟到期 → Ready
        web_pods = [p for p in sim.all_pod_dicts() if p["owner_name"] == "web" and p["uid"] != "w1"]
        assert len(web_pods) == 1
        assert web_pods[0]["phase"] == "Running"
        assert web_pods[0]["node"] == "n2"
        assert web_pods[0]["ready"] is True

    def test_never_ready_fault_blocks_readiness(self):
        sim = sim_from([dpod("w1", "n1")], [{**WEB_DEP, "replicas": 1}], [])
        sim.cordon("n1")
        sim.inject_fault("default", "web", "never_ready")
        sim.evict("default", "w1")
        for _ in range(5):
            sim.tick()
        web = [p for p in sim.all_pod_dicts() if p["owner_name"] == "web" and p["uid"] != "w1"]
        assert len(web) == 1
        assert web[0]["phase"] == "Running"
        assert web[0]["ready"] is False
        # 清除故障后恢复 Ready
        sim.clear_fault("default", "web")
        sim.tick()
        sim.tick()
        assert any(p["ready"] for p in sim.all_pod_dicts() if p["uid"] != "w1")

    def test_generation_bumps_on_structural_change_only(self):
        sim = sim_from([dpod("w1", "n1")], [{**WEB_DEP, "replicas": 1}], [])
        g0 = sim.generation
        sim.cordon("n1")
        assert sim.generation == g0 + 1
        g1 = sim.generation
        sim.tick()  # 无结构变化
        assert sim.generation == g1
        sim.evict("default", "w1")
        assert sim.generation == g1 + 1


class TestMultiPdb:
    def test_intersection_requires_both_admit(self):
        pdb_a = {"name": "a", "namespace": "default", "selector": {"app": "web"},
                 "min_available": None, "max_unavailable": 1}
        pdb_b = {"name": "b", "namespace": "default", "selector": {"tier": "crit"},
                 "min_available": 3, "max_unavailable": None}
        pods = [
            {**dpod(f"w{i}", "n1"), "labels": {"app": "web", "tier": "crit"}}
            for i in range(1, 5)
        ]
        dep = {**WEB_DEP, "replicas": 4, "selector": {"app": "web"},
               "template_labels": {"app": "web", "tier": "crit"}}
        sim = sim_from(pods, [dep], [pdb_a, pdb_b])
        sim.evict("default", "w1")  # 两者都许可（各允许 1）
        with pytest.raises(PdbAdmissionError) as ei:
            sim.evict("default", "w2")
        names = {v["pdb"] for v in ei.value.denied_views}
        # 在途中断占名额：两个 PDB 都拒绝第二个
        assert names == {"default/a", "default/b"}
