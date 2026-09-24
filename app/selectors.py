"""Label selector matching (Kubernetes semantics for the supported subset).

Supported operators: In, NotIn, Exists, DoesNotExist.
Within one selector / term all requirements are AND-ed; node selector terms
passed to node affinity are OR-ed by the caller.
"""

from __future__ import annotations

from .models import LabelSelector, LabelSelectorRequirement, NodeSelectorTerm


def requirement_matches(req: LabelSelectorRequirement, labels: dict[str, str]) -> bool:
    present = req.key in labels
    if req.operator == "In":
        return present and labels[req.key] in req.values
    if req.operator == "NotIn":
        # Kubernetes semantics: a missing key satisfies NotIn.
        return (not present) or labels[req.key] not in req.values
    if req.operator == "Exists":
        return present
    if req.operator == "DoesNotExist":
        return not present
    raise ValueError(f"unsupported selector operator {req.operator!r}")  # pragma: no cover


def selector_matches(selector: LabelSelector | None, labels: dict[str, str]) -> bool:
    if selector is None:
        return False
    if selector.match_labels:
        for key, value in selector.match_labels.items():
            if labels.get(key) != value:
                return False
    for req in selector.match_expressions:
        if not requirement_matches(req, labels):
            return False
    return True


def node_selector_term_matches(term: NodeSelectorTerm, labels: dict[str, str]) -> bool:
    # An empty term matches nothing in this subset (caller validates non-empty).
    if not term.match_expressions:
        return False
    return all(requirement_matches(req, labels) for req in term.match_expressions)
