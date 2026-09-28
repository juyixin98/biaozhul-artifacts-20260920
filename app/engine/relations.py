"""In-memory relations and nested-loop rule evaluation.

The engine is deliberately independent of SQLite: it evaluates over plain
sets of ground tuples and returns *firings* (head tuple + the exact body
tuples that satisfied the rule).  The interpreter layer persists those
firings as derivations; the store never participates in recursion itself.
"""

from __future__ import annotations

from dataclasses import dataclass, field

from ..language.compiler import WILDCARD_PREFIX
from ..language.terms import Atom, Comparison, Const, NegAtom, Rule, Term, Var

PredKey = tuple[str, int]
Tuple = tuple[str, ...]


@dataclass
class Database:
    """Accumulated relations during evaluation."""

    rels: dict[PredKey, set[Tuple]] = field(default_factory=dict)
    # Delta relations hold only tuples produced by the previous iteration;
    # they are non-empty for stratum IDB predicates during semi-naive eval.
    deltas: dict[PredKey, set[Tuple]] = field(default_factory=dict)

    def get(self, key: PredKey, delta: bool = False) -> set[Tuple]:
        if delta:
            return self.deltas.get(key, set())
        return self.rels.get(key, set())

    def ensure(self, key: PredKey) -> set[Tuple]:
        return self.rels.setdefault(key, set())


@dataclass(frozen=True)
class Firing:
    """One successful instantiation of a rule."""

    rule_index: int
    head: Tuple
    pos_inputs: tuple[tuple[PredKey, Tuple], ...]
    # Each element is (predicate, match-pattern); '*' marks an existential
    # position (anonymous variable).  Absence of ANY matching tuple is what
    # the negation asserts.
    neg_checks: tuple[tuple[PredKey, tuple[str, ...]], ...]


def _any_matching(relation: set[Tuple], pattern: Tuple) -> bool:
    """True if any tuple agrees on every non-wildcard (``*``) position."""

    for tup in relation:
        if len(tup) != len(pattern):
            continue
        if all(p == "*" or p == v for p, v in zip(pattern, tup)):
            return True
    return False


def _try_bind(args: tuple[Term, ...], tup: Tuple, bindings: dict[str, str]) -> dict[str, str] | None:
    """Extend ``bindings`` so ``args`` match ground tuple ``tup``.

    Returns ``None`` on a constant clash or an inconsistent variable.
    """

    out = dict(bindings)
    for term, value in zip(args, tup):
        if isinstance(term, Const):
            if term.value != value:
                return None
        elif isinstance(term, Var):
            existing = out.get(term.name)
            if existing is not None and existing != value:
                return None
            out[term.name] = value
        else:  # pragma: no cover - defensive
            raise TypeError(f"unsupported term {term!r}")
    return out


def _term_value(term: Term, bindings: dict[str, str]) -> str | None:
    if isinstance(term, Const):
        return term.value
    if isinstance(term, Var):
        return bindings.get(term.name)
    return None  # pragma: no cover


def _as_number(value: str) -> float | None:
    try:
        return float(value)
    except ValueError:
        return None


def _comparison_holds(cmp: Comparison, bindings: dict[str, str]) -> bool:
    lv = _term_value(cmp.left, bindings)
    rv = _term_value(cmp.right, bindings)
    if lv is None or rv is None:
        # Safety guarantees both are bound by positive literals; a missing
        # binding here means the literal cannot be satisfied.
        return False
    if cmp.op == "=":
        return lv == rv
    if cmp.op == "!=":
        return lv != rv
    ln, rn = _as_number(lv), _as_number(rv)
    if ln is not None and rn is not None:
        a, b = ln, rn
    else:
        a, b = lv, rv
    if cmp.op == "<":
        return a < b
    if cmp.op == "<=":
        return a <= b
    if cmp.op == ">":
        return a > b
    if cmp.op == ">=":
        return a >= b
    return False  # pragma: no cover


def fire_rule(
    rule: Rule,
    rule_index: int,
    db: Database,
    designated_index: int | None = None,
) -> list[Firing]:
    """Evaluate one rule.

    ``designated_index`` is the body index of the positive literal scanned
    against the *delta* relation (semi-naive variant).  ``None`` scans every
    positive literal against the full relation (naive / bootstrap).
    """

    positive: list[tuple[int, Atom]] = []
    negative: list[NegAtom] = []
    comparisons: list[Comparison] = []
    for i, lit in enumerate(rule.body):
        if isinstance(lit, Atom):
            positive.append((i, lit))
        elif isinstance(lit, NegAtom):
            negative.append(lit)
        else:
            comparisons.append(lit)

    firings: list[Firing] = []

    def head_tuple(bindings: dict[str, str]) -> Tuple:
        return tuple(bindings[a.name] if isinstance(a, Var) else a.value for a in rule.head.args)

    def finish(bindings: dict[str, str], pos_inputs: list[tuple[PredKey, Tuple]]) -> None:
        for cmp in comparisons:
            if not _comparison_holds(cmp, bindings):
                return
        neg_checks: list[tuple[PredKey, Tuple]] = []
        for neg in negative:
            key = (neg.atom.pred, neg.atom.arity)
            pattern = tuple(
                "*"
                if isinstance(a, Var) and a.name.startswith(WILDCARD_PREFIX)
                else (_term_value(a, bindings) or "")
                for a in neg.atom.args
            )
            if _any_matching(db.get(key), pattern):
                return
            neg_checks.append((key, pattern))
        firings.append(
            Firing(
                rule_index=rule_index,
                head=head_tuple(bindings),
                pos_inputs=tuple(pos_inputs),
                neg_checks=tuple(neg_checks),
            )
        )

    def join(j: int, bindings: dict[str, str], pos_inputs: list[tuple[PredKey, Tuple]]) -> None:
        if j == len(positive):
            finish(bindings, pos_inputs)
            return
        body_index, atom = positive[j]
        key = (atom.pred, atom.arity)
        relation = db.get(key, delta=(designated_index == body_index))
        for tup in relation:
            extended = _try_bind(atom.args, tup, bindings)
            if extended is None:
                continue
            pos_inputs.append((key, tup))
            join(j + 1, extended, pos_inputs)
            pos_inputs.pop()

    join(0, {}, [])
    return firings
