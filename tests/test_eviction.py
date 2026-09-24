"""Unit tests for PDB accounting, admission and safety audit."""
from __future__ import annotations

from app.eviction import (
    account,
    admit_evictions,
    audit_snapshot,
    hard_eviction_blocker,
    is_daemonset_pod,
    resolve_workload,
)
from app.models import PodDisruptionBudget, LabelSelector
from tests.conftest import make_pod


# --------------------------------------------------------------------------- #
# intstr / accounting
# --------------------------------------------------------------------------- #
def test_min_available_integer_accounting(three_node_cluster):
    pdb = three_node_cluster.pdbs[0]
    acct = account(three_node_cluster, pdb)
    assert acct.expected == 3
    assert acct.healthy == 3
    assert acct.audit_floor == 2
    assert acct.disruptions_allowed == 1


def test_min_available_percentage_rounds_up(three_node_cluster):
    pdb = PodDisruptionBudget(
        name="pct", minAvailable="51%",
        selector=LabelSelector(matchLabels={"app": "app"}),
    )
    acct = account(three_node_cluster, pdb)
    # ceil(3 * 0.51) = 2
    assert acct.audit_floor == 2
    assert acct.disruptions_allowed == 1


def test_max_unavailable_accounting(two_app_cluster):
    pdb = next(p for p in two_app_cluster.pdbs if p.name == "pdb-b")
    acct = account(two_app_cluster, pdb)
    assert acct.expected == 4
    assert acct.healthy == 4
    assert acct.audit_floor == 3
    assert acct.disruptions_allowed == 1


def test_unhealthy_replica_reduces_disruptions_allowed(three_node_cluster):
    # app-0 not ready -> healthy=2, floor=2 -> nothing may be disrupted.
    three_node_cluster.pods[0].ready = False
    acct = account(three_node_cluster, three_node_cluster.pdbs[0])
    assert acct.healthy == 2
    assert acct.audit_floor == 2
    assert acct.disruptions_allowed == 0


# --------------------------------------------------------------------------- #
# Workload resolution
# --------------------------------------------------------------------------- #
def test_rs_pod_attributed_to_deployment_by_selector(three_node_cluster):
    kind, obj = resolve_workload(three_node_cluster, three_node_cluster.pods[0])
    assert kind == "Deployment"
    assert obj.name == "app"


def test_daemonset_detection(ds_cluster):
    agent = next(p for p in ds_cluster.pods if p.name == "agent-n1")
    assert is_daemonset_pod(ds_cluster, agent)
    app0 = next(p for p in ds_cluster.pods if p.name == "app-0")
    assert not is_daemonset_pod(ds_cluster, app0)


def test_bare_pod_has_no_workload(three_node_cluster):
    bare = make_pod("bare-0", "n1", owner_kind="", uid="x")
    bare.owner = None
    assert resolve_workload(three_node_cluster, bare) is None


# --------------------------------------------------------------------------- #
# Hard blockers
# --------------------------------------------------------------------------- #
def test_mirror_and_static_pods_are_hard_blockers():
    assert hard_eviction_blocker(make_pod("m", "n1", mirror=True, uid="m"))
    assert hard_eviction_blocker(make_pod("s", "n1", static=True, uid="s"))
    assert hard_eviction_blocker(make_pod("o", "n1", uid="o")) is None


# --------------------------------------------------------------------------- #
# Multi-PDB intersection admission
# --------------------------------------------------------------------------- #
def test_admit_at_most_one_shared_pod_at_a_time(two_app_cluster):
    n1_pods = [p for p in two_app_cluster.pods if p.node == "n1"]
    assert len(n1_pods) == 2
    admitted, rejected = admit_evictions(two_app_cluster, n1_pods)
    # shared PDB maxUnavailable=1 admits only the first of the two.
    assert len(admitted) == 1
    assert len(rejected) == 1
    rej = rejected[0]
    assert rej.pdb == "default/pdb-shared"


def test_admission_consumes_budget_within_one_wave(two_app_cluster):
    pods = [p for p in two_app_cluster.pods if p.name in ("a-0", "b-0", "a-1")]
    admitted, rejected = admit_evictions(two_app_cluster, pods)
    # The shared PDB (maxUnavailable=1 over 8 pods) has exactly one slot: one
    # pod admitted, the other two rejected by the intersection.
    assert account(
        two_app_cluster,
        next(p for p in two_app_cluster.pdbs if p.name == "pdb-shared"),
    ).disruptions_allowed == 1
    assert len(admitted) == 1
    assert len(rejected) == 2


def test_unhealthy_pod_blocks_under_if_healthy_budget(three_node_cluster):
    # One already-unhealthy pod means healthy(2) == required(2): an unhealthy
    # pod cannot be evicted (it would still count as disrupted in K8s terms),
    # and a healthy pod cannot either because no disruption slot exists.
    target = three_node_cluster.pods[0]
    target.ready = False
    candidate = three_node_cluster.pods[1]
    admitted, rejected = admit_evictions(three_node_cluster, [candidate])
    assert admitted == []
    assert rejected[0].pdb == "default/app-pdb"


def test_always_allow_admits_unhealthy_pod():
    pdb = PodDisruptionBudget(
        name="relaxed", maxUnavailable=1,
        unhealthyPodEvictionPolicy="AlwaysAllow",
        selector=LabelSelector(matchLabels={"app": "app"}),
    )
    from app.models import Snapshot, Node, Deployment
    snap = Snapshot(
        generation=1,
        nodes=[Node(name="n1"), Node(name="n2")],
        deployments=[Deployment(name="app", replicas=2,
                                selector=LabelSelector(matchLabels={"app": "app"}))],
        pdbs=[pdb],
        pods=[
            make_pod("app-0", "n1", ready=False, uid="u0"),
            make_pod("app-1", "n2", ready=True, uid="u1"),
        ],
    )
    admitted, rejected = admit_evictions(snap, [snap.pods[0]])
    assert len(admitted) == 1
    assert rejected == []


# --------------------------------------------------------------------------- #
# Audit
# --------------------------------------------------------------------------- #
def test_audit_clean_on_initial_snapshot(three_node_cluster):
    assert audit_snapshot(three_node_cluster) == []


def test_terminating_bare_pod_does_not_falsely_violate(three_node_cluster):
    # An unmanaged bare pod selected by the PDB, once terminating, no longer
    # promises a slot (no controller replaces it): the audit must stay clean.
    bare = make_pod("bare-9", "n2", uid="bare9", owner_kind="")
    bare.owner = None
    three_node_cluster.pods.append(bare)
    pdb = three_node_cluster.pdbs[0]
    assert account(three_node_cluster, pdb).expected == 4  # 3 controller + bare
    bare.deletion_tick = three_node_cluster.tick
    acct = account(three_node_cluster, pdb)
    assert acct.expected == 3  # bare pod's slot is gone
    assert audit_snapshot(three_node_cluster) == []


def test_audit_flags_healthy_below_floor(three_node_cluster):
    # Simulate two pods being unready at once — floor 2 violated.
    three_node_cluster.pods[0].ready = False
    three_node_cluster.pods[1].ready = False
    violations = audit_snapshot(three_node_cluster)
    assert len(violations) == 1
    assert "below auditFloor=2" in violations[0].detail
