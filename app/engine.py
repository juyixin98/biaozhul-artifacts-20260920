"""审计引擎：把协议模型转换为数值对象，执行校验、传播、闭环与版本审计。"""

from __future__ import annotations

from collections import defaultdict

import numpy as np

from .chain import (
    Edge,
    bfs_path,
    build_graph,
    evaluate_loop,
    fundamental_cycles,
    spanning_tree_poses,
)
from .se3 import make_tf, rot_to_quat
from .validation import validate_cross_covariance, validate_covariance, validate_rotation


class AuditError(Exception):
    def __init__(self, issues: list[dict]):
        self.issues = issues
        super().__init__(f"{len(issues)} validation issue(s)")


def _build_edges(req) -> tuple[list[Edge], list[dict]]:
    issues: list[dict] = []
    edges: list[Edge] = []
    for em in req.edges:
        where = f"edge:{em.id}"
        R, r_issues = validate_rotation(em.transform.rotation, where)
        issues.extend(r_issues)
        t = np.asarray(em.transform.translation, dtype=float)
        t_ok = tuple(t.shape) == (3,) and bool(np.isfinite(t).all())
        if not t_ok:
            issues.append(
                {
                    "code": "MALFORMED_TRANSFORM",
                    "message": f"{where}: translation 必须为长度 3 的有限数值",
                    "evidence_path": [where],
                }
            )
        cov_issues: list[dict] = []
        C = None
        if em.covariance is not None:
            C, cov_issues = validate_covariance(em.covariance, where)
            issues.extend(cov_issues)
        if R is not None and t_ok:
            edges.append(
                Edge(
                    id=em.id,
                    parent=em.parent,
                    child=em.child,
                    T=make_tf(t, R),
                    covariance=C,
                    version=em.version,
                    has_covariance=em.covariance is not None,
                )
            )
    return edges, issues


def _cross_cov_map(req, issues: list[dict]) -> dict[tuple[str, str], np.ndarray]:
    cross: dict[tuple[str, str], np.ndarray] = {}
    edge_ids = {e.id for e in req.edges}
    for cc in req.cross_covariances or []:
        if cc.edge_a == cc.edge_b:
            issues.append(
                {
                    "code": "MALFORMED_COVARIANCE",
                    "message": f"互协方差边对必须不同: {cc.edge_a},{cc.edge_b}",
                    "evidence_path": [f"edge:{cc.edge_a}", f"edge:{cc.edge_b}"],
                }
            )
            continue
        if cc.edge_a not in edge_ids or cc.edge_b not in edge_ids:
            issues.append(
                {
                    "code": "UNKNOWN_EDGE_REF",
                    "message": (
                        f"互协方差引用了不存在的边: {cc.edge_a},{cc.edge_b}"
                    ),
                    "evidence_path": [f"edge:{cc.edge_a}", f"edge:{cc.edge_b}"],
                }
            )
            continue
        C, ci = validate_cross_covariance(cc.matrix, cc.edge_a, cc.edge_b)
        issues.extend(ci)
        if C is not None:
            cross[tuple(sorted((cc.edge_a, cc.edge_b)))] = C
    return cross


