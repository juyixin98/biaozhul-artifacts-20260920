"""Minimal pruned SSA construction (Cytron et al.).

1. Compute dominators / immediate dominators / dominance frontiers.
2. Insert ``phi`` nodes at frontier blocks of variable definition sites.
3. Rename every definition with a fresh version and fill phi arguments by
   walking the dominator tree with per-name stacks.

Named variables have an implicit initial value 0, so their rename stack
starts at the immediate ``0`` (version 0 is never emitted as a def).
Temporaries (``$tN``) are assigned exactly once by construction but are put
through the same machinery.
"""

from __future__ import annotations

import copy

from .errors import LangError
from .ir import IRProgram, Imm, Operand, index_prog


# --------------------------------------------------------------------------
# Dominance
# --------------------------------------------------------------------------

def _reachable(prog: IRProgram) -> set[str]:
    seen = {prog.entry}
    stack = [prog.entry]
    while stack:
        l = stack.pop()
        for s in prog.block(l).succs:
            if s not in seen:
                seen.add(s)
                stack.append(s)
    return seen


def compute_dominators(prog: IRProgram) -> dict[str, set[str]]:
    """Return dom[block] = set of blocks dominating it (including itself).

    Classic iterative dataflow over the structurally reachable CFG.
    """
    labels = [b.label for b in prog.blocks]
    reachable = _reachable(prog)

    dom: dict[str, set[str]] = {l: (set(labels) if l != prog.entry else {l})
                                for l in labels}
    changed = True
    while changed:
        changed = False
        for l in labels:
            if l == prog.entry or l not in reachable:
                continue
            ps = [p for p in prog.block(l).preds if p in reachable]
            new = set(dom[ps[0]]) if ps else set()
            for p in ps[1:]:
                new &= dom[p]
            new.add(l)
            if new != dom[l]:
                dom[l] = new
                changed = True
    for l in labels:
        if l not in reachable:
            dom[l] = set()
    return dom


def immediate_dominators(dom: dict[str, set[str]]) -> dict[str, str | None]:
    """idom(l) = strict dominator with the largest dominator set."""
    idom: dict[str, str | None] = {}
    for l, ds in dom.items():
        strict = ds - {l}
        idom[l] = max(strict, key=lambda d: len(dom[d])) if strict else None
    return idom


def dominance_frontiers(prog: IRProgram,
                        idom: dict[str, str | None]) -> dict[str, set[str]]:
    """DF(n): join blocks whose some-pred runner reaches n but not block."""
    df: dict[str, set[str]] = {b.label: set() for b in prog.blocks}
    for j in prog.blocks:
        if len(j.preds) < 2:
            continue
        for p in j.preds:
            runner: str | None = p
            while runner is not None and runner != idom[j.label]:
                df[runner].add(j.label)
                runner = idom[runner]
    return df


# --------------------------------------------------------------------------
# Phi insertion
# --------------------------------------------------------------------------

def _defblocks(prog: IRProgram) -> dict[str, set[str]]:
    defs: dict[str, set[str]] = {}
    for b in prog.blocks:
        for dest in b.phis:
            defs.setdefault(dest, set()).add(b.label)
        for ins in b.instrs:
            if ins.dest is not None:
                defs.setdefault(ins.dest, set()).add(b.label)
    return defs


def insert_phis(prog: IRProgram, df: dict[str, set[str]]) -> None:
    defblocks = _defblocks(prog)
    # Compiler temporaries ($tN) are defined exactly once and dominated by
    # their single use, so they never need phi nodes; only user variables do.
    for name, blocks in defblocks.items():
        if name.startswith("$"):
            continue
        work = list(blocks)
        already: set[str] = set()
        while work:
            x = work.pop()
            for y in df.get(x, ()):
                if y in already:
                    continue
                already.add(y)
                block = prog.block(y)
                if name not in block.phis:
                    block.phis[name] = [Imm(0)] * len(block.preds)
                if y not in blocks:
                    blocks.add(y)
                    work.append(y)


