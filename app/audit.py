"""High-level audit orchestration: parse, propagate, check loop closures."""

from __future__ import annotations

import numpy as np

from .graph import (
    assemble_propagation,
    build_steps,
    chain_transform,
    enumerate_fundamental_loops,
    loop_residual,
    parse_edges,
)
from .se3 import EPS

MISSING_COV_MAHALANOBIS = None

# Findings at these severities stop numeric propagation because the input
# cannot support it.
FATAL_KINDS = {
    "illegal_rotation",
    "covariance_not_psd",
    "covariance_not_square",
    "covariance_not_finite",
    "translation_not_finite",
    "mixed_convention",
    "duplicate_edge_id",
}


def _edge_summary(parse) -> list[dict]:
    out = []
    for e in parse.edges:
        out.append(
            {
                "edge_id": e.edge_id,
                "parent_frame": e.parent_frame,
                "child_frame": e.child_frame,
                "has_covariance": e.cov is not None,
                "convention": e.convention,
            }
        )
    return out


def _rho_entries(rho, steps) -> dict | None:
    if rho is None:
        return None
    if np.isscalar(rho):
        return {"type": "scalar", "value": float(rho)}
    m = np.asarray(rho, dtype=float)
    order = [s.edge.index for s in steps] if steps else list(range(m.shape[0]))
    sub = m[np.ix_(order, order)] if m.ndim == 2 else m
    return {"type": "matrix", "matrix": sub.tolist()}


def audit_loops(req) -> dict:
    convention = req.convention
    parse = parse_edges(req.edges, convention)
    findings = list(parse.findings)

    fatal = [f for f in findings if f["kind"] in FATAL_KINDS]
    result: dict = {
        "calibration_version": req.calibration_version,
        "convention": convention,
        "convention_formula": (
            "T = Tbar * Exp(xi)" if convention == "right" else "T = Exp(xi) * Tbar"
        ),
        "correlation_policy": req.correlation_policy,
        "tangent_order": "[rho_x, rho_y, rho_z, phi_x, phi_y, phi_z]",
        "edges": _edge_summary(parse),
        "edges_submitted": len(req.edges),
        "edges_usable": len(parse.edges),
        "findings": findings,
        "loops": [],
        "chains": [],
        "fatal": bool(fatal),
    }

    if fatal:
        # Shortest evidence path: the offending edge is already identified in
        # each finding; surface them directly rather than propagating garbage.
        result["summary"] = {
            "status": "rejected",
            "reason": "fatal input errors; covariance propagation not performed",
            "fatal_count": len(fatal),
            "conflicting_loops": 0,
        }
        return result

    # ----- validate rho shape/range up front (per submitted-edge order) -----
    if req.correlation_policy == "conservative_rho" and req.rho is not None:
        rho_arr = np.asarray(req.rho, dtype=float)
        if rho_arr.ndim == 2:
            m = len(req.edges)
            if rho_arr.shape != (m, m):
                findings.append(
                    {
                        "kind": "rho_bad_shape",
                        "shape": list(rho_arr.shape),
                        "expected": [m, m],
                        "message": f"rho matrix must be {m}x{m} (one entry per edge)",
                    }
                )
        if np.any(rho_arr < -1e-12) or np.any(rho_arr > 1.0 + 1e-9):
            findings.append(
                {
                    "kind": "rho_out_of_range",
                    "min": float(np.nanmin(rho_arr)),
                    "max": float(np.nanmax(rho_arr)),
                    "message": "rho entries must lie in [0, 1]",
                }
            )

    # ----- determine loops -----
    if req.loops:
        loop_walks = [
            {"name": lp.name, "frame_ids": list(lp.frame_ids), "source": "explicit"}
            for lp in req.loops
        ]
    else:
        auto = enumerate_fundamental_loops(
            parse, req.auto_loop_max_edges, req.auto_loop_max_count
        )
        loop_walks = [
            {"name": f"fundamental[{i}]", "frame_ids": cyc, "source": "auto_spanning_tree"}
            for i, cyc in enumerate(auto)
        ]

    conflict_records = []
    loop_reports = []
    for walk in loop_walks:
        frames = walk["frame_ids"]
        if frames[0] != frames[-1]:
            findings.append(
                {
                    "kind": "loop_not_closed",
                    "loop": walk["name"],
                    "frames": frames,
                    "message": f"loop {walk['name']!r} must start and end on the same frame",
                }
            )
            continue
        if len(frames) < 3:
            findings.append(
                {
                    "kind": "loop_too_short",
                    "loop": walk["name"],
                    "frames": frames,
                }
            )
            continue
        steps, err = build_steps(parse, frames)
        if err is not None:
            err["loop"] = walk["name"]
            findings.append(err)
            continue

        xi, Tloop = loop_residual(steps)
        trans_norm = float(np.linalg.norm(xi[:3]))
        rot_norm = float(np.linalg.norm(xi[3:]))

        prop = assemble_propagation(
            parse,
            steps,
            convention,
            req.correlation_policy,
            req.rho,
            req.cross_blocks,
            closed=True,
            T_total=Tloop,
        )
        findings.extend(prop.findings)

        report = {
            "name": walk["name"],
            "source": walk["source"],
            "frames": frames,
            "edges": [
                {
                    "edge_id": s.edge.edge_id,
                    "direction": (
                        f"{s.frame_from}->{s.frame_to}"
                    ),
                    "traversed_as": "forward (child->parent)"
                    if s.forward
                    else "reverse (parent->child)",
                }
                for s in steps
            ],
            "num_edges": len(steps),
            "residual": {
                "translation": xi[:3].tolist(),
                "rotation_axisangle_rad": xi[3:].tolist(),
                "translation_norm": trans_norm,
                "rotation_norm_rad": rot_norm,
            },
            "loop_transform": np.asarray(Tloop).tolist(),
        }

        if prop.Sigma is None:
            report["closure_test"] = {
                "status": "indeterminate",
                "reason": "missing covariance on one or more edges",
                "missing_edges": prop.missing_edges,
            }
        else:
            Sigma = prop.Sigma
            # Mahalanobis distance, regularised against rank-deficient Sigma
            try:
                mahal = float(xi @ np.linalg.solve(Sigma, xi))
            except np.linalg.LinAlgError:
                # pseudo-inverse fallback (still a real computation)
                pi = np.linalg.pinv(Sigma)
                mahal = float(xi @ pi @ xi)
            conflict = mahal > req.chi2_threshold
            report["closure_test"] = {
                "status": "conflict" if conflict else "ok",
                "mahalanobis_sq": mahal,
                "chi2_threshold": req.chi2_threshold,
                "sigma_diag_sqrt": np.sqrt(np.maximum(np.diag(Sigma), 0)).tolist(),
                "propagated_covariance": Sigma.tolist(),
                "policy": req.correlation_policy,
            }
            if conflict:
                conflict_records.append(
                    {
                        "name": walk["name"],
                        "frames": frames,
                        "num_edges": len(steps),
                        "mahalanobis_sq": mahal,
                        "residual_translation_norm": trans_norm,
                        "residual_rotation_norm_rad": rot_norm,
                    }
                )
        loop_reports.append(report)

    result["loops"] = loop_reports

    # shortest conflict path = fewest edges, then largest Mahalanobis tension
    shortest = None
    if conflict_records:
        shortest = min(
            conflict_records,
            key=lambda c: (
                c["num_edges"],
                -c["mahalanobis_sq"],
                c["name"],
            ),
        )
        findings.append(
            {
                "kind": "loop_closure_conflict",
                "loop": shortest["name"],
                "frames": shortest["frames"],
                "num_edges": shortest["num_edges"],
                "mahalanobis_sq": shortest["mahalanobis_sq"],
                "residual_translation_norm": shortest["residual_translation_norm"],
                "residual_rotation_norm_rad": shortest["residual_rotation_norm_rad"],
                "evidence_path": shortest["frames"],
                "message": (
                    f"loop {shortest['name']!r} closure exceeds chi^2 threshold "
                    f"({shortest['mahalanobis_sq']:.3f} > {req.chi2_threshold:.3f}); "
                    f"shortest conflicting path: {' -> '.join(shortest['frames'])}"
                ),
            }
        )

    result["summary"] = {
        "status": "conflict" if conflict_records else ("ok" if loop_reports else "no_loops"),
        "loops_checked": len(loop_reports),
        "conflicting_loops": len(conflict_records),
        "shortest_conflict_path": (
            shortest["frames"] if shortest else None
        ),
        "shortest_conflict_loop": shortest["name"] if shortest else None,
    }
    return result