def _check_joint_covariance_psd(
    edges: list[Edge], cross: dict[tuple[str, str], np.ndarray]
) -> list[dict]:
    """对每个由互协方差连通的边连通分量，组装联合 6m×6m 协方差并验 PSD。

    逐对边的互协方差可能各自有限但使联合矩阵不定；Mahalanobis 需要整个
    联合协方差半正定，故必须在分量层面检查。
    """
    import numpy as np

    issues: list[dict] = []
    cov_by_id = {
        e.id: e.covariance for e in edges if e.has_covariance and e.covariance is not None
    }
    # 并查集（只针对出现在互协方差中的边）
    parent: dict[str, str] = {}

    def find(x: str) -> str:
        parent.setdefault(x, x)
        while parent[x] != x:
            parent[x] = parent[parent[x]]
            x = parent[x]
        return x

    def union(a: str, b: str) -> None:
        parent[find(a)] = find(b)

    for a, b in cross:
        if a in cov_by_id and b in cov_by_id:
            union(a, b)

    # 收集每个连通分量的边
    comp: dict[str, set[str]] = {}
    for a, b in cross:
        if a in cov_by_id and b in cov_by_id:
            r = find(a)
            comp.setdefault(r, set()).update([a, b])

    for root_id, members in comp.items():
        ids = sorted(members)
        m = len(ids)
        idx = {eid: k for k, eid in enumerate(ids)}
        M = np.zeros((6 * m, 6 * m))
        for eid in ids:
            k = idx[eid]
            M[6 * k : 6 * (k + 1), 6 * k : 6 * (k + 1)] = cov_by_id[eid]
        for (a, b), Cab in cross.items():
            if a not in idx or b not in idx:
                continue
            ia, ib = idx[a], idx[b]
            M[6 * ia : 6 * (ia + 1), 6 * ib : 6 * (ib + 1)] = Cab
            M[6 * ib : 6 * (ib + 1), 6 * ia : 6 * (ia + 1)] = Cab.T
        eig = np.linalg.eigvalsh(0.5 * (M + M.T))
        if eig.min() < -1e-10:
            issues.append(
                {
                    "code": "JOINT_COVARIANCE_NOT_PSD",
                    "message": (
                        f"互协方差使边组 {ids} 的联合协方差非正定，"
                        f"最小特征值={eig.min():.6e}（交叉项相对对角块过大）"
                    ),
                    "evidence_path": [f"edge:{i}" for i in ids],
                    "details": {
                        "edges": ids,
                        "eigenvalue_min": float(eig.min()),
                    },
                }
            )
    return issues



def _pose_report(pe) -> dict:
    q = rot_to_quat(pe.T_root_node[:3, :3])
    return {
        "node": pe.node,
        "path_edges": pe.path_edges,
        "translation": pe.T_root_node[:3, 3].tolist(),
        "rotation_quaternion_wxyz": q.tolist(),
        "covariance": pe.covariance.tolist() if pe.covariance is not None else None,
        "covariance_status": pe.covariance_status,
        "missing_edges": pe.missing_edges,
    }


def _version_mismatches(edges: list[Edge]) -> list[dict]:
    """同一坐标系上挂多条不同版本的边 -> 潜在的标定版本混用。

    报告每个节点汇集到的版本集合；多版本即给证据（节点 + 相关边）。
    """
    node_versions: dict[str, dict[str, list[str]]] = defaultdict(
        lambda: defaultdict(list)
    )
    for e in edges:
        node_versions[e.parent][e.version].append(e.id)
        node_versions[e.child][e.version].append(e.id)

    out: list[dict] = []
    for node in sorted(node_versions):
        vmap = node_versions[node]
        if len(vmap) > 1:
            versions = sorted(vmap)
            edge_ids = sorted({x for lst in vmap.values() for x in lst})
            out.append(
                {
                    "node": node,
                    "versions": versions,
                    "edge_ids": edge_ids,
                    "message": (
                        f"节点 {node} 汇集了多个标定版本 {versions}，"
                        "闭环/传播前应确认版本一致性"
                    ),
                    "evidence_path": [f"node:{node}"]
                    + [f"edge:{i}" for i in edge_ids],
                }
            )
    return out


