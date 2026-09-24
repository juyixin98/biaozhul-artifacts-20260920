"""Shared fixtures: small cluster snapshots used across the test-suite."""
from __future__ import annotations

import pytest

from app.models import (
    DaemonSet,
    Deployment,
    LabelSelector,
    Node,
    OwnerReference,
    Pod,
    PodDisruptionBudget,
    Snapshot,
)
from app.security import new_secret
from app.simulator import Simulator


def make_pod(
    name: str,
    node: str,
    app: str = "app",
    *,
    ready: bool = True,
    phase: str = "Running",
    namespace: str = "default",
    owner_kind: str = "ReplicaSet",
    owner_name: str = "app-rs",
    labels: dict | None = None,
    static: bool = False,
    mirror: bool = False,
    priority: int = 0,
    uid: str | None = None,
) -> Pod:
    return Pod(
        name=name,
        namespace=namespace,
        node=node,
        phase=phase,
        ready=ready,
        labels=labels if labels is not None else {"app": app},
        owner=OwnerReference(kind=owner_kind, name=owner_name),
        staticPod=static,
        mirror=mirror,
        priority=priority,
        uid=uid or f"uid-{name}",
    )


def app_deployment(name: str = "app", replicas: int = 3, ready_delay: int = 2,
                   app_label: str = "app") -> Deployment:
    return Deployment(
        name=name,
        replicas=replicas,
        readyDelay=ready_delay,
        selector=LabelSelector(matchLabels={"app": app_label}),
    )


@pytest.fixture
def secret() -> str:
    return new_secret()


@pytest.fixture
def three_node_cluster():
    """3 nodes, Deployment/app 3 replicas (one pod per node), PDB minAvailable=2."""
    snap = Snapshot(
        generation=1,
        nodes=[Node(name="n1"), Node(name="n2"), Node(name="n3")],
        deployments=[app_deployment()],
        pdbs=[PodDisruptionBudget(
            name="app-pdb", minAvailable=2,
            selector=LabelSelector(matchLabels={"app": "app"}),
        )],
        pods=[make_pod("app-0", "n1", uid="u0"),
              make_pod("app-1", "n2", uid="u1"),
              make_pod("app-2", "n3", uid="u2")],
    )
    return snap


@pytest.fixture
def sim(three_node_cluster) -> Simulator:
    return Simulator(three_node_cluster)


@pytest.fixture
def two_app_cluster():
    """Two deployments with overlapping PDB coverage on 4 nodes."""
    pods = []
    # app-a: 4 pods across n1..n4
    for i, n in enumerate(["n1", "n2", "n3", "n4"]):
        pods.append(Pod(
            name=f"a-{i}", node=n, labels={"app": "a", "tier": "shared"},
            owner=OwnerReference(kind="ReplicaSet", name="a-rs"), uid=f"a{i}",
        ))
    # app-b: 4 pods across n1..n4 (labels carry both selectors)
    for i, n in enumerate(["n1", "n2", "n3", "n4"]):
        pods.append(Pod(
            name=f"b-{i}", node=n, labels={"app": "b", "tier": "shared"},
            owner=OwnerReference(kind="ReplicaSet", name="b-rs"), uid=f"b{i}",
        ))
    snap = Snapshot(
        generation=1,
        nodes=[Node(name=n) for n in ("n1", "n2", "n3", "n4")],
        deployments=[
            Deployment(name="a", replicas=4, readyDelay=1,
                       selector=LabelSelector(matchLabels={"app": "a"})),
            Deployment(name="b", replicas=4, readyDelay=1,
                       selector=LabelSelector(matchLabels={"app": "b"})),
        ],
        pdbs=[
            PodDisruptionBudget(name="pdb-a", minAvailable=3,
                                selector=LabelSelector(matchLabels={"app": "a"})),
            PodDisruptionBudget(name="pdb-b", maxUnavailable=1,
                                selector=LabelSelector(matchLabels={"app": "b"})),
            # Shared PDB selects both apps via tier label — real intersection.
            PodDisruptionBudget(name="pdb-shared", maxUnavailable=1,
                                selector=LabelSelector(matchLabels={"tier": "shared"})),
        ],
        pods=pods,
    )
    return snap


@pytest.fixture
def ds_cluster():
    """Cluster with a DaemonSet pod on each node."""
    snap = Snapshot(
        generation=1,
        nodes=[Node(name="n1"), Node(name="n2"), Node(name="n3")],
        daemon_sets=[DaemonSet(name="agent",
                               selector=LabelSelector(matchLabels={"app": "agent"}))],
        deployments=[app_deployment()],
        pdbs=[PodDisruptionBudget(name="app-pdb", minAvailable=2,
                                  selector=LabelSelector(matchLabels={"app": "app"}))],
        pods=[
            make_pod("app-0", "n1", uid="u0"),
            make_pod("app-1", "n2", uid="u1"),
            make_pod("app-2", "n3", uid="u2"),
            Pod(name="agent-n1", node="n1", labels={"app": "agent"},
                owner=OwnerReference(kind="DaemonSet", name="agent"), uid="d1"),
            Pod(name="agent-n2", node="n2", labels={"app": "agent"},
                owner=OwnerReference(kind="DaemonSet", name="agent"), uid="d2"),
            Pod(name="agent-n3", node="n3", labels={"app": "agent"},
                owner=OwnerReference(kind="DaemonSet", name="agent"), uid="d3"),
        ],
    )
    return snap
