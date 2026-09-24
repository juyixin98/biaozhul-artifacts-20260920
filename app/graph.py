"""Graph parsing, rotation validation and first-order covariance propagation."""

from __future__ import annotations

from collections import deque
from dataclasses import dataclass, field

import numpy as np

from .linalg_utils import validate_covariance, validate_cross_block
from .se3 import EPS, adjoint, compose, exp_se3, invert, log_se3, make_T, quat_to_R

ROT_ORTHO_TOL = 1.0e-6
ROT_DET_TOL = 1.0e-6
QUAT_NORM_TOL = 5.0e-3


@dataclass
class Edge:
    edge_id: str
    parent_frame: str  # transform maps points INTO this frame
    child_frame: str  # transform maps points FROM this frame
    T: np.ndarray  # child -> parent
    R_raw: np.ndarray
    cov: np.ndarray | None  # 6x6 or None when unknown
    convention: str  # "right" | "left"
    index: int

    @property
    def frames(self) -> tuple[str, str]:
        return (self.parent_frame, self.child_frame)


@dataclass
class ParseResult:
    edges: list[Edge]
    findings: list[dict] = field(default_factory=list)
    edge_by_id: dict[str, Edge] = field(default_factory=dict)
    adjacency: dict[str, list[tuple[str, Edge]]] = field(default_factory=dict)


def _rotation_evidence(edge_id: str, R: np.ndarray, detail: str, **extra) -> dict:
    out = {
        "kind": "illegal_rotation",
        "edge_id": edge_id,
        "detail": detail,
        "message": f"edge {edge_id}: illegal rotation matrix ({detail})",
    }
    out.update(extra)
    return out


def parse_rotation(edge_id: str, spec) -> tuple[np.ndarray | None, list[dict]]:
    """Return (R, findings). R is None when the rotation is unusable."""
    findings: list[dict] = []
    if spec.kind == "quat_wxyz":
        q = spec.quat_wxyz
        if q is None or len(q) != 4:
            findings.append(
                _rotation_evidence(edge_id, np.eye(3), "quaternion_must_be_length_4")
            )
            return None, findings
        q_arr = np.asarray(q, dtype=float)
        if not np.all(np.isfinite(q_arr)):
            findings.append(_rotation_evidence(edge_id, np.eye(3), "quaternion_not_finite"))
            return None, findings
        n = float(np.linalg.norm(q_arr))
        if n < 1e-12:
            findings.append(
                _rotation_evidence(edge_id, np.eye(3), "quaternion_zero_norm", norm=n)
            )
            return None, findings
        if abs(n - 1.0) > QUAT_NORM_TOL:
            findings.append(
                _rotation_evidence(
                    edge_id,
                    np.eye(3),
                    "quaternion_not_unit",
                    norm=n,
                    tolerance=QUAT_NORM_TOL,
                )
            )
            return None, findings
        return quat_to_R(q_arr), findings

    if spec.matrix is None:
        findings.append(_rotation_evidence(edge_id, np.eye(3), "rotation_matrix_missing"))
        return None, findings
    R = np.asarray(spec.matrix, dtype=float)
    if R.shape != (3, 3):
        findings.append(
            _rotation_evidence(
                edge_id, R, "rotation_matrix_not_3x3", shape=list(R.shape)
            )
        )
        return None, findings
    if not np.all(np.isfinite(R)):
        findings.append(_rotation_evidence(edge_id, R, "rotation_not_finite"))
        return None, findings
    err_I = float(np.max(np.abs(R.T @ R - np.eye(3))))
    det = float(np.linalg.det(R))
    if err_I > ROT_ORTHO_TOL:
        findings.append(
            _rotation_evidence(
                edge_id,
                R,
                "not_orthogonal",
                max_abs_RTR_minus_I=err_I,
                tolerance=ROT_ORTHO_TOL,
            )
        )
        return None, findings
    if abs(det - 1.0) > ROT_DET_TOL:
        findings.append(
            _rotation_evidence(
                edge_id, R, "det_not_plus_one", det=det, tolerance=ROT_DET_TOL
            )
        )
        return None, findings
    return R, findings


