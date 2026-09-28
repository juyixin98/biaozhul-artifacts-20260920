"""Reasoning orchestration: compile -> saturate -> equivalence groups ->
independent oracle cross-check, producing fully explainable evidence.
"""

from __future__ import annotations

import time
from dataclasses import dataclass
from typing import Any

from ..kernel import engine
from ..kernel.engine import (
    BaseNode,
    ConflictPath,
    DerivedNode,
    ProofNode,
    SaturationReport,
)
from ..kernel.equivalence import build_equivalence_groups
from ..lang import ast
from ..lang.compiler import CompiledOntology, compile_ontology, render_expr
from ..oracle.enumerator import OracleVerdict, enumerate_ontology, normalize

__all__ = ["ReasoningService", "CrossCheck"]


@dataclass(frozen=True, slots=True)
class CrossCheck:
    agree: bool
    kernel_unsatisfiable: tuple[str, ...]
    oracle_unsatisfiable: tuple[str, ...]
    kernel_inconsistent: bool
    oracle_inconsistent: bool
    kernel_types: dict[str, tuple[str, ...]]
    oracle_types: dict[str, tuple[str, ...]]
    mismatches: tuple[str, ...]
    labels_explored: int


class ReasoningService:
    """Stateless core; state (evidence) lives in the store layer."""

    def compile(self, axiom_items: list[tuple[str, ast.Axiom]]) -> CompiledOntology:
        return compile_ontology(axiom_items)

    def reason(self, program: CompiledOntology) -> tuple[SaturationReport, Any, float]:
        start = time.perf_counter()
        report = engine.saturate(program)
        groups = build_equivalence_groups(program, report)
        duration_ms = (time.perf_counter() - start) * 1000.0
        return report, groups, duration_ms

    def oracle_check(
        self,
        axioms: list[ast.Axiom],
        report: SaturationReport,
        max_classes: int = 16,
    ) -> tuple[OracleVerdict, CrossCheck]:
        # The oracle rebuilds everything from the raw AST; it never sees rules.
        normalized = normalize(axioms)
        if len(normalized.classes) > max_classes:  # 2^n brute-force safety bound
            raise ValueError(
                f"oracle supports at most {max_classes} classes (brute-force enumeration);"
                f" ontology declares {len(normalized.classes)}"
            )
        verdict = enumerate_ontology(normalized)

        kernel_unsat = tuple(sorted(u.cls for u in report.unsatisfiable))
        oracle_unsat = tuple(sorted(c.cls for c in verdict.classes if not c.satisfiable))

        # Instance types: kernel gives exact derived types for a consistent
        # individual. Against an inconsistent individual the oracle returns
        # "all classes" (ex falso); compare only required/TBox-relevant types
        # via a mismatch listing rather than set equality.
        kernel_types: dict[str, tuple[str, ...]] = {}
        oracle_types: dict[str, tuple[str, ...]] = {}
        mismatches: list[str] = []
        if kernel_unsat != oracle_unsat:
            mismatches.append(
                f"unsatisfiable classes differ: kernel={list(kernel_unsat)} "
                f"oracle={list(oracle_unsat)}"
            )
        if report.inconsistent != (not verdict.ontology_consistent):
            mismatches.append(
                f"ontology consistency differs: kernel={report.inconsistent} "
                f"oracle={not verdict.ontology_consistent}"
            )

        for ind in verdict.individuals:
            kres = report.individual(ind.individual)
            ktypes = tuple(sorted(kres.types)) if kres else ()
            kernel_types[ind.individual] = ktypes
            if kres is not None and kres.conflict is None:
                # Consistent individual: every kernel type must be in every
                # oracle-valid label, and vice-versa.
                kset = set(ktypes)
                oset = set(ind.entailed_types)
                oracle_types[ind.individual] = tuple(sorted(o for o in oset))
                if kset != oset:
                    mismatches.append(
                        f"individual {ind.individual!r} entailed types differ: "
                        f"kernel={sorted(kset)} oracle={sorted(oset)}"
                    )
            else:
                oracle_types[ind.individual] = tuple(
                    sorted(ind.entailed_types)
                )

        return verdict, CrossCheck(
            agree=not mismatches,
            kernel_unsatisfiable=kernel_unsat,
            oracle_unsatisfiable=oracle_unsat,
            kernel_inconsistent=report.inconsistent,
            oracle_inconsistent=not verdict.ontology_consistent,
            kernel_types=kernel_types,
            oracle_types=oracle_types,
            mismatches=tuple(mismatches),
            labels_explored=verdict.labels_explored,
        )


# --------------------------------------------------------------------------- #
# Evidence serialization (proof trees -> plain dicts)
# --------------------------------------------------------------------------- #
def proof_to_dict(node: ProofNode) -> dict[str, Any]:
    if isinstance(node, BaseNode):
        return {
            "node": node.kind,
            "predicate": node.predicate,
            "source_axiom": node.source_axiom,
        }
    return {
        "node": "derived",
        "predicate": node.predicate,
        "rule_id": node.rule_id,
        "rule_kind": node.rule_kind,
        "source_axiom": node.source_axiom,
        "sources": list(node.sources()),
        "children": [proof_to_dict(c) for c in node.children],
    }


def conflict_to_dict(path: ConflictPath) -> dict[str, Any]:
    return {
        "code": "MUTEX_INSTANCE_CONFLICT",
        "disjoint_axiom": path.disjoint_axiom,
        "disjoint_pair": list(path.disjoint_pair),
        "firing_rule": path.rule_id,
        "sources": list(path.sources),
        # The conflict path: one proof chain per jointly-satisfied operand.
        "conflict_path": [proof_to_dict(p) for p in path.literal_proofs],
    }
