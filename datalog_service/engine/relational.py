"""In-memory relational matching used by the fixpoint engine and query layer.

A *relation* is a frozenset of tuples of hashable Python values (``int`` or
``str``).  Relations are immutable: the engine builds new frozensets when
tuples are derived rather than mutating existing ones.
"""

from __future__ import annotations

from typing import Dict, Optional, Sequence, Tuple

from ..language.ast import Constant, Term, Variable

Row = Tuple[object, ...]
Relation = frozenset[Row]
# Substitution: variable name -> constant value.
Subst = Dict[str, object]


def match_terms(terms: Sequence[Term], row: Row) -> Optional[Subst]:
    """Unify a list of (partly variable) terms with a concrete row.

    Returns the variable substitution on success, or ``None`` when a
    constant clashes.  Repeated variables within the same atom are
    unified: ``p(X, X)`` only matches rows whose two columns agree.
    """
    if len(terms) != len(row):
        return None
    subst: Subst = {}
    for term, value in zip(terms, row):
        if isinstance(term, Variable):
            existing = subst.get(term.name)
            if existing is not None and existing != value:
                return None
            subst[term.name] = value
        else:
            assert isinstance(term, Constant)
            if term.value != value:
                return None
    return subst
