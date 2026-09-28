"""Core data types shared by the ATMS kernel."""

from __future__ import annotations

from dataclasses import dataclass
from typing import FrozenSet, Tuple

# An environment is a set of assumption identifiers.  Frozensets give us
# hashability and subset semantics for free.
Environment = FrozenSet[str]

# Reserved consequent marking a contradiction.  Any environment that
# derives FALSE_NODE is a nogood.  It may never be used as an assumption
# or as a premise (enforced by the rule language).
FALSE_NODE = "FALSE"


@dataclass(frozen=True)
class Justification:
    """A Horn-style justification: the antecedents jointly support the consequent.

    An empty ``antecedents`` tuple models a premise/fact: it is supported by
    the empty environment.
    """

    rule_id: str
    antecedents: Tuple[str, ...]
    consequent: str
