"""Epsilon-filtered composition of two WFSTs.

Scheduling (after pure ``(EPS, EPS)`` arcs have been removed from both
operands by :mod:`wfst_service.core.eps_removal`):

* **synchronise** -- a real middle-tape symbol ``y``: A arc with
  ``olabel == y`` paired with B arc with ``ilabel == y``;
* **A alone** -- an A arc whose ``olabel`` is epsilon (B does nothing);
* **B alone** -- a B arc whose ``ilabel`` is epsilon (A does nothing).

Input epsilon and output epsilon never synchronise with each other.

The classic duplicate-scheduling problem -- when both an A-alone and a
B-alone move are enabled, "A then B" and "B then A" reach the same pair
of states with the same tape consumption/emission and the same weight --
is removed by a three-state epsilon filter enforcing one canonical
interleaving, ``B* A*`` (all B-side epsilon moves before A-side ones)
between synchronisations.  Filter state:

* ``0`` FREE: either side may move (or a real label synchronise);
* ``2`` B-OPEN: B-alone moves may continue; an A-alone move is allowed
  exactly once as the hand-off into ``1``;
* ``1`` A-OPEN: only A-alone moves (and synchronisations) are allowed.

A synchronisation resets the phase to ``0``.  The canonical order is
sound because A-alone moves carry only input labels (their output label
is epsilon) and B-alone moves carry only output labels (their input label
is epsilon): every interleaving shares the same input string, output
string, weight and endpoint pair, so suppressing the non-canonical
interleavings removes only identical copies, never distinct alignments
or reachable endpoints.
"""

from __future__ import annotations

from dataclasses import dataclass, field

from ..corpus.symbols import EPS
from .eps_removal import remove_silent_epsilon
from .errors import TopologyError
from .fst import Arc, Fst

# Filter phases.
PHASE_FREE = 0
PHASE_A = 1
PHASE_B = 2


@dataclass(frozen=True, slots=True)
class CompositionTrace:
    """Auditable summary of one composition run."""

    name: str
    left: str
    right: str
    states: int
    arcs: int
    synchronised_moves: int
    a_alone_moves: int
    b_alone_moves: int
    filter_blocked: int
    unreachable_pairs_pruned: int
    dangling_middle_labels: tuple[str, ...] = field(default_factory=tuple)

    def as_lines(self) -> list[str]:
        return [
            f"compose {self.left!r} o {self.right!r} -> {self.name!r}",
            f"  states={self.states} arcs={self.arcs}",
            f"  moves: sync={self.synchronised_moves} "
            f"a-alone={self.a_alone_moves} b-alone={self.b_alone_moves}",
            f"  filter blocked duplicate schedules: {self.filter_blocked}",
            f"  unreachable pairs pruned: {self.unreachable_pairs_pruned}",
            (
                "  dangling middle labels (never consumed by right): "
                f"{self.render_labels()}"
            ),
        ]

    def render_labels(self) -> str:
        if not self.dangling_middle_labels:
            return "-"
        return ",".join(repr(x) for x in self.dangling_middle_labels)