def parse_edges(edge_specs, convention: str) -> ParseResult:
    res = ParseResult(edges=[], findings=[], edge_by_id={}, adjacency={})
    used_auto = 0
    for idx, spec in enumerate(edge_specs):
        eid = spec.edge_id or f"edge[{idx}]"
        if eid in res.edge_by_id:
            res.findings.append(
                {
                    "kind": "duplicate_edge_id",
                    "edge_id": eid,
                    "message": (
                        f"duplicate edge id {eid!r}; edge rejected, assign "
                        "unique edge ids"
                    ),
                }
            )
            continue
        edge_conv = spec.convention or convention
        if spec.convention is not None and spec.convention != convention:
            res.findings.append(
                {
                    "kind": "mixed_convention",
                    "edge_id": eid,
                    "requested": convention,
                    "edge": spec.convention,
                    "message": (
                        f"edge {eid} declares convention {spec.convention!r} but "
                        f"the audit convention is fixed to {convention!r}; "
                        "edge rejected: unify the convention before resubmitting"
                    ),
                }
            )
            continue
        R, rot_findings = parse_rotation(eid, spec.rotation)
        res.findings.extend(rot_findings)
        if R is None:
            continue
        t = np.asarray(spec.translation, dtype=float)
        if not np.all(np.isfinite(t)):
            res.findings.append(
                {
                    "kind": "translation_not_finite",
                    "edge_id": eid,
                    "message": f"edge {eid}: translation contains NaN/Inf",
                }
            )
            continue
        T = make_T(R, t)
        cov = None
        if spec.covariance is not None:
            S, ev = validate_covariance(np.asarray(spec.covariance, dtype=float), eid)
            if ev is not None:
                res.findings.append(ev)
            if S is not None:
                cov = S
        edge = Edge(
            edge_id=eid,
            parent_frame=spec.parent_frame,
            child_frame=spec.child_frame,
            T=T,
            R_raw=R,
            cov=cov,
            convention=edge_conv,
            index=idx,
        )
        res.edges.append(edge)
        res.edge_by_id.setdefault(eid, edge)
        for f in (spec.parent_frame, spec.child_frame):
            res.adjacency.setdefault(f, [])
        res.adjacency[spec.parent_frame].append((spec.child_frame, edge))
        res.adjacency[spec.child_frame].append((spec.parent_frame, edge))
        used_auto += 1
    return res


# --------------------------------------------------------------------------- #
# Traversal
# --------------------------------------------------------------------------- #


@dataclass
class Step:
    edge: Edge
    forward: bool  # True: child -> parent (T); False: parent -> child (T^-1)
    frame_from: str
    frame_to: str

    @property
    def X(self) -> np.ndarray:
        return self.edge.T if self.forward else invert(self.edge.T)


def _pick_edge(adj_entries, other_frame, edge_id=None):
    cands = [
        (nb, e)
        for nb, e in adj_entries
        if nb == other_frame and (edge_id is None or e.edge_id == edge_id)
    ]
    if not cands:
        return None
    if edge_id is None and len({id(e) for _, e in cands}) > 1:
        return "ambiguous"
    return cands[0]


def build_steps(parse: ParseResult, frame_path) -> tuple[list[Step] | None, dict | None]:
    """Resolve an ordered frame walk to directed steps."""
    steps: list[Step] = []
    for a, b in zip(frame_path[:-1], frame_path[1:]):
        if a == b:
            return None, {
                "kind": "degenerate_walk",
                "frame": a,
                "message": f"zero-length step on frame {a!r}",
            }
        if a not in parse.adjacency:
            return None, {
                "kind": "unknown_frame",
                "frame": a,
                "message": f"frame {a!r} not present in any edge",
            }
        picked = _pick_edge(parse.adjacency[a], b)
        if picked is None:
            return None, {
                "kind": "no_edge_between_frames",
                "frames": [a, b],
                "message": f"no edge connects {a!r} and {b!r}",
            }
        if picked == "ambiguous":
            return None, {
                "kind": "ambiguous_edge",
                "frames": [a, b],
                "message": (
                    f"multiple parallel edges connect {a!r} and {b!r}; "
                    "loops/chains between parallel edges must use edge_id qualifiers"
                ),
            }
        _, edge = picked
        # Forward traversal a -> b uses T (which maps child -> parent), so we
        # leave FROM the child and arrive AT the parent: a == child, b == parent.
        forward = edge.child_frame == a and edge.parent_frame == b
        steps.append(Step(edge=edge, forward=forward, frame_from=a, frame_to=b))
    return steps, None


def chain_transform(steps: list[Step]) -> np.ndarray:
    """Walk product ``X_{n-1} ... X_1 X_0`` (later steps leftmost).

    Step ``X_k`` maps coordinates from frame ``F_k`` to ``F_{k+1}`` along the
    walk; composing them in this order maps ``F_0`` coordinates into ``F_n``.
    """
    T = np.eye(4)
    for s in steps:
        T = s.X @ T
    return T


