"""Executor acceptance tests.

Every scenario asserts the central invariant after *every* transition: the
simulator audit reports no PDB violation, i.e. no single step ever goes below
the promised healthy floor, and a deleted pod is never counted as a ready
replacement.
"""
from __future__ import annotations

import pytest

from app.executor import (
    DrainState,
    ExecutorError,
    advance,
    autorun,
    create_drain,
    evaluate,
    request_cancel,
)
from app.eviction import audit_snapshot
from app.models import EvictWaveStep
from app.security import verify_token


def _audit_clean(sim):
    violations = audit_snapshot(sim.snap)
    assert violations == [], "; ".join(v.detail for v in violations)


def _run_to_end(d, token, ticks=60):
    return autorun(d, token, max_ticks=ticks)


# --------------------------------------------------------------------------- #
# Happy path baseline
# --------------------------------------------------------------------------- #
def test_simple_drain_completes_and_keeps_n1_empty(sim, secret):
    d = create_drain(sim, "d1", secret, nodes=["n1"])
    res = _run_to_end(d, d.token())
    assert res["finalState"] == DrainState.COMPLETE.value
    assert all(p.node != "n1" for p in sim.snap.pods if not p.deletion_tick)
    ready = [p for p in sim.snap.pods if p.ready and p.phase == "Running"]
    assert len(ready) == 3  # 2 originals + 1 replacement
    _audit_clean(sim)


def test_every_step_exposes_preconditions(sim, secret):
    d = create_drain(sim, "d1", secret, nodes=["n1"])
    token = d.token()
    executed_messages = []
    precondition_ids_seen = set()
    while True:
        pre = evaluate(d)
        assert pre, "every non-terminal step must expose preconditions"
        for p in pre:
            assert p.id and p.description
            precondition_ids_seen.add(p.id)
        result = advance(d, token, tick_wait=10)
        token = result.token
        executed_messages.append(result.message)
        _audit_clean(sim)
        if result.state == DrainState.COMPLETE.value:
            break
    assert any("cordoned" in m for m in executed_messages)
    assert any("evicted" in m and "wave" in m for m in executed_messages)
    # Each step family must have surfaced its characteristic preconditions.
    assert "node-exists" in precondition_ids_seen
    assert "multi-pdb-intersection" in precondition_ids_seen
    assert "earlier-replacements-ready" in precondition_ids_seen
    assert "drain-set-empty-of-evictable-pods" in precondition_ids_seen


# --------------------------------------------------------------------------- #
# Scenario 1: already-unhealthy replica
# --------------------------------------------------------------------------- #
def test_unhealthy_replica_blocks_eviction_until_healthy(sim, secret):
    # app-2 is already NotReady: healthy=2 == floor 2, no disruption slot.
    sim.external_set_pod_ready("default", "app-2", False)
    d = create_drain(sim, "d1", secret, nodes=["n1"])
    token = d.token()

    # Cordon still runs (it does not touch pods).
    r = advance(d, token, tick_wait=0)
    assert r.executed and r.current_step and r.current_step["kind"] == "EvictWave"
    token = r.token

    # The wave cannot execute: multi-pdb intersection admits nothing while
    # the workload is already at its healthy floor.
    r = advance(d, token, tick_wait=0)
    assert r.executed is False
    assert r.state == DrainState.WAITING.value
    failed = [p for p in r.preconditions if not p.satisfied]
    assert any(p.id == "multi-pdb-intersection" for p in failed)
    _audit_clean(sim)
    # app-0 must still be safely on n1 (no destructive action taken).
    app0 = next(p for p in sim.snap.pods if p.name == "app-0")
    assert app0.node == "n1" and app0.deletion_tick is None

    # Once the unhealthy replica recovers out of band, the old token is stale;
    # the error hands back a fresh token against the recomputed plan.
    sim.external_set_pod_ready("default", "app-2", True)
    with pytest.raises(ExecutorError) as ei:
        advance(d, token, tick_wait=10)
    assert ei.value.code == "stale_generation"
    token = ei.value.fresh_token
    res = autorun(d, token, max_ticks=30)
    assert res["finalState"] == DrainState.COMPLETE.value
    _audit_clean(sim)


def test_unhealthy_replica_recovery_never_skips_replacement_barrier(sim, secret):
    sim.external_set_pod_ready("default", "app-2", False)
    d = create_drain(sim, "d1", secret, nodes=["n1"])
    token = d.token()
    advance(d, token, 0)  # cordon
    token = d.token()
    # Even with wait ticks, the wave stays blocked while unhealthy.
    r = advance(d, token, tick_wait=5)
    assert r.executed is False
    assert not any(p.name == "app-0" and p.deletion_tick is not None
                   for p in sim.snap.pods)
    _audit_clean(sim)