def compose(
    left: Fst,
    right: Fst,
    name: str,
    *,
    max_states: int = 200_000,
) -> tuple[Fst, CompositionTrace]:
    """Compose ``left o right`` (input first traverses ``left``).

    Both operands are normalised (pure silent arcs removed) before the
    on-the-fly construction.  Raises :class:`TopologyError` if the state
    budget is exceeded.
    """
    a = remove_silent_epsilon(left)
    b = remove_silent_epsilon(right)

    # Index B arcs by (source state, real input label) for synchronisation.
    # The source state is mandatory: arcs matching the label elsewhere in
    # B must not fire from the current pair's B state.
    b_by_input: dict[tuple[int, str], list[Arc]] = {}
    for arc in b.arcs:
        if arc.ilabel != EPS:
            b_by_input.setdefault((arc.src, arc.ilabel), []).append(arc)

    # Labels A can emit but B never consumes (kept in the trace only;
    # they simply produce no accepting path through the composition).
    b_input_labels = {arc.ilabel for arc in b.arcs if arc.ilabel != EPS}
    a_outputs = {arc.olabel for arc in a.arcs if arc.olabel != EPS}
    dangling = tuple(sorted(a_outputs - b_input_labels))

    start_pair = (a.start, b.start, PHASE_FREE)
    discovered: dict[tuple[int, int, int], int] = {start_pair: 0}
    queue: list[tuple[int, int, int]] = [start_pair]

    merged: dict[tuple[int, int, str, str], float] = {}
    finals: dict[int, float] = {}
    n_sync = n_a_only = n_b_only = n_blocked = 0

    def pair_id(pair: tuple[int, int, int]) -> int:
        existing = discovered.get(pair)
        if existing is not None:
            return existing
        new_id = len(discovered)
        if new_id > max_states:
            raise TopologyError(
                f"composition {name!r}: state budget {max_states} exceeded "
                f"(epsilon/lattice blow-up)"
            )
        discovered[pair] = new_id
        queue.append(pair)
        return new_id

    head = 0
    while head < len(queue):
        sa, sb, phase = queue[head]
        head += 1
        src_id = discovered[(sa, sb, phase)]

        if a.is_final(sa) and b.is_final(sb):
            w = a.final_cost(sa) + b.final_cost(sb)
            prev = finals.get(src_id)
            finals[src_id] = w if prev is None else min(prev, w)

        for arc_a in a.outgoing(sa):
            # 1) Synchronise on a real middle symbol (allowed in every
            #    phase; resets phase to FREE).
            if arc_a.olabel != EPS:
                for arc_b in b_by_input.get((sb, arc_a.olabel), ()):
                    dst = pair_id((arc_a.dst, arc_b.dst, PHASE_FREE))
                    key = (src_id, dst, arc_a.ilabel, arc_b.olabel)
                    w = arc_a.cost + arc_b.cost
                    prev = merged.get(key)
                    if prev is None or w < prev:
                        merged[key] = w
                    n_sync += 1
            else:
                # 2) A-alone move.  Always permitted: FREE -> A-OPEN,
                #    A-OPEN continues in A-OPEN, and B-OPEN may hand off
                #    into A-OPEN once (the canonical B* A* boundary).
                dst = pair_id((arc_a.dst, sb, PHASE_A))
                key = (src_id, dst, arc_a.ilabel, EPS)
                prev = merged.get(key)
                if prev is None or arc_a.cost < prev:
                    merged[key] = arc_a.cost
                n_a_only += 1

        # 3) B-alone move (its input tape is epsilon).  Allowed while the
        #    B-run is still open (FREE opens one, B-OPEN continues); once
        #    A-OPEN has been entered the B-run is closed for good.
        if phase != PHASE_A:
            for arc_b in b.outgoing(sb):
                if arc_b.ilabel != EPS:
                    continue
                dst = pair_id((sa, arc_b.dst, PHASE_B))
                key = (src_id, dst, EPS, arc_b.olabel)
                prev = merged.get(key)
                if prev is None or arc_b.cost < prev:
                    merged[key] = arc_b.cost
                n_b_only += 1
        else:
            n_blocked += sum(
                1 for arc_b in b.outgoing(sb) if arc_b.ilabel == EPS
            )

    # Every discovered id is reachable from the start by construction;
    # pairs that cannot reach any co-final pair are pruned now (they carry
    # no accepting derivation, so removing them keeps the stored model
    # minimal and the counts honest).
    live, _arcs_live = _prune_to_cofinal(discovered, merged, finals)

    if not live or 0 not in live:
        # Empty language (no co-final pair reachable): return a one-state
        # machine with no finals.  This keeps downstream code total
        # instead of crashing on an unreachable start.
        empty = Fst.create(name, 1, 0, {}, ())
        trace = CompositionTrace(
            name=name,
            left=left.name,
            right=right.name,
            states=1,
            arcs=0,
            synchronised_moves=n_sync,
            a_alone_moves=n_a_only,
            b_alone_moves=n_b_only,
            filter_blocked=n_blocked,
            unreachable_pairs_pruned=len(discovered),
            dangling_middle_labels=dangling,
        )
        return empty, trace

    old_to_new = {old: idx for idx, old in enumerate(sorted(live))}
    out_arcs = tuple(
        Arc(
            old_to_new[s],
            old_to_new[d],
            ilab,
            olab,
            weight,
        )
        for (s, d, ilab, olab), weight in sorted(merged.items())
        if s in live and d in live
    )
    out_finals = {old_to_new[s]: w for s, w in finals.items() if s in live}
    fst = Fst.create(name, len(live), old_to_new[0], out_finals, out_arcs)

    pruned = len(discovered) - len(live)
    trace = CompositionTrace(
        name=name,
        left=left.name,
        right=right.name,
        states=len(live),
        arcs=len(out_arcs),
        synchronised_moves=n_sync,
        a_alone_moves=n_a_only,
        b_alone_moves=n_b_only,
        filter_blocked=n_blocked,
        unreachable_pairs_pruned=pruned,
        dangling_middle_labels=dangling,
    )
    return fst, trace


def _prune_to_cofinal(
    discovered: dict[tuple[int, int, int], int],
    merged: dict[tuple[int, int, str, str], float],
    finals: dict[int, float],
) -> tuple[set[int], int]:
    """Keep exactly states from which some co-final pair is reachable."""
    adj: dict[int, set[int]] = {sid: set() for sid in discovered.values()}
    for s, d, _i, _o in merged:
        adj[s].add(d)
    can_final: set[int] = set(finals)
    rev: dict[int, list[int]] = {sid: [] for sid in discovered.values()}
    for s, ds in adj.items():
        for d in ds:
            rev[d].append(s)
    stack = list(can_final)
    while stack:
        u = stack.pop()
        for p in rev[u]:
            if p not in can_final:
                can_final.add(p)
                stack.append(p)
    live_arcs = sum(1 for s, d, *_ in merged if s in can_final and d in can_final)
    return can_final, live_arcs
