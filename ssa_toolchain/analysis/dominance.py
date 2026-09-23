"""Dominance analysis over the IR CFG.

Implements the standard algorithms from scratch:

* reachability from the entry block (used to discard structurally-present
  but unreachable blocks before any SSA work);
* immediate dominators with the iterative Cooper/Harvey/Kennedy fixed-point
  algorithm over a reverse-postorder numbering;
* dominator sets, dominator-tree children and post-dominator independent
  *dominance frontiers* via the Cytron rule
  ``DF(n) = (succ(n) - idom-children(n))  plus  DF(c) for children c``.
"""
from __future__ import annotations

from dataclasses import dataclass


@dataclass
class DomInfo:
    entry: str
    labels: list[str]                 # reachable blocks in RPO
    removed: list[str]                # blocks unreachable from entry
    rpo_index: dict[str, int]
    idom: dict[str, str | None]
    dom: dict[str, set[str]]
    children: dict[str, list[str]]
    df: dict[str, set[str]]
    preds: dict[str, list[str]]
    succs: dict[str, list[str]]

    def dominates(self, a: str, b: str) -> bool:
        return a in self.dom.get(b, set())


def _reachable(entry, succs) -> set[str]:
    seen = {entry}
    stack = [entry]
    while stack:
        x = stack.pop()
        for y in succs.get(x, ()):
            if y not in seen:
                seen.add(y)
                stack.append(y)
    return seen


def _dfs_rpo(entry, succs) -> list[str]:
    """Iterative DFS producing reverse postorder over reachable nodes."""
    WHITE, GRAY, BLACK = 0, 1, 2
    color = {n: WHITE for n in succs}
    order: list[str] = []
    # stack frames: (node, iterator index)
    stack = [(entry, 0)]
    color[entry] = GRAY
    while stack:
        node, idx = stack[-1]
        if idx < len(succs.get(node, ())):
            child = succs[node][idx]
            stack[-1] = (node, idx + 1)
            if color.get(child, WHITE) == WHITE:
                color[child] = GRAY
                stack.append((child, 0))
        else:
            color[node] = BLACK
            order.append(node)
            stack.pop()
    order.reverse()
    return order


def _intersect(b1, b2, rpo_index, idom) -> str:
    finger1, finger2 = b1, b2
    while finger1 != finger2:
        while rpo_index[finger1] > rpo_index[finger2]:
            finger1 = idom[finger1]
        while rpo_index[finger2] > rpo_index[finger1]:
            finger2 = idom[finger2]
    return finger1


def compute_dominance(func, prune_unreachable: bool = True) -> DomInfo:
    raw_preds = func.preds()
    raw_succs: dict[str, list[str]] = {}
    for l in func.ordered_labels:
        term = func.blocks[l].term
        raw_succs[l] = [s for s in (term.successors() if term else [])
                        if s in raw_preds]

    reachable = _reachable(func.entry, raw_succs)
    removed = [l for l in func.ordered_labels if l not in reachable]

    if prune_unreachable:
        labels = [l for l in func.ordered_labels if l in reachable]
    else:
        labels = list(func.ordered_labels)
        reachable = set(labels)

    preds = {l: [p for p in raw_preds[l] if p in reachable] for l in labels}
    succs = {l: [s for s in raw_succs[l] if s in reachable] for l in labels}

    rpo = _dfs_rpo(func.entry, succs) if labels else []
    rpo_index = {l: i for i, l in enumerate(rpo)}

    # --- immediate dominators (Cooper, Harvey, Kennedy) -------------------
    idom: dict[str, str | None] = {func.entry: None}
    for l in labels:
        if l != func.entry:
            idom[l] = None
    changed = True
    while changed:
        changed = False
        for b in rpo[1:]:
            processed = [p for p in preds[b] if idom.get(p) is not None or p == func.entry]
            if not processed:
                continue
            new_idom = processed[0]
            for p in processed[1:]:
                new_idom = _intersect(new_idom, p, rpo_index, idom)
            if idom[b] != new_idom:
                idom[b] = new_idom
                changed = True

    # --- dominator sets ----------------------------------------------------
    dom: dict[str, set[str]] = {}
    for b in rpo:
        s: set[str] = {b}
        d = idom.get(b)
        if d is not None:
            s |= dom[d]
        dom[b] = s

    children: dict[str, list[str]] = {l: [] for l in labels}
    for b in labels:
        d = idom.get(b)
        if d is not None:
            children[d].append(b)

    # --- dominance frontiers (Cytron et al.) -------------------------------
    df: dict[str, set[str]] = {l: set() for l in labels}
    for b in rpo:
        bpreds = preds[b]
        if len(bpreds) >= 2:
            b_idom = idom[b]
            for p in bpreds:
                runner = p
                while runner is not None and runner != b_idom:
                    df[runner].add(b)
                    runner = idom.get(runner)

    return DomInfo(
        entry=func.entry,
        labels=labels,
        removed=removed,
        rpo_index=rpo_index,
        idom=idom,
        dom=dom,
        children=children,
        df=df,
        preds=preds,
        succs=succs,
    )