def run_audit(req) -> dict:
    """执行完整审计。校验失败抛 AuditError(issues)。"""
    edges, issues = _build_edges(req)
    cross = _cross_cov_map(req, issues)
    if not issues:
        issues.extend(_check_joint_covariance_psd(edges, cross))

    # 根存在性
    all_nodes = {e.parent for e in edges} | {e.child for e in edges}
    if req.root_frame not in all_nodes:
        issues.append(
            {
                "code": "UNKNOWN_ROOT",
                "message": f"root_frame={req.root_frame} 不在任何边的端点中",
                "evidence_path": [f"node:{req.root_frame}"],
            }
        )

    if issues:
        raise AuditError(issues)

    graph = build_graph(edges)

    # 根连通性（BFS）
    chain = bfs_path(graph, req.root_frame, req.root_frame)
    poses, tree_issues = spanning_tree_poses(
        graph,
        edges,
        req.root_frame,
        req.convention,
        req.correlation_policy,
        req.rho_max,
        cross,
    )
    if tree_issues:
        raise AuditError(tree_issues)

    # 闭环（基本环）
    cycles = fundamental_cycles(graph, req.root_frame)
    loops = []
    any_conflict = False
    for cyc in cycles:
        lr = evaluate_loop(
            cyc,
            graph,
            req.convention,
            req.correlation_policy,
            req.rho_max,
            cross,
            req.alpha,
        )
        any_conflict = any_conflict or lr.conflict
        loops.append(
            {
                "nodes": lr.nodes,
                "edge_ids": lr.edge_ids,
                "evidence_path": lr.evidence_path,
                "versions": lr.versions,
                "residual": lr.residual.tolist(),
                "residual_norm": lr.residual_norm,
                "rotation_angle": lr.rotation_angle,
                "covariance": lr.covariance.tolist()
                if lr.covariance is not None
                else None,
                "covariance_status": lr.covariance_status,
                "mahalanobis_sq": lr.mahalanobis_sq,
                "p_value": lr.p_value,
                "conflict": lr.conflict,
                "conflict_reason": lr.conflict_reason,
                "missing_edges": lr.missing_edges,
            }
        )

    version_mismatches = _version_mismatches(edges)

    # 相关性声明回显（显式列出哪些边对按独立处理——绝不沉默假设）
    all_ids = sorted(e.id for e in edges)
    declared_independent: list[list[str]] = []
    bounded_pairs: list[list[str]] = []
    explicit_pairs = sorted([list(k) for k in cross])
    if req.correlation_policy == "independent":
        declared_independent = [
            [a, b] for i, a in enumerate(all_ids) for b in all_ids[i + 1 :]
        ]
    elif req.correlation_policy == "bounded":
        bounded_pairs = [
            [a, b] for i, a in enumerate(all_ids) for b in all_ids[i + 1 :]
        ]
    else:  # explicit
        cross_keys = {tuple(k) for k in cross}
        for i, a in enumerate(all_ids):
            for b in all_ids[i + 1 :]:
                pair = tuple(sorted((a, b)))
                if pair in cross_keys:
                    continue
                if req.unspecified_policy == "independent":
                    declared_independent.append([a, b])
                else:
                    bounded_pairs.append([a, b])

    unknown_loops = [
        l["edge_ids"] for l in loops if l["covariance_status"] == "unknown"
    ]
    overall = {
        "status": "conflict" if any_conflict else "ok",
        "n_edges": len(edges),
        "n_nodes": len(poses),
        "n_loops": len(loops),
        "n_conflicts": sum(1 for l in loops if l["conflict"]),
        "n_loops_untestable_missing_covariance": len(unknown_loops),
        "n_version_mismatches": len(version_mismatches),
        "untestable_loops": unknown_loops,
    }

    return {
        "root_frame": req.root_frame,
        "convention": req.convention,
        "alpha": req.alpha,
        "poses": {k: _pose_report(v) for k, v in sorted(poses.items())},
        "loops": loops,
        "version_mismatches": [
            {
                "node": m["node"],
                "edge_ids": m["edge_ids"],
                "versions": m["versions"],
                "message": m["message"],
                "evidence_path": m["evidence_path"],
            }
            for m in version_mismatches
        ],
        "correlation_declaration": {
            "policy": req.correlation_policy,
            "rho_max": req.rho_max,
            "unspecified_policy": req.unspecified_policy,
            "declared_independent_pairs": declared_independent,
            "bounded_pairs": bounded_pairs,
            "explicit_pairs": explicit_pairs,
        },
        "overall": overall,
    }
