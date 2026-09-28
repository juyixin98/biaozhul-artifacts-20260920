"""Compile a validated :class:`RuleSet` onto an :class:`ATMS` instance."""

from __future__ import annotations

from typing import Optional

from ..core.atms import ATMS
from ..core.types import Justification
from .language import RuleSet

PREMISE_PREFIX = "__premise__:"


def premise_rule_id(node_id: str) -> str:
    return PREMISE_PREFIX + node_id


def compile_ruleset(rs: RuleSet, atms: Optional[ATMS] = None) -> ATMS:
    """Declare assumptions and install justifications; no propagation yet.

    Facts become zero-antecedent justifications, which the kernel labels
    with the empty environment.  Propagation is intentionally left to the
    caller so persistence and propagation can be scheduled separately.
    """
    atms = atms or ATMS()
    for a in rs.assumptions:
        atms.declare_assumption(a)
    for f in rs.facts:
        atms.add_justification(
            Justification(premise_rule_id(f), (), f)
        )
    for r in rs.rules:
        atms.add_justification(
            Justification(r.rule_id, tuple(r.antecedents), r.consequent)
        )
    return atms