# --------------------------------------------------------------------------
# Renaming
# --------------------------------------------------------------------------

class _SSARename:
    def __init__(self, prog: IRProgram, idom: dict[str, str | None]):
        self.prog = prog
        self.idom = idom
        # Monotonic per-base version counters; versions are never reused,
        # so a phi at a join (x.2) cannot collide with a sibling def that
        # happens to sit at the same stack depth.
        self.counts: dict[str, int] = {}
        self.stacks: dict[str, list[Operand]] = {}
        self.children: dict[str, list[str]] = {b.label: [] for b in prog.blocks}
        for l, d in idom.items():
            if d is not None:
                self.children[d].append(l)

    def stk(self, base: str) -> list[Operand]:
        return self.stacks[base]

    def push_def(self, base: str) -> str:
        i = self.counts[base] = self.counts.get(base, 0) + 1
        name = f"{base}.{i}"
        self.stacks[base].append(name)
        return name

    def use(self, op: Operand) -> Operand:
        if isinstance(op, Imm):
            return op
        return self.stk(op)[-1]  # pre-SSA operands carry no version suffix

    def run(self) -> None:
        # Pre-register every name that may be defined or used: the implicit
        # zero init lives at stack depth 0 and the version counter starts at 0.
        names: set[str] = set()
        for b in self.prog.blocks:
            names.update(b.phis.keys())
            for ins in b.instrs:
                if ins.dest is not None:
                    names.add(ins.dest)
                for a in ins.args:
                    if isinstance(a, str):
                        names.add(a)
            if b.term.cond is not None and isinstance(b.term.cond, str):
                names.add(b.term.cond)
        for name in names:
            if name not in self.stacks:
                self.stacks[name] = [Imm(0)]
                self.counts[name] = 0
        self.visit(self.prog.entry)

    def visit(self, label: str) -> None:
        block = self.prog.block(label)
        pushed: list[str] = []

        # 1. rename phi destinations
        new_phis: dict[str, list[Operand]] = {}
        for dest, args in block.phis.items():
            new_phis[self.push_def(dest)] = args
            pushed.append(dest)
        block.phis = new_phis

        # 2. rename ordinary instructions
        for ins in block.instrs:
            ins.args = [self.use(a) for a in ins.args]
            if ins.dest is not None:
                base = ins.dest
                ins.dest = self.push_def(base)
                pushed.append(base)

        # 3. rename terminator condition
        if block.term.cond is not None:
            block.term.cond = self.use(block.term.cond)

        # 4. fill successor phi slots for edge label -> succ
        for succ_label in block.succs:
            succ = self.prog.block(succ_label)
            if not succ.phis or label not in succ.preds:
                continue
            idx = succ.preds.index(label)
            for full_name, args in succ.phis.items():
                base = full_name.split(".")[0]
                args[idx] = self.stk(base)[-1]

        # 5. recurse into dominated children
        for child in self.children.get(label, ()):
            self.visit(child)

        # 6. pop definitions made in this block
        for base in pushed:
            self.stk(base).pop()


def _verify_ssa(prog: IRProgram) -> None:
    defined: set[str] = set()
    for b in prog.blocks:
        defined.update(b.phis.keys())
        for ins in b.instrs:
            if ins.dest is not None:
                if ins.dest in defined:
                    raise LangError(
                        f"SSA invariant violated: {ins.dest} defined twice",
                        "ssa")
                defined.add(ins.dest)


def build_ssa(prog_in: IRProgram) -> IRProgram:
    prog = copy.deepcopy(prog_in)
    index_prog(prog)
    dom = compute_dominators(prog)
    idom = immediate_dominators(dom)
    df = dominance_frontiers(prog, idom)
    insert_phis(prog, df)
    _SSARename(prog, idom).run()
    _verify_ssa(prog)
    index_prog(prog)
    return prog