# --------------------------------------------------------------------------- #
# Scenario 2: two nodes drained simultaneously
# --------------------------------------------------------------------------- #
def test_two_nodes_drain_together_and_intersection_holds(two_app_cluster, secret):
    from app.simulator import Simulator
    sim = Simulator(two_app_cluster)
    d = create_drain(sim, "d1", secret, nodes=["n1", "n2"])
    res = _run_to_end(d, d.token(), ticks=60)
    assert res["finalState"] == DrainState.COMPLETE.value

    snap = sim.snap
    # Both nodes emptied; 8 promised replicas exist and are ready.
    remaining = [p for p in snap.pods if p.node in {"n1", "n2"}
                 and not p.deletion_tick]
    assert remaining == []
    ready = [p for p in snap.pods if p.ready and p.phase == "Running"]
    assert len(ready) == 8

    # Reconstruct the executed waves from events: never more than one shared
    # disruption at a time.
    evict_events = [e for e in d.events if e.type == "execute"
                    and "evicted wave" in e.detail]
    assert len(evict_events) >= 2
    _audit_clean(sim)


def test_two_node_drain_wave_sizes_respect_all_three_pdbs(two_app_cluster, secret):
    from app.simulator import Simulator
    sim = Simulator(two_app_cluster)
    d = create_drain(sim, "d1", secret, nodes=["n1", "n2"])
    predicted = [s for s in d.plan.steps if isinstance(s, EvictWaveStep)]
    # The shared PDB selects 8 pods with maxUnavailable=1: every predicted
    # wave contains exactly one pod.
    assert predicted, "planner must produce waves"
    assert all(len(s.pods) == 1 for s in predicted)
    # 4 pods (a-0,b-0 on n1; a-1,b-1 on n2) across 4 waves.
    assert len(predicted) == 4


# --------------------------------------------------------------------------- #
# Scenario 3: replacement pod never becomes ready
# --------------------------------------------------------------------------- #
def test_stuck_replacement_halts_next_wave_without_violation(sim, secret):
    # Freeze replacements: nothing ever becomes ready automatically.
    for dep in sim.snap.deployments:
        dep.ready_delay = 10 ** 6
    d = create_drain(sim, "d1", secret, nodes=["n1"])
    token = d.token()
    advance(d, token, 0)  # cordon n1
    token = d.token()
    # Wave 0 can evict (budget initially allows one), replacement hangs.
    r = advance(d, token, 0)
    assert r.executed
    token = r.token
    _audit_clean(sim)

    # Trying to finish with a small budget stays WAITING: the barrier
    # "earlier-replacements-ready" never clears, and completion never lies.
    r = advance(d, token, tick_wait=3)
    assert r.executed is False
    assert r.state == DrainState.WAITING.value
    unsat = {p.id for p in r.preconditions if not p.satisfied}
    assert "earlier-replacements-ready" in unsat or \
           "in-flight-work-settled" in unsat
    _audit_clean(sim)

    # Exactly one original pod (app-0) was disrupted; app-1/app-2 untouched.
    assert next(p for p in sim.snap.pods if p.name == "app-1").ready
    assert next(p for p in sim.snap.pods if p.name == "app-2").ready

    # autorun with a finite budget must end WAITING, not COMPLETE.
    out = autorun(d, token, max_ticks=5)
    assert out["finalState"] == DrainState.WAITING.value
    _audit_clean(sim)


def test_stuck_replacement_recovers_when_pod_finally_ready(sim, secret):
    for dep in sim.snap.deployments:
        dep.ready_delay = 10 ** 6
    d = create_drain(sim, "d1", secret, nodes=["n1"])
    token = d.token()
    advance(d, token, 0)
    token = d.token()
    advance(d, token, 0)  # evict wave 0
    token = d.token()
    # Out-of-band: force the Pending replacement to become ready.
    replacement = next(p for p in sim.snap.pods if p.origin == "replacement")
    sim.external_set_pod_ready(replacement.namespace, replacement.name, True)
    # Old token must be rejected (generation moved); fresh one lets it finish.
    with pytest.raises(ExecutorError) as ei:
        advance(d, token, tick_wait=5)
    assert ei.value.code == "stale_generation"
    fresh = ei.value.fresh_token
    out = autorun(d, fresh, max_ticks=10)
    assert out["finalState"] == DrainState.COMPLETE.value
    _audit_clean(sim)


