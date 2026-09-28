"""Convert kernel result dataclasses into JSON-ready dictionaries."""
from __future__ import annotations

from dataclasses import asdict
from typing import Any

from ..kernel.reasoner import ReasoningResult


def _conflict(conflict) -> dict[str, Any] | None:
    if conflict is None:
        return None
    return {
        "subject": conflict.subject,
        "category": conflict.category,
        "disjoint_classes": list(conflict.disjoint_classes),
        "disjoint_axiom_source": conflict.disjoint_source,
        "left_chain": [asdict(s) for s in conflict.left_chain],
        "right_chain": [asdict(s) for s in conflict.right_chain],
    }


def result_to_dict(result: ReasoningResult) -> dict[str, Any]:
    return {
        "classes": list(result.classes),
        "equivalences": [
            {
                "canonical": e.canonical,
                "members": list(e.members),
                "declaration_sources": list(e.declaration_sources),
                "merge_chain": [asdict(m) for m in e.merge_chain],
            }
            for e in result.equivalences
        ],
        "unsatisfiable": [
            {
                "class": u.cls,
                "equivalence_members": list(u.members),
                "category": u.category,
                "conflict_path": _conflict(u.conflict),
            }
            for u in result.unsatisfiable
        ],
        "instances": [
            {
                "instance": i.instance,
                "asserted_types": list(i.asserted_types),
                "entailed_types": list(i.entailed_types),
                "status": i.status,
                "conflict_path": _conflict(i.conflict),
            }
            for i in result.instances
        ],
    }
