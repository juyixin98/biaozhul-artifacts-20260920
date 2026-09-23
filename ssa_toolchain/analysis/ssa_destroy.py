"""SSA destruction: eliminate phi nodes into executable copy code.

Two classic problems are handled explicitly:

1. **Critical edges.**  When a block with multiple successors jumps to a
   block with phis, the phi arguments cannot be placed at the end of the
   predecessor (its other successors would execute them) nor at the top of
   the successor (its other predecessors would execute them).  Such an edge
   is *split* by inserting a fresh empty edge block on it.

2. **Parallel copy cycles.**  After splitting, each edge carries one
   *parallel copy* covering **all** phis of the destination block at once:
   all sources must be read before any destination is written (``a,b=b,a``
   swaps; and a phi source such as ``i+1`` defined inside the predecessor
   must be computed before that predecessor writes loop-carried values).
   We sequence every parallel copy with the ready-node algorithm and break
   permutation cycles using one temporary (``%cswapN``).

Self copies (``x = x``) are discarded; empty parallel copies emit nothing.
"""
from __future__ import annotations

from ..ir import Block, Function, Instr, make_jmp
from .dominance import compute_dominance


class DestroyResult:
    def __init__(self, func: Function, split_edges: list[str],
                 copies: dict[str, list[tuple[str, str]]],
                 swap_temps: int):
        self.func = func
        self.split_edges = split_edges
        self.copies = copies
        self.swap_temps = swap_temps


def eliminate_phis(func: Function) -> DestroyResult:
    f = func.copy()
    dom = compute_dominance(f, prune_unreachable=False)

    # ---------------------------------------------------------- 1. split
    edges_to_split: list[tuple[str, str]] = []
    for pred in list(f.ordered_labels):
        t = f.blocks[pred].term
        if t is None:
            continue
        succs = t.successors()
        if len(succs) > 1:
            for s in succs:
                if f.blocks[s].phis and len(dom.preds.get(s, ())) > 1:
                    edges_to_split.append((pred, s))

    split_edges: list[str] = []
    edge_n = 0
    for pred, succ in edges_to_split:
        edge_n += 1
        edge_label = f"b.edge{edge_n}.{pred}.{succ}"
        edge_block = Block(edge_label)
        insert_at = f.ordered_labels.index(pred) + 1
        f.ordered_labels.insert(insert_at, edge_label)
        f.blocks[edge_label] = edge_block
        t = f.blocks[pred].term
        args = t.edge_args(succ)
        was_true_branch = (t.kind == "br" and succ == t.target_t)
        if t.kind == "br":
            if was_true_branch:
                t.target_t = edge_label
                t.args_t = []
            else:
                t.target_f = edge_label
                t.args_f = []
        else:
            t.target = edge_label
            t.args = []
        edge_block.term = make_jmp(succ, args)
        split_edges.append(edge_label)

    dom = compute_dominance(f, prune_unreachable=False)

    # ---------------------------------------------------------- 2. copies
    # Collect, per edge (pred -> succ with phis), the full parallel copy
    # (all phi destinations together), then sequence each independently.
    parallel: dict[tuple[str, str], list[tuple[str, str]]] = {}
    for succ in f.ordered_labels:
        dests = [p.dest for p in f.blocks[succ].phis]
        if not dests:
            continue
        for pred in dom.preds[succ]:
            tpred = f.blocks[pred].term
            srcs = tpred.edge_args(succ)
            pairs = [(d, s) for d, s in zip(dests, srcs) if d != s]
            if pairs:
                parallel[(pred, succ)] = pairs
            tpred.set_edge_args(succ, [])

    existing = {ins.dest for b in f.blocks.values() for ins in b.instrs}
    counter = 0
    copies_placed: dict[str, list[tuple[str, str]]] = {}
    swap_temp_count = 0
    for (pred, succ), pairs in parallel.items():
        seq, used, counter, existing = _sequence_parallel_copies(
            pairs, counter, existing)
        swap_temp_count += used
        copies_placed[f"{pred}->{succ}"] = pairs
        f.blocks[pred].instrs.extend(seq)
        # copies now live in the predecessor (edge block or sole-pred
        # block); the terminator must carry no residual phi arguments.
        f.blocks[pred].term.set_edge_args(succ, [])

    for label in f.ordered_labels:
        f.blocks[label].phis = []

    return DestroyResult(f, split_edges, copies_placed, swap_temp_count)


