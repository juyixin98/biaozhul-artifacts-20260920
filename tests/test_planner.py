"""Planner tests: ordered waves, blockers, PDB-driven batching."""
from __future__ import annotations

from app.planner import PlanRequest, build_plan
from app.models import CordonStep, CompleteStep, EvictWaveStep
from app.simulator import Simulator
from app.eviction import audit_snapshot


def _plan(snap, nodes, **kw):
    return build_plan(snap, PlanRequest(drain_id="d", nodes=nodes, **kw))


def test_plan_starts_with_cordon_for_every_node(three_node_cluster):
    plan = _plan(three_node_cluster, ["n1"])
    assert isinstance(plan.steps[0], CordonStep)
    assert plan.steps[0].node == "n1"
    assert isinstance(plan.steps[-1], CompleteStep)


def test_two_nodes_are_both_cordoned_before_any_eviction(two_app_cluster):
    plan = _plan(two_app_cluster, ["n1", "n2"])
    cordons = [s for s in plan.steps if isinstance(s, CordonStep)]
    assert [c.node for c in cordons] == ["n1", "n2"]
    first_wave = next(s for s in plan.steps if isinstance(s, EvictWaveStep))
    assert first_wave.index == 0
    # All first-wave pods live on the two drained nodes.
    pod_nodes = {p.name: p.node for p in two_app_cluster.pods}
    for name in first_wave.pods:
        assert pod_nodes[name] in {"n1", "n2"}
    # The shared PDB (maxUnavailable=1 across 8 pods) bounds the first wave.
    assert len(first_wave.pods) == 1


def test_min_available_two_of_three_forces_single_pod_waves(three_node_cluster):
    # 3 replicas, minAvailable 2 => wave size 1 and 3 sequential waves for a
    # full-cluster drain (capacity allowing). Here only n1 drains: one wave.
    plan = _plan(three_node_cluster, ["n1"])
    waves = [s for s in plan.steps if isinstance(s, EvictWaveStep)]
    assert len(waves) == 1
    assert waves[0].pods == ["app-0"]
    assert plan.pdb_baseline["default/app-pdb"] == 3


def test_full_drain_batches_sequentially_for_pdb(three_node_cluster):
    # Draining all 3 nodes with only n1..n3: there is no destination node,
    # which is a hard capacity blocker — the planner must say so.
    plan = _plan(three_node_cluster, ["n1", "n2", "n3"])
    assert plan.blockers, "expected a capacity blocker when draining all nodes"
    assert all("no schedulable node" in b.reason for b in plan.blockers)


def test_static_pod_is_reported_as_blocker(three_node_cluster):
    three_node_cluster.pods[0].static_pod = True
    plan = _plan(three_node_cluster, ["n1"])
    blockers = [b for b in plan.blockers if b.pod == "app-0"]
    assert len(blockers) == 1
    assert "static pod" in blockers[0].reason


def test_mirror_pod_is_reported_as_blocker(three_node_cluster):
    three_node_cluster.pods[0].mirror = True
    plan = _plan(three_node_cluster, ["n1"])
    assert any(b.pod == "app-0" and "mirror" in b.reason for b in plan.blockers)


def test_daemonset_pods_skipped_not_blocking(ds_cluster):
    plan = _plan(ds_cluster, ["n1"])
    assert not plan.blockers
    waves = [s for s in plan.steps if isinstance(s, EvictWaveStep)]
    assert waves[0].pods == ["app-0"]
    assert any("daemonset" in r for r in plan.rationale)


def test_plan_is_generation_labelled(three_node_cluster):
    plan = _plan(three_node_cluster, ["n1"])
    assert plan.generation == three_node_cluster.generation


def test_unknown_node_rejected(three_node_cluster):
    import pytest
    with pytest.raises(ValueError):
        _plan(three_node_cluster, ["nope"])


def test_planned_shadow_run_never_violates_pdb(two_app_cluster):
    # The shadow simulation used to build the plan must itself remain
    # violation-free after every predicted eviction.
    shadow = Simulator(two_app_cluster)
    for n in ("n1", "n2"):
        shadow.cordon(n)
    plan = _plan(two_app_cluster, ["n1", "n2"])
    # Replay plan manually with settling and audit after every wave.
    from app.planner import next_wave
    evicted: set[str] = set()
    idx = 0
    while True:
        pods, blockers, _ = next_wave(
            shadow.snap, ["n1", "n2"], True, idx, evicted
        )
        assert not blockers
        if not pods:
            break
        for p in pods:
            shadow.evict(p, drain_id="d")
            evicted.add(p.uid)
        assert audit_snapshot(shadow.snap) == []
        # Settle before the next wave.
        for _ in range(20):
            busy = any(x.deleting for x in shadow.snap.pods) or any(
                (not x.ready) and not x.deleting for x in shadow.snap.pods
            )
            if not busy:
                break
            shadow.tick(1)
        assert audit_snapshot(shadow.snap) == []
        idx += 1
    assert idx == sum(1 for s in plan.steps if isinstance(s, EvictWaveStep))