def step_jacobian(
    steps: list[Step], k: int, convention: str, *, closed: bool, T_total: np.ndarray
) -> np.ndarray:
    """Map step k's local tangent perturb into the reported increment frame.

    The walk product is ``W = X_{n-1} ... X_0`` (later steps leftmost).

      * right convention (body increment ``log(W' W^{-1})``): coefficient
        ``Ad(H_k)`` with ``H_k = X_{n-1} ... X_k``;
      * left convention (spatial increment ``log(W^{-1} W')``): coefficient
        ``Ad(P_k^{-1})`` with ``P_k = X_k ... X_0``.

    Both are FD-verified to ~1e-10. For a closed loop ``W = I`` the same
    formulas apply unchanged (``closed``/``T_total`` do not alter them).
    """
    if convention == "right":
        # H_k = X_{n-1} ... X_{k+1} X_k  (later steps leftmost)
        H = np.eye(4)
        for s in steps[k:]:
            H = s.X @ H
        return adjoint(H)
    # P_k = X_k X_{k-1} ... X_0 (earlier steps rightmost)
    P = np.eye(4)
    for s in reversed(steps[: k + 1]):
        P = P @ s.X
    return adjoint(invert(P))


def reverse_edge_map(edge: Edge, convention: str) -> np.ndarray:
    """Map an edge perturb into the reverse-step tangent frame.

    Right pert: (T Exp(x))^{-1} = T^{-1} Exp(-Ad(T) x) -> factor Ad(T).
    Left pert:  (Exp(x) T)^{-1} = Exp(-Ad(T^{-1}) x) T^{-1} -> factor Ad(T^{-1}).
    """
    if convention == "right":
        return adjoint(edge.T)
    return adjoint(invert(edge.T))


def step_local_cov(step: Step, convention: str) -> np.ndarray | None:
    """Covariance of the step's transform in its own tangent frame."""
    S = step.edge.cov
    if S is None:
        return None
    if step.forward:
        return S
    A = reverse_edge_map(step.edge, convention)
    return A @ S @ A.T


# --------------------------------------------------------------------------- #
# Fundamental cycles (undirected spanning tree)
# --------------------------------------------------------------------------- #


def enumerate_fundamental_loops(
    parse: ParseResult, max_edges: int, max_count: int
) -> list[list[str]]:
    """Return fundamental frame cycles via a BFS spanning tree.

    Each non-tree edge produces the unique tree path closing through it.
    Cycles longer than ``max_edges`` are skipped.
    """
    if not parse.adjacency:
        return []
    start = next(iter(parse.adjacency))
    seen_frames = {start}
    parent: dict[str, tuple[str, Edge] | None] = {start: None}
    tree_edges: set[int] = set()
    q = deque([start])
    non_tree: list[tuple[str, str, Edge]] = []
    recorded_non_tree: set[int] = set()
    while q:
        u = q.popleft()
        for v, e in parse.adjacency[u]:
            if id(e) in tree_edges:
                continue
            if v not in seen_frames:
                seen_frames.add(v)
                parent[v] = (u, e)
                tree_edges.add(id(e))
                q.append(v)
            elif id(e) not in recorded_non_tree:
                # only record the non-tree edge once, from one endpoint
                non_tree.append((u, v, e))
                recorded_non_tree.add(id(e))

    def tree_path(a: str, b: str) -> list[str] | None:
        # path a -> ... -> b over tree edges
        def ancestors(x):
            chain = [x]
            while parent[chain[-1]] is not None:
                chain.append(parent[chain[-1]][0])
            return chain

        up_a, up_b = ancestors(a), ancestors(b)
        set_b = set(up_b)
        lca = next(x for x in up_a if x in set_b)
        part = up_a[: up_a.index(lca)]  # a up to (not incl) lca
        down = up_b[: up_b.index(lca)][::-1]
        return part + [lca] + down

    loops: list[list[str]] = []
    for u, v, e in non_tree:
        path = tree_path(u, v)
        if path is None:
            continue
        cycle = path + [u]  # tree path u->v then edge v->u closes at u
        if len(cycle) - 1 > max_edges:
            continue
        loops.append(cycle)
        if len(loops) >= max_count:
            break
    loops.sort(key=lambda c: (len(c), c))
    return loops


# --------------------------------------------------------------------------- #
# First-order assembly
# --------------------------------------------------------------------------- #


@dataclass
class PropagationResult:
    Sigma: np.ndarray | None  # None when covariance is missing/indeterminate
    steps: list[Step]
    missing_edges: list[str]
    findings: list[dict]
    jacobians: list[np.ndarray]
    local_covs: list[np.ndarray]