def _sequence_parallel_copies(
    pairs: list[tuple[str, str]],
    counter: int,
    existing: set[str],
) -> tuple[list[Instr], int, int, set[str]]:
    """Sequence one parallel copy ``[(dest, src), ...]``.

    Semantics: every source is read before any destination is written.

    The copy defines a partial function ``dst -> src`` (exactly one write
    per destination, arbitrary fan-in).  Each component is a tree or a
    directed cycle with trees hanging off it.  Algorithm:

    1. Mark every node lying on a cycle.
    2. Emit non-cycle copies whose source is external or already emitted;
       this drains standalone trees and *out-trees* of cycles (nodes that
       read a cycle-produced value), but never a cycle's in-tree, whose
       sources are still pending cycle nodes.
    3. For each cycle left: collect its entire in-tree (non-cycle pending
       nodes that reach this cycle), save their new values into
       temporaries in dependency order (deepest first, reading still-old
       values), rotate the cycle with one temporary, replay the in-tree
       (nearest the cycle first), then drain newly-unlocked out-trees.
    """
    if not pairs:
        return [], 0, counter, existing

    src_of: dict[str, str] = {}
    # reverse edges: src -> destinations that read it
    readers: dict[str, list[str]] = {}
    for d, s in pairs:
        if d in src_of:
            raise ValueError(f"parallel copy writes {d} twice")
        src_of[d] = s
        readers.setdefault(s, []).append(d)

    instrs: list[Instr] = []
    temps = 0

    def fresh_temp() -> str:
        nonlocal counter
        while True:
            counter += 1
            name = f"%cswap{counter}"
            if name not in existing:
                existing.add(name)
                return name

    # ---------------------------------------------------- mark cycle nodes
    on_cycle: set[str] = set()
    for start in src_of:
        if start in on_cycle:
            continue
        seen: dict[str, int] = {}
        path: list[str] = []
        node = start
        while node in src_of and node not in seen:
            seen[node] = len(path)
            path.append(node)
            node = src_of[node]
        if node in seen:
            on_cycle.update(path[seen[node]:])

    pending = dict(src_of)

    def reaches_any_cycle(d: str, memo: dict[str, bool]) -> bool:
        if d in memo:
            return memo[d]
        memo[d] = True   # break pathological self reference
        s = pending.get(d)
        result = False
        if s is not None:
            if s in on_cycle:
                result = True
            elif s in pending:
                result = reaches_any_cycle(s, memo)
        memo[d] = result
        return result

    acyclic_memo: dict[str, bool] = {}

    def emit_available() -> None:
        # Emit non-cycle copies that cannot (through pending writes) flow
        # into a cycle.  Nodes feeding a cycle are reserved for that
        # cycle's component handling.
        progress = True
        while progress:
            progress = False
            for d in list(pending):
                if d in on_cycle:
                    continue
                if reaches_any_cycle(d, acyclic_memo):
                    continue
                s = pending[d]
                if s not in pending:
                    instrs.append(Instr("copy", d, [s]))
                    del pending[d]
                    progress = True

    emit_available()

    while pending:
        # next cycle; enumerate it in functional order: node k is written
        # from the old value of node k+1.
        cyc_seed = next(d for d in pending if d in on_cycle)
        cycle_set = [cyc_seed]
        node = src_of[cyc_seed]
        while node != cyc_seed:
            cycle_set.append(node)
            node = src_of[node]

        # collect the in-tree: every pending non-cycle node that can
        # reach a node of this cycle by following functional edges.
        cycle_members = set(cycle_set)
        reaches: set[str] = set(cycle_set)
        changed = True
        while changed:
            changed = False
            for d, s in pending.items():
                if d in on_cycle or d in reaches:
                    continue
                if s in reaches:
                    reaches.add(d)
                    changed = True
        in_nodes = [d for d in reaches if d not in cycle_members]

        # Ordering: traverse the reversed functional graph (cycle ->
        # readers -> ...) in DFS preorder, so a node is always saved
        # before the node that reads it; every saved value therefore
        # reads the source's still-current (old) location.  Replay is
        # the reverse order.
        ordered: list[str] = []
        seen_tree: set[str] = set()

        def walk(node: str):
            for r in readers.get(node, ()):
                if r in cycle_members or r not in reaches or r in seen_tree:
                    continue
                seen_tree.add(r)
                ordered.append(r)
                walk(r)

        for c in cycle_set:
            walk(c)
        # DFS must visit every in-tree node exactly once
        if len(ordered) != len(in_nodes):  # pragma: no cover - defensive
            missing = set(in_nodes) - set(ordered)
            ordered.extend(sorted(missing))

        # Save in-tree NEW values deepest first (postorder): when saving
        # y <- x then x <- a, y's temporary captures the old x before x's
        # own save can rewrite it.  Replay then happens from nearest the
        # cycle outward.
        save_order = list(reversed(ordered))
        saved: dict[str, str] = {}
        for d in save_order:
            t = fresh_temp()
            temps += 1
            instrs.append(Instr("copy", t, [pending[d]]))
            saved[d] = t

        # Rotate cycle: cycle[k] new = old(cycle[(k+1) % m]).
        m = len(cycle_set)
        ct = fresh_temp()
        temps += 1
        instrs.append(Instr("copy", ct, [cycle_set[0]]))
        for k in range(m - 1):
            instrs.append(Instr("copy", cycle_set[k], [cycle_set[k + 1]]))
        # cycle_set[-1] receives old(cycle_set[0]):
        # directly from ct, unless a pending in-tree node copies it
        incoming = src_of[cycle_set[-1]]
        if incoming in saved:
            instrs.append(Instr("copy", cycle_set[-1], [saved[incoming]]))
        else:
            instrs.append(Instr("copy", cycle_set[-1], [ct]))

        # Replay in-tree in reverse save order (deepest first), so each
        # destination receives its saved new value in place.
        for d in reversed(ordered):
            instrs.append(Instr("copy", d, [saved[d]]))

        for d in cycle_set:
            pending.pop(d, None)
        for d in list(saved):
            pending.pop(d, None)

        emit_available()

    return instrs, temps, counter, existing
