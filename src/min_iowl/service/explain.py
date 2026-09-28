"""Shaping of reasoning output into the reviewable evidence envelope.

Kept in the service layer (not the API layer) so that command-line scripts can
produce the exact same evidence document without importing FastAPI.
"""

from __future__ import annotations

from typing import Any

from ..config import ENGINE_VERSION
from ..lang.errors import ErrorCode
from .reasoning import (
    CrossCheck,
    conflict_to_dict,
    proof_to_dict,
)


def shape_result(
    *,
    ontology_id: str,
    program,
    report,
    groups,
    cross_check: CrossCheck | None,
    duration_ms: float,
    oracle_skipped: str | None,
) -> dict[str, Any]:
    steps = [
        {"step": "compile", "status": "ok", "detail": f"{len(program.rules)} rules, {len(program.facts)} base facts"},
        {"step": "saturate", "status": "ok", "detail": f"{len(report.individuals)} individuals saturated"},
        {"step": "equivalence_partition", "status": "ok", "detail": f"{sum(1 for g in groups if len(g.members) > 1)} non-trivial group(s)"},
        {"step": "oracle_cross_check", "status": "skipped" if oracle_skipped else "ok",
         "detail": oracle_skipped or ("independent finite-model enumeration agreed" if cross_check and cross_check.agree else "MISMATCH")},
    ]

    hierarchy = {cls: sorted(supers) for cls, supers in sorted(report.tbox_supers.items())}
    equivalence = [
        {
            "canonical": g.canonical,
            "members": list(g.members),
            "supporting_axioms": list(g.supporting_axioms),
            "direct_equivalence_axioms": list(g.direct_equivalence_axioms),
        }
        for g in groups if len(g.members) > 1
    ]
    individuals = [
        {
            "individual": r.individual,
            "types": sorted(r.types),
            "proofs": {p: proof_to_dict(node) for p, node in sorted(r.proofs.items())},
        }
        for r in report.individuals
    ]

    failures: list[dict[str, Any]] = []
    for u in report.unsatisfiable:
        failures.append({
            "code": ErrorCode.UNSATISFIABLE_CLASS,
            "severity": "CLASS_LEVEL",
            "class": u.cls,
            "message": f"class {u.cls!r} is unsatisfiable (no instance can exist)",
            "conflict": conflict_to_dict(u.conflict),
        })
    for r in report.individuals:
        if r.conflict is not None:
            failures.append({
                "code": ErrorCode.MUTEX_INSTANCE_CONFLICT,
                "severity": "INDIVIDUAL_LEVEL",
                "individual": r.individual,
                "message": f"individual {r.individual!r} belongs to mutually disjoint classes",
                "conflict": conflict_to_dict(r.conflict),
            })
    if report.inconsistent:
        failures.append({
            "code": ErrorCode.ONTOLOGY_INCONSISTENT,
            "severity": "ONTOLOGY_LEVEL",
            "message": "the ontology ABox has no model: a declared individual violates disjointness",
        })

    uncertain: list[dict[str, Any]] = []
    if oracle_skipped:
        uncertain.append({"code": "ORACLE_SKIPPED", "message": oracle_skipped})
    if cross_check is not None and not cross_check.agree:
        uncertain.append({
            "code": "CROSS_CHECK_MISMATCH",
            "message": "kernel and independent enumerator disagree; do not trust result blindly",
            "mismatches": list(cross_check.mismatches),
        })
    if report.inconsistent:
        uncertain.append({
            "code": "EX_FALSO_QUALIFICATION",
            "message": "under inconsistency classical logic entails everything; only rule-derived types are listed, conflict evidence is authoritative",
        })

    cross_section = None
    if cross_check is not None:
        cross_section = {
            "agrees": cross_check.agree,
            "labels_explored": cross_check.labels_explored,
            "kernel": {
                "unsatisfiable_classes": list(cross_check.kernel_unsatisfiable),
                "ontology_inconsistent": cross_check.kernel_inconsistent,
                "individual_types": {k: list(v) for k, v in cross_check.kernel_types.items()},
            },
            "oracle": {
                "unsatisfiable_classes": list(cross_check.oracle_unsatisfiable),
                "ontology_inconsistent": cross_check.oracle_inconsistent,
                "individual_entailed_types": {k: list(v) for k, v in cross_check.oracle_types.items()},
            },
            "mismatches": list(cross_check.mismatches),
        }

    return {
        "ontology_id": ontology_id,
        "engine_version": ENGINE_VERSION,
        "reasoning_ms": round(duration_ms, 3),
        "state": {
            "ontology_inconsistent": report.inconsistent,
            "unsatisfiable_classes": [u.cls for u in report.unsatisfiable],
            "note": "class unsatisfiability is independent of ontology inconsistency",
        },
        "steps": steps,
        "results": {
            "class_hierarchy": hierarchy,
            "equivalence_classes": equivalence,
            "individuals": individuals,
        },
        "failures": failures,
        "uncertain": uncertain,
        "cross_check": cross_section,
    }
