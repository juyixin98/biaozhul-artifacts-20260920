"""Removal of pure silent ``(EPS, EPS)`` arcs.

Composition needs an unambiguous epsilon scheduling.  We obtain one by first
eliminating every **pure silent arc** (an arc whose input and output labels
are both epsilon): such arcs carry no tape symbol at all and are the only
source of unbounded epsilon-interleaving ambiguity (a silent chain of
length ``m`` on the left and ``n`` on the right admits ``C(m+n, m)``
identical schedules).  Arcs that are silent on only one tape are kept:

* ``(x, EPS)`` deletion arcs still consume a real input symbol;
* ``(EPS, z)`` insertion arcs still emit a real output symbol.

Shortcut weights use tropical (minimum) closure, computed with
Floyd-Warshall so negative silent-path weights are handled (pure epsilon
cycles and negative cycles are rejected earlier by
:mod:`wfst_service.core.cycles`).
"""

from __future__ import annotations

from ..corpus.symbols import EPS
from .fst import Arc, Fst

_INF = float("inf")


def _silent_closure(fst: Fst) -> list[list[float]]:
    n = fst.num_states
    closure = [[_INF] * n for _ in range(n)]
    for s in range(n):
        closure[s][s] = 0.0
    for arc in fst.arcs:
        if arc.ilabel == EPS and arc.olabel == EPS:
            if arc.cost < closure[arc.src][arc.dst]:
                closure[arc.src][arc.dst] = arc.cost
    for mid in range(n):
        cm = closure[mid]
        for src in range(n):
            sm = closure[src][mid]
            if sm == _INF:
                continue
            row = closure[src]
            for dst in range(n):
                cand = sm + cm[dst]
                if cand < row[dst]:
                    row[dst] = cand
    return closure


def remove_silent_epsilon(fst: Fst) -> Fst:
    """Return an equivalent FST without ``(EPS, EPS)`` arcs.

    A non-silent arc ``p -> q`` (weight ``w``) is reachable after any
    silent prefix ``s ~> p`` (weight ``c_sp``) and silent continuation
    ``q ~> r`` (weight ``c_qr``); it becomes ``s -> r`` with weight
    ``c_sp + w + c_qr``.  Final weights absorb trailing silent paths.
    Parallel arcs with identical labels/destination are merged by min.
    """
    closure = _silent_closure(fst)
    n = fst.num_states

    non_silent = tuple(
        a for a in fst.arcs if not (a.ilabel == EPS and a.olabel == EPS)
    )
    if len(non_silent) == len(fst.arcs):
        return fst  # nothing to remove

    # preds[p] = states that can reach p silently, with their best cost.
    preds: dict[int, list[tuple[int, float]]] = {s: [] for s in range(n)}
    # succ[q] = states silently reachable from q.
    succ: dict[int, list[tuple[int, float]]] = {s: [] for s in range(n)}
    for src in range(n):
        row = closure[src]
        for dst, cost in enumerate(row):
            if cost != _INF:
                preds[dst].append((src, cost))
                succ[src].append((dst, cost))

    merged: dict[tuple[int, int, str, str], float] = {}
    for arc in non_silent:
        for s, pre_cost in preds[arc.src]:
            for r, post_cost in succ[arc.dst]:
                weight = pre_cost + arc.cost + post_cost
                key = (s, r, arc.ilabel, arc.olabel)
                prev = merged.get(key)
                if prev is None or weight < prev:
                    merged[key] = weight

    arcs = tuple(
        Arc(s, r, ilab, olab, weight)
        for (s, r, ilab, olab), weight in merged.items()
    )

    finals: dict[int, float] = {}
    for s in range(n):
        best = _INF
        row = closure[s]
        for fstate, fweight in fst.finals.items():
            if row[fstate] != _INF:
                cand = row[fstate] + fweight
                if cand < best:
                    best = cand
        if best != _INF:
            finals[s] = best

    return Fst.create(f"{fst.name}#noeps", n, fst.start, finals, arcs)