def audit_chain(req) -> dict:
    parse = parse_edges(req.edges, req.convention)
    findings = list(parse.findings)
    fatal = [f for f in findings if f["kind"] in FATAL_KINDS]
    result = {
        "calibration_version": req.calibration_version,
        "convention": req.convention,
        "convention_formula": (
            "T = Tbar * Exp(xi)" if req.convention == "right" else "T = Exp(xi) * Tbar"
        ),
        "correlation_policy": req.correlation_policy,
        "edges": _edge_summary(parse),
        "findings": findings,
        "fatal": bool(fatal),
    }
    if fatal:
        result["summary"] = {
            "status": "rejected",
            "reason": "fatal input errors; propagation not performed",
        }
        return result

    steps, err = build_steps(parse, req.frame_path)
    if err is not None:
        findings.append(err)
        result["fatal"] = True
        result["summary"] = {"status": "rejected", "reason": err["kind"]}
        return result

    T_total = chain_transform(steps)
    prop = assemble_propagation(
        parse,
        steps,
        req.convention,
        req.correlation_policy,
        req.rho,
        req.cross_blocks,
        closed=False,
        T_total=T_total,
    )
    findings.extend(prop.findings)
    out = {
        **result,
        "frame_path": req.frame_path,
        "composed_transform": np.asarray(T_total).tolist(),
        "translation": T_total[:3, 3].tolist(),
        "rotation_matrix": T_total[:3, :3].tolist(),
        "summary": {"status": "propagated"},
    }
    if prop.Sigma is not None:
        out["covariance"] = prop.Sigma.tolist()
        out["sigma_diag_sqrt"] = np.sqrt(
            np.maximum(np.diag(prop.Sigma), 0)
        ).tolist()
    else:
        out["covariance"] = None
        out["summary"] = {
            "status": "indeterminate",
            "reason": "missing covariance on one or more edges",
            "missing_edges": prop.missing_edges,
        }
    return out
