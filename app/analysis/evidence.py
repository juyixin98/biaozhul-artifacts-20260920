"""Provenance chains: why a value is tainted.

Each variable in an abstract environment carries a level and a finite set of
*chains*.  Every chain starts at a source call and records the hops taint
flowed through.  Chains are also used to reconstruct the source-to-sink
evidence path reported by the service.
"""

from __future__ import annotations

from dataclasses import dataclass, field

from .taint import Taint

#: max chains kept per variable / sink argument; extra chains are folded into
#: a truncated marker so pathological programs cannot blow up the result
CHAIN_CAP = 8
#: max hops on a single chain.  Both caps bound the provenance universe, which
#: is what guarantees fixpoint termination in the presence of loops/recursion.
HOP_CAP = 24


@dataclass(frozen=True)
class Hop:
    """One taint-flow step on an evidence chain."""
    kind: str            # "source" | "assign" | "binop" | "call-arg" |
                         # "call-ret" | "param" | "param-merge" |
                         # "branch-merge" | "recursion-widen" | "unknown-fn"
    detail: str
    function: str
    line: int
    col: int = 0

    def to_dict(self) -> dict:
        d = {"kind": self.kind, "detail": self.detail,
             "function": self.function, "line": self.line}
        if self.col:
            d["col"] = self.col
        return d


@dataclass(frozen=True)
class Chain:
    hops: tuple[Hop, ...] = ()
    truncated: bool = False

    @property
    def source_name(self) -> str | None:
        for hop in self.hops:
            if hop.kind == "source":
                return hop.detail
        return None

    def extend(self, hop: Hop) -> "Chain":
        if len(self.hops) >= HOP_CAP:
            # Keep the chain bounded; the truncation flag notes that hops are
            # missing, so evidence paths stay finite and the fixpoint converges.
            return Chain(hops=self.hops, truncated=True)
        return Chain(hops=self.hops + (hop,), truncated=self.truncated)

    def mark_truncated(self) -> "Chain":
        return Chain(hops=self.hops, truncated=True)

    def to_dicts(self) -> list[dict]:
        return [h.to_dict() for h in self.hops]

    def render(self) -> str:
        parts = []
        for h in self.hops:
            parts.append(f"{h.function}@{h.line}: {h.kind}({h.detail})")
        return " -> ".join(parts)


#: empty provenance
CLEAN_CHAINS: frozenset[Chain] = frozenset()


@dataclass(frozen=True)
class Value:
    """Abstract value: taint level plus explanatory chains.

    ``BOTTOM`` means "no information / not defined on a path" and is
    join-neutral.  User-visible levels are CLEAN / MAYBE / TAINT.
    """
    level: Taint = Taint.BOTTOM
    chains: frozenset[Chain] = CLEAN_CHAINS

    @staticmethod
    def bottom() -> "Value":
        return Value(Taint.BOTTOM, CLEAN_CHAINS)

    @staticmethod
    def clean() -> "Value":
        return Value(Taint.CLEAN, CLEAN_CHAINS)

    @staticmethod
    def tainted(chains: frozenset[Chain]) -> "Value":
        chains = _cap(chains)
        return Value(Taint.TAINT, chains)

    @staticmethod
    def maybe(chains: frozenset[Chain]) -> "Value":
        chains = _cap(chains)
        return Value(Taint.MAYBE, chains)

    @staticmethod
    def join(a: "Value", b: "Value") -> "Value":
        """Control-flow merge: clean + tainted sibling paths => MAYBE."""
        level = Taint.join(a.level, b.level)
        if level is Taint.BOTTOM:
            return Value.bottom()
        if level is Taint.CLEAN:
            return Value.clean()
        # TAINT (both sides tainted) or MAYBE (mixed paths): union chains.
        chains = _cap(a.chains | b.chains)
        return Value(level, chains)

    @staticmethod
    def combine(a: "Value", b: "Value") -> "Value":
        """Value-dependency merge within ONE expression/path.

        * BOTTOM is the neutral element (a literal/constant carries no data
          dependency): ``combine(TAINT, BOTTOM) == TAINT`` and before a value
          is bound ``combine(CLEAN?, BOTTOM)`` stays unknown, never producing
          a spurious defined-CLEAN that would later join into MAYBE.
        * A defined-clean operand combined with a tainted one on the SAME path
          yields TAINT (there is no alternative clean value), not MAYBE.
        * MAYBE is sticky: a path-conditional operand keeps the result
          conditional.
        """
        if a.level is Taint.BOTTOM and b.level is Taint.BOTTOM:
            return Value.bottom()
        if a.level is Taint.BOTTOM:
            # BOTTOM + CLEAN literal stays unknown; BOTTOM + TAINT is taint.
            return b if b.level is Taint.TAINT else Value.bottom()
        if b.level is Taint.BOTTOM:
            return a if a.level is Taint.TAINT else Value.bottom()
        if a.level is Taint.CLEAN and b.level is Taint.CLEAN:
            return Value.clean()
        if a.level is Taint.MAYBE or b.level is Taint.MAYBE:
            return Value(Taint.MAYBE, _cap(a.chains | b.chains))
        # one side TAINT, the other a defined CLEAN value: single-path taint
        return Value(Taint.TAINT, _cap(a.chains | b.chains))

    def add_hop(self, hop: Hop) -> "Value":
        if self.level in (Taint.BOTTOM, Taint.CLEAN):
            return self
        chains = frozenset(c.extend(hop) for c in self.chains) or CLEAN_CHAINS
        return Value(self.level, _cap(chains))


def _cap(chains: frozenset[Chain]) -> frozenset[Chain]:
    if len(chains) <= CHAIN_CAP:
        return chains
    kept = list(chains)[: CHAIN_CAP - 1]
    extra = next(iter(chains - frozenset(kept)))
    kept.append(extra.mark_truncated())
    return frozenset(kept)


def join_envs(
    a: dict[str, Value], b: dict[str, Value]
) -> dict[str, Value]:
    """Join two variable environments (lattice LUB).

    A variable missing from one side is BOTTOM there ("not defined on that
    path", join-neutral), not CLEAN.  Therefore:

    * x=TAINT on one incoming edge, undefined on the other -> TAINT
    * x=TAINT on one arm, x=CLEAN on the sibling arm       -> MAYBE
    """
    out: dict[str, Value] = {}
    bottom = Value.bottom()
    for name in a.keys() | b.keys():
        out[name] = Value.join(a.get(name, bottom), b.get(name, bottom))
    return out