# --------------------------------------------------------------------------- #
# Scenario 4: cancellation
# --------------------------------------------------------------------------- #
def test_cancel_stops_further_evictions_but_keeps_workload_safe(sim, secret):
    for dep in sim.snap.deployments:
        dep.ready_delay = 3
    d = create_drain(sim, "d1", secret, nodes=["n1"])
    token = d.token()
    advance(d, token, 0)  # cordon
    token = d.token()
    advance(d, token, 0)  # wave 0 evicted, replacement still maturing
    token = d.token()

    request_cancel(d, token)
    assert d.state == DrainState.CANCELLED

    # Further advance calls are refused.
    with pytest.raises(ExecutorError) as ei:
        advance(d, d.token(), tick_wait=10)
    assert ei.value.code == "cancelled"

    # Node stays cordoned; let time pass — the in-flight replacement still
    # matures, and the system heals without a second eviction.
    for _ in range(6):
        sim.tick(1)
    _audit_clean(sim)
    assert not sim.node("n1").schedulable, "cancelled drain leaves node cordoned"
    # No further pod was evicted after cancellation: only app-0 terminating/gone.
    evicted_names = {
        e["detail"].split("evicted by")[0].strip()
        for e in sim.events if e["type"] == "evict"
    }
    assert evicted_names == {"pod default/app-0"}
    # The replacement healed the deployment back to 3 ready.
    ready = [p for p in sim.snap.pods if p.ready and p.phase == "Running"]
    assert len(ready) == 3


def test_cannot_cancel_completed_drain(sim, secret):
    d = create_drain(sim, "d1", secret, nodes=["n1"])
    out = autorun(d, d.token(), max_ticks=30)
    assert out["finalState"] == DrainState.COMPLETE.value
    with pytest.raises(ExecutorError) as ei:
        request_cancel(d, d.token())
    assert ei.value.code == "already_complete"


# --------------------------------------------------------------------------- #
# Generation drift / replanning
# --------------------------------------------------------------------------- #
def test_external_pod_add_bumps_generation_and_replans(sim, secret):
    d = create_drain(sim, "d1", secret, nodes=["n1"])
    gen0 = d.plan.generation
    token = d.token()
    # Add an unrelated pod on n3: generation must move.
    sim.external_add_pod({
        "name": "extra-0", "node": "n3", "labels": {"app": "app"},
        "owner": {"kind": "ReplicaSet", "name": "app-rs"}, "uid": "x9",
    })
    assert sim.generation > gen0
    with pytest.raises(ExecutorError) as ei:
        advance(d, token, 0)
    assert ei.value.code == "stale_generation"
    # Fresh token works; drain still completes.
    out = autorun(d, ei.value.fresh_token, max_ticks=30)
    assert out["finalState"] == DrainState.COMPLETE.value
    _audit_clean(sim)


def test_blocked_drain_recovers_after_blocker_removed(three_node_cluster, secret):
    from app.simulator import Simulator
    three_node_cluster.pods[0].static_pod = True
    sim = Simulator(three_node_cluster)
    d = create_drain(sim, "d1", secret, nodes=["n1"])
    assert d.state == DrainState.BLOCKED
    token = d.token()
    advance(d, token, 0)  # cordon allowed even when blocked is decided in advance
    token = d.token()
    with pytest.raises(ExecutorError) as ei:
        advance(d, token, 0)
    assert ei.value.code == "blocked"
    # Remove the static pod out of band (node admin cleaned the manifest).
    sim.external_remove_pod("default", "app-0")
    # The blocked token predates the external change; the blocked error gave a
    # token at the old watermark, so fetch the current one and run to complete.
    out = autorun(d, d.token(), max_ticks=20)
    assert out["finalState"] == DrainState.COMPLETE.value
    _audit_clean(sim)


# --------------------------------------------------------------------------- #
# Token protocol
# --------------------------------------------------------------------------- #
def test_token_is_signed_and_tamper_detected(sim, secret):
    d = create_drain(sim, "d1", secret, nodes=["n1"])
    token = d.token()
    payload = verify_token(secret, token)
    assert payload["drain"] == "d1"
    body, sig = token.split(".")
    bad = body + "." + sig[:-2] + ("aa" if not sig.endswith("aa") else "bb")
    with pytest.raises(ExecutorError) as ei:
        advance(d, bad, 0)
    assert ei.value.code == "invalid_token"


def test_token_for_other_drain_rejected(sim, secret):
    d1 = create_drain(sim, "d1", secret, nodes=["n1"])
    d2 = create_drain(sim, "d2", secret, nodes=["n2"])
    with pytest.raises(ExecutorError) as ei:
        advance(d2, d1.token(), 0)
    assert ei.value.code == "invalid_token"


def test_step_replay_with_same_token_rejected(sim, secret):
    d = create_drain(sim, "d1", secret, nodes=["n1"])
    token = d.token()
    advance(d, token, 0)  # step 0 cordon; generation moved
    with pytest.raises(ExecutorError) as ei:
        advance(d, token, 0)  # replay old token
    assert ei.value.code in ("stale_generation", "stale_step")
