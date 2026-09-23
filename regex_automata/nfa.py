"""Thompson NFA construction from the parsed AST.

The NFA has three edge kinds, all stored as plain tuples (JSON friendly):

* ``("char", cp)``               consume one code point equal to ``cp``
* ``("pred", predicate_descr)`` consume one code point accepted by a
                                :class:`~regex_automata.predicates.CharPredicate`
* ``("eps",)``                   epsilon transition (consumes nothing)

Zero-width anchors (``^ $ \\b \\B``) are not edges at all: they are
recorded in ``assertions[state]`` and evaluated by the matching
simulation when the state is entered.  Keeping assertions separate from
epsilon edges makes the epsilon-closure computation a plain fixpoint
with no special "pass-once" rule.

Nothing in this module imports Python's ``re``.
"""
from __future__ import annotations

from dataclasses import dataclass, field

from . import ast
from .predicates import CharPredicate


@dataclass
class Edge:
    kind: str                 # "char" | "pred" | "eps"
    target: int
    cp: int | None = None
    predicate: CharPredicate | None = None


@dataclass
class Fragment:
    start: int
    accept: int


@dataclass
class NFA:
    edges: list[list[Edge]] = field(default_factory=lambda: [[]])
    start: int = 0
    accept: int = 0

    def new_state(self) -> int:
        self.edges.append([])
        return len(self.edges) - 1

    def add(self, src: int, edge: Edge) -> None:
        self.edges[src].append(edge)

    # serialisation ------------------------------------------------------------
    def to_dict(self) -> dict:
        states = []
        for out in self.edges:
            edges = []
            for e in out:
                if e.kind == "char":
                    edges.append({"type": "char", "cp": e.cp, "target": e.target})
                elif e.kind == "pred":
                    edges.append(
                        {
                            "type": "pred",
                            "matches": e.predicate.describe(),  # type: ignore[union-attr]
                            "target": e.target,
                        }
                    )
                else:
                    edges.append({"type": "eps", "target": e.target})
            states.append({"id": len(states), "edges": edges})
        return {
            "states": states,
            "start": self.start,
            "accept": self.accept,
            "state_count": len(self.edges),
        }


class Builder:
    def __init__(self, tree: ast.Node) -> None:
        self.nfa = NFA()
        # Anchor assertions attached to a state: list of ("^"/"$"/"b"/"B").
        self.assertions: dict[int, list[str]] = {}
        self.tree = tree

    # primitives ---------------------------------------------------------------
    def eps_frag(self) -> Fragment:
        s, a = self.nfa.new_state(), self.nfa.new_state()
        self.nfa.add(s, Edge("eps", a))
        return Fragment(s, a)

    def char_frag(self, cp: int) -> Fragment:
        s, a = self.nfa.new_state(), self.nfa.new_state()
        self.nfa.add(s, Edge("char", a, cp=cp))
        return Fragment(s, a)

    def pred_frag(self, predicate: CharPredicate) -> Fragment:
        s, a = self.nfa.new_state(), self.nfa.new_state()
        self.nfa.add(s, Edge("pred", a, predicate=predicate))
        return Fragment(s, a)

    def anchor_frag(self, kind: str) -> Fragment:
        s, a = self.nfa.new_state(), self.nfa.new_state()
        self.assertions.setdefault(s, []).append(kind)
        # No edge at all: the matcher enters s, checks the assertion, and can
        # continue only via an epsilon we add from s to a on success.
        self.nfa.add(s, Edge("eps", a))
        return Fragment(s, a)

    def concat_frags(self, frags: list[Fragment]) -> Fragment:
        if not frags:
            return self.eps_frag()
        for left, right in zip(frags, frags[1:]):
            # Merge: right.start disappears, redirect left.accept epsilon chain
            # by linking the two states directly.
            self.nfa.add(left.accept, Edge("eps", right.start))
        return Fragment(frags[0].start, frags[-1].accept)

    def alt_frags(self, frags: list[Fragment]) -> Fragment:
        start = self.nfa.new_state()
        end = self.nfa.new_state()
        for f in frags:
            self.nfa.add(start, Edge("eps", f.start))
            self.nfa.add(f.accept, Edge("eps", end))
        return Fragment(start, end)

    def repeat_frag(self, child: ast.Node, lo: int, hi: int | None) -> Fragment:
        """Thompson construction for ``child{lo,hi}``.

        Every concrete copy of ``child`` is rebuilt from the AST so it gets
        fresh states (Thompson graphs cannot be structurally shared).
        """
        start = self.nfa.new_state()
        end = self.nfa.new_state()
        cur = start

        mandatory = lo
        optional = None if hi is None else hi - lo

        for _ in range(mandatory):
            f = self.build(child)
            self.nfa.add(cur, Edge("eps", f.start))
            cur = f.accept

        if optional is not None:
            for _ in range(optional):
                f = self.build(child)
                # Bypass: entering the copy may either run it or skip it.
                self.nfa.add(cur, Edge("eps", f.start))
                self.nfa.add(cur, Edge("eps", f.accept))
                cur = f.accept
            self.nfa.add(cur, Edge("eps", end))
        else:
            # Unbounded tail: a loop state with two epsilon choices.
            loop = self.nfa.new_state()
            f = self.build(child)
            self.nfa.add(cur, Edge("eps", loop))
            self.nfa.add(loop, Edge("eps", f.start))
            self.nfa.add(f.accept, Edge("eps", loop))
            self.nfa.add(loop, Edge("eps", end))

        return Fragment(start, end)

    def build(self, node: ast.Node) -> Fragment:
        if isinstance(node, ast.Empty):
            return self.eps_frag()
        if isinstance(node, ast.Char):
            return self.char_frag(node.cp)
        if isinstance(node, ast.Dot):
            return self.pred_frag(CharPredicate.any_char())
        if isinstance(node, ast.Class):
            return self.pred_frag(node.predicate)
        if isinstance(node, ast.Anchor):
            return self.anchor_frag(node.kind)
        if isinstance(node, ast.Group):
            return self.build(node.child)
        if isinstance(node, ast.Concat):
            return self.concat_frags([self.build(p) for p in node.parts])
        if isinstance(node, ast.Alt):
            return self.alt_frags([self.build(b) for b in node.branches])
        if isinstance(node, ast.Repeat):
            return self.repeat_frag(node.child, node.minimum, node.maximum)
        raise TypeError(f"unknown AST node: {node!r}")


def build_nfa(tree: ast.Node) -> tuple[NFA, dict[int, list[str]]]:
    builder = Builder(tree)
    frag = builder.build(tree)
    nfa = builder.nfa
    nfa.start = frag.start
    nfa.accept = frag.accept
    return nfa, builder.assertions