def assemble_propagation(
    parse: ParseResult,
    steps: list[Step],
    convention: str,
    policy: str,
    rho,
    cross_block_specs,
    *,
    closed: bool,
    T_total: np.ndarray,
) -> PropagationResult:
    findings: list[dict] = []
    missing = [s.edge.edge_id for s in steps if s.edge.cov is None]
    jacobians = [
        step_jacobian(steps, k, convention, closed=closed, T_total=T_total)
        for k in range(len(steps))
    ]
    local_covs = [step_local_cov(s, convention) for s in steps]

    if missing:
        findings.append(
            {
                "kind": "missing_covariance",
                "edge_ids": missing,
                "message": (
                    "covariance unknown for edge(s) "
                    + ", ".join(missing)
                    + "; it is NOT treated as zero. Provide the covariance, "
                    "or run a dedicated Monte Carlo check with an assumed prior."
                ),
            }
        )
        return PropagationResult(
            Sigma=None,
            steps=steps,
            missing_edges=missing,
            findings=findings,
            jacobians=jacobians,
            local_covs=[c for c in local_covs if c is not None],
        )

    n = len(steps)
    # map edge_id -> step position(s); cross blocks refer to edge ids
    id_to_positions: dict[str, list[int]] = {}
    for k, s in enumerate(steps):
        id_to_positions.setdefault(s.edge.edge_id, []).append(k)

    cross_map: dict[tuple[int, int], np.ndarray] = {}
    if policy == "cross_blocks":
        for cb in cross_block_specs:
            if cb.i not in parse.edge_by_id or cb.j not in parse.edge_by_id:
                findings.append(
                    {
                        "kind": "unknown_cross_block_edge",
                        "edge_ids": [cb.i, cb.j],
                        "message": f"cross block references unknown edge {cb.i!r} or {cb.j!r}",
                    }
                )
                continue
            pi, pj = id_to_positions[cb.i][0], id_to_positions[cb.j][0]
            ei, ej = parse.edge_by_id[cb.i], parse.edge_by_id[cb.j]
            C = np.asarray(cb.block, dtype=float)
            if C.shape != (6, 6) or not np.all(np.isfinite(C)):
                findings.append(
                    {
                        "kind": "bad_cross_block_shape",
                        "edge_ids": [cb.i, cb.j],
                        "shape": list(C.shape),
                    }
                )
                continue
            if pi > pj:
                pi, pj = pj, pi
                C = C.T
            # orient the block to the actual traversal steps
            if not steps[pi].forward:
                A = reverse_edge_map(steps[pi].edge, convention)
                C = A @ C
            if not steps[pj].forward:
                B = reverse_edge_map(steps[pj].edge, convention)
                C = C @ B.T
            wmin = validate_cross_block(C, local_covs[pi], local_covs[pj])
            if wmin is None or (wmin is not None and not np.isnan(wmin) and wmin < -1e-7):
                findings.append(
                    {
                        "kind": "cross_block_inconsistent",
                        "edge_ids": [cb.i, cb.j],
                        "min_eigenvalue": wmin,
                        "message": (
                            f"cross block ({cb.i},{cb.j}) makes the joint "
                            f"covariance indefinite (min eigenvalue {wmin})"
                        ),
                    }
                )
                continue
            cross_map[(pi, pj)] = C

    # rho can be a scalar or an n x n matrix keyed by request-edge order;
    # for traversals we index by step position in request-edge order.
    rho_mat = None
    if policy == "conservative_rho":
        if rho is None:
            findings.append(
                {
                    "kind": "rho_missing",
                    "message": "correlation_policy=conservative_rho requires rho",
                }
            )
            rho_mat = np.zeros((n, n))
        elif np.isscalar(rho):
            rho_mat = np.full((n, n), float(rho))
        else:
            # matrix indexed by request edge order -> re-index to step order
            req = np.asarray(rho, dtype=float)
            order = [s.edge.index for s in steps]
            if req.ndim != 2 or req.shape[0] <= max(order, default=-1):
                findings.append(
                    {
                        "kind": "rho_bad_shape",
                        "shape": list(req.shape),
                        "message": "rho matrix does not match the number of edges",
                    }
                )
                rho_mat = np.zeros((n, n))
            else:
                rho_mat = req[np.ix_(order, order)]

    from .linalg_utils import propagate_sum

    Sigma, notes = propagate_sum(
        local_covs,
        jacobians,
        policy=policy,
        rho=rho_mat if rho_mat is not None else 0.0,
        cross_blocks=cross_map,
    )
    findings.extend(notes)

    if policy == "independent":
        findings.append(
            {
                "kind": "independence_assumed",
                "message": (
                    "correlation_policy=independent: edge perturbations are "
                    "EXPLICITLY assumed mutually uncorrelated. If that is not "
                    "justified, use conservative_rho or supply cross_blocks."
                ),
            }
        )

    return PropagationResult(
        Sigma=Sigma,
        steps=steps,
        missing_edges=[],
        findings=findings,
        jacobians=jacobians,
        local_covs=local_covs,
    )


def loop_residual(steps: list[Step]) -> np.ndarray:
    """6-vector residual vee(log(T_loop)) and the raw loop transform."""
    Tloop = chain_transform(steps)
    return log_se3(Tloop), Tloop
