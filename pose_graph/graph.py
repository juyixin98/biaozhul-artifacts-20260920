"""Pose graph data structure and connectivity diagnostics.

The graph stores:

* :class:`Node` objects with index ``i`` and an initial SE2 guess
  ``(x, y, theta)``;
* :class:`Edge` objects constraining two nodes with a relative SE2
  measurement ``z = (dx, dy, dtheta)`` and a 3x3 information matrix
  ``Omega`` (inverse covariance).

Connectivity (including detection of disconnected components that carry no
fixed anchor) is diagnosed with a union-find over the undirected edges.
"""

from dataclasses import dataclass, field

import numpy as np

from .se2 import wrap_angle


class GraphStructureError(ValueError):
    """Raised when the pose graph is malformed (bad indices, matrices, ...)."""


@dataclass(frozen=True)
class Node:
    """A robot pose ``(x, y, theta)`` with a user-provided initial guess."""

    index: int
    initial_pose: np.ndarray

    def __post_init__(self):
        pose = np.asarray(self.initial_pose, dtype=float)
        if pose.shape != (3,):
            raise GraphStructureError(
                f"node {self.index}: initial_pose must have shape (3,), got {pose.shape}"
            )
        object.__setattr__(self, "initial_pose", pose.copy())


@dataclass(frozen=True)
class Edge:
    """Relative-pose constraint between node ``i`` and node ``j``."""

    i: int
    j: int
    z: np.ndarray
    information: np.ndarray
    kernel_type: str = "linear"
    kernel_parameter: float | None = None
    label: str = ""

    def __post_init__(self):
        z = np.asarray(self.z, dtype=float)
        info = np.asarray(self.information, dtype=float)
        if z.shape != (3,):
            raise GraphStructureError(
                f"edge {self.i}->{self.j}: measurement must have shape (3,), got {z.shape}"
            )
        if info.shape != (3, 3):
            raise GraphStructureError(
                f"edge {self.i}->{self.j}: information must be a 3x3 matrix, got {info.shape}"
            )
        if not np.all(np.isfinite(z)) or not np.all(np.isfinite(info)):
            raise GraphStructureError(f"edge {self.i}->{self.j}: non-finite values")
        eigvals = np.linalg.eigvalsh(0.5 * (info + info.T))
        if np.any(eigvals < -1e-10):
            raise GraphStructureError(
                f"edge {self.i}->{self.j}: information matrix must be PSD "
                f"(min eigenvalue {eigvals.min():.3e})"
            )
        # Store the symmetric part and a wrapped angle measurement (immutably).
        z = z.copy()
        z[2] = wrap_angle(z[2])
        object.__setattr__(self, "z", z)
        object.__setattr__(self, "information", 0.5 * (info + info.T))


class _UnionFind:
    """Classic disjoint-set structure with path compression and union by rank."""

    def __init__(self, n):
        self.parent = list(range(n))
        self.rank = [0] * n

    def find(self, x):
        root = x
        while self.parent[root] != root:
            root = self.parent[root]
        while self.parent[x] != x:
            self.parent[x], x = root, self.parent[x]
        return root

    def union(self, a, b):
        ra, rb = self.find(a), self.find(b)
        if ra == rb:
            return
        if self.rank[ra] < self.rank[rb]:
            ra, rb = rb, ra
        self.parent[rb] = ra
        if self.rank[ra] == self.rank[rb]:
            self.rank[ra] += 1


@dataclass
class ComponentInfo:
    """Diagnostic information for one connected component of the graph."""

    member_nodes: list
    edges: list
    fixed_nodes: list
    anchored: bool = False  # True if the component contains (or receives) an anchor

    @property
    def gauge_dof(self) -> int:
        """Unspecified SE2 degrees of freedom (0 once anchored)."""
        return 0 if self.anchored else 3


@dataclass
class PoseGraph:
    """Container for pose-graph nodes, edges, and fixed-node bookkeeping."""

    nodes: list = field(default_factory=list)
    edges: list = field(default_factory=list)
    fixed_nodes: set = field(default_factory=set)
    _node_map: dict = field(default_factory=dict, repr=False)

    def add_node(self, index, pose, fixed=False):
        """Register a node (indices need not be added in order)."""
        if index in self._node_map:
            raise GraphStructureError(f"duplicate node index {index}")
        node = Node(index=index, initial_pose=np.asarray(pose, dtype=float))
        self._node_map[index] = node
        self.nodes.append(node)
        if fixed:
            self.fixed_nodes.add(index)
        return node

    def fix_node(self, index):
        if index not in self._node_map:
            raise GraphStructureError(f"cannot fix unknown node {index}")
        self.fixed_nodes.add(index)

    def add_edge(
        self, i, j, z, information, kernel_type="linear", kernel_parameter=None, label=""
    ):
        if i not in self._node_map or j not in self._node_map:
            raise GraphStructureError(f"edge {i}->{j}: references unknown node")
        edge = Edge(
            i=i,
            j=j,
            z=z,
            information=information,
            kernel_type=kernel_type,
            kernel_parameter=kernel_parameter,
            label=label,
        )
        self.edges.append(edge)
        return edge

    def num_nodes(self):
        return len(self.nodes)

    def num_edges(self):
        return len(self.edges)

    def ordered_indices(self):
        """Node indices sorted ascending; this defines the state vector order."""
        return sorted(self._node_map)

    def get_node(self, index):
        return self._node_map[index]

    def connected_components(self):
        """Return :class:`ComponentInfo` objects for every connected component."""
        if not self.nodes:
            return []
        order = self.ordered_indices()
        uf = _UnionFind(len(order))
        pos = {idx: k for k, idx in enumerate(order)}
        for e in self.edges:
            uf.union(pos[e.i], pos[e.j])

        members_by_root = {}
        for k, idx in enumerate(order):
            members_by_root.setdefault(uf.find(k), []).append(idx)

        components = []
        for root, members in members_by_root.items():
            member_set = set(members)
            comp_edges = [e for e in self.edges if e.i in member_set]
            fixed = sorted(member_set & self.fixed_nodes)
            components.append(
                ComponentInfo(
                    member_nodes=sorted(members),
                    edges=comp_edges,
                    fixed_nodes=fixed,
                    anchored=bool(fixed),
                )
            )
        # Largest component first; deterministic tie-break by smallest node id.
        components.sort(key=lambda c: (-len(c.member_nodes), c.member_nodes[0]))
        return components

    def diagnose(self):
        """Human- and machine-readable connectivity diagnostics.

        Returns a dict with component lists, a boolean ``is_connected`` flag and
        the list of components lacking an anchor (gauge freedom unresolved).
        """
        components = self.connected_components()
        report = {
            "num_nodes": self.num_nodes(),
            "num_edges": self.num_edges(),
            "is_connected": len(components) <= 1,
            "num_components": len(components),
            "components": [
                {
                    "member_nodes": c.member_nodes,
                    "num_nodes": len(c.member_nodes),
                    "num_edges": len(c.edges),
                    "fixed_nodes": c.fixed_nodes,
                    "anchored": c.anchored,
                }
                for c in components
            ],
            "unanchored_components": [
                c.member_nodes for c in components if not c.anchored
            ],
        }
        return report
