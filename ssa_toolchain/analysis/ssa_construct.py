"""Mem2reg-style SSA construction (Cytron et al.).

Pipeline
--------
1. Compute dominance on the CFG, pruning blocks unreachable from entry.
2. Remove the unreachable blocks from the function (and report them).
3. Collect every promoted slot: parameters, stores and loads.
4. For each variable, insert a ``phi`` placeholder at the dominance
   frontier of every block that *defines* the variable (iterated
   frontier worklist).
5. Rename in dominator-tree preorder:
      * a parameter seeds its value stack before entering the entry;
      * a ``load x`` disappears; its result name is aliased to the top
        of x's stack for all later operand references;
      * a ``store v -> x`` disappears and pushes v on x's stack;
      * a phi result pushes itself;
      * terminator edge arguments are written from the running stacks.
6. Phi incoming lists are rebuilt from the edge arguments recorded on
   each predecessor terminator (single source of truth), in CFG
   predecessor order.

Each resulting SSA value has exactly one textual definition, confirmed by
:mod:`ssa_toolchain.analysis.verify`.
"""
from __future__ import annotations

from ..errors import LoweringError, VerifyError
from ..ir import Function, Instr, Phi
from .dominance import DomInfo, compute_dominance


class SSAConstructResult:
    def __init__(self, func: Function, dom: DomInfo, variables: list[str],
                 phi_blocks: dict[str, list[str]], removed: list[str]):
        self.func = func
        self.dom = dom
        self.variables = variables
        self.phi_blocks = phi_blocks
        self.removed = removed


def construct_ssa(func: Function,
                  param_names: list[str] | None = None) -> SSAConstructResult:
    """Return a new SSA-form function; the input is not mutated."""
    f = func.copy()
    dom = compute_dominance(f, prune_unreachable=True)

    # -------------------------------------------------- prune unreachable
    removed = dom.removed
    for l in removed:
        del f.blocks[l]
    f.ordered_labels = [l for l in f.ordered_labels if l not in removed]
    for l in f.ordered_labels:
        t = f.blocks[l].term
        if t is None:
            continue
        for s in t.successors():
            if s in removed:
                raise LoweringError(
                    f"reachable block {l!r} falls through to pruned block {s!r}",
                    t.loc,
                )

    # -------------------------------------------------- collect variables
    if param_names is None:
        param_names = [f"__param{i}__" for i in range(len(f.params))]
    if len(param_names) != len(f.params):
        raise LoweringError("parameter name table length mismatch")

    all_vars: set[str] = set()
    def_blocks: dict[str, set[str]] = {}
    for pname in param_names:
        all_vars.add(pname)
        def_blocks.setdefault(pname, set()).add(f.entry)
    for label in f.ordered_labels:
        b = f.blocks[label]
        for ins in b.instrs:
            if ins.op == "store":
                v = ins.attrs["var"]
                all_vars.add(v)
                def_blocks.setdefault(v, set()).add(label)
            elif ins.op == "load":
                all_vars.add(ins.attrs["var"])
    variables = sorted(all_vars)

    # -------------------------------------------------- insert phi nodes
    phi_counter = 0
    phi_blocks: dict[str, list[str]] = {}
    for var in variables:
        worklist = list(def_blocks.get(var, ()))
        seen_def = set(worklist)
        seen_phi: set[str] = set()
        while worklist:
            d = worklist.pop()
            for y in sorted(dom.df.get(d, ()), key=lambda z: dom.rpo_index[z]):
                if y in seen_phi:
                    continue
                seen_phi.add(y)
                dest = f"%{var}.phi{phi_counter}"
                phi_counter += 1
                f.blocks[y].phis.append(Phi(dest=dest, var=var, incoming=[]))
                phi_blocks.setdefault(var, []).append(y)
                if y not in seen_def:
                    seen_def.add(y)
                    worklist.append(y)

    # -------------------------------------------------- rename
    stacks: dict[str, list[str]] = {v: [] for v in variables}
    # Aliases for results of removed loads: old temp -> canonical SSA name.
    aliases: dict[str, str] = {}

    # Language semantics: every non-parameter slot is zero-initialised at
    # function entry (the frontend guarantees no textual use precedes the
    # declaration, so this value can only reach phi incoming edges on
    # paths that never execute the initialiser - e.g. entry -> loop header
    # before the first back edge, where the phi result is never consumed
    # along that edge).
    from ..ir import Instr
    zero = "%__zero"
    f.blocks[f.entry].instrs.insert(
        0, Instr("const", zero, [], {"value": 0}))

    def resolve(name: str) -> str:
        seen = set()
        while name in aliases and name not in seen:
            seen.add(name)
            name = aliases[name]
        return name

    def push(var: str, value: str) -> None:
        stacks[var].append(value)

    def rename_block(label: str):
        block = f.blocks[label]
        pushed = {v: 0 for v in variables}

        for phi in block.phis:
            push(phi.var, phi.dest)
            pushed[phi.var] += 1

        new_instrs: list[Instr] = []
        for ins in block.instrs:
            ins.operands = [resolve(o) for o in ins.operands]
            if ins.op == "load":
                var = ins.attrs["var"]
                stk = stacks.get(var, ())
                if not stk:
                    raise VerifyError(
                        f"use of possibly-uninitialised variable {var!r} "
                        f"in block {label!r}",
                        ins.loc,
                    )
                aliases[ins.dest] = stk[-1]
                continue
            if ins.op == "store":
                push(ins.attrs["var"], ins.operands[0])
                pushed[ins.attrs["var"]] += 1
                continue
            new_instrs.append(ins)
        block.instrs = new_instrs

        t = block.term
        if t is not None:
            t.operands = [resolve(o) for o in t.operands]
            if t.cond is not None:
                t.cond = resolve(t.cond)
            for succ in t.successors():
                succ_block = f.blocks[succ]
                values: list[str] = []
                for phi in succ_block.phis:
                    stk = stacks.get(phi.var, ())
                    values.append(stk[-1] if stk else "__undefined__")
                t.set_edge_args(succ, values)

        for child in sorted(dom.children.get(label, ()),
                            key=lambda z: dom.rpo_index[z]):
            rename_block(child)

        for v, n in pushed.items():
            if n:
                del stacks[v][len(stacks[v]) - n:]

    for i, pname in enumerate(param_names):
        push(pname, f.params[i])
    for v in variables:
        if not stacks[v]:
            push(v, zero)
    rename_block(f.entry)

    # -------------------------------------------------- fill phi incoming
    for label in f.ordered_labels:
        block = f.blocks[label]
        preds = dom.preds.get(label, [])
        for idx, phi in enumerate(block.phis):
            incoming = []
            for p in preds:
                term = f.blocks[p].term
                if term is None:
                    raise VerifyError(f"predecessor {p!r} has no terminator")
                args = term.edge_args(label)
                if idx >= len(args):
                    raise VerifyError(
                        f"edge {p} -> {label} carries no argument for phi "
                        f"{phi.dest}")
                val = args[idx]
                if val == "__undefined__":
                    raise VerifyError(
                        f"variable {phi.var!r} is uninitialised on edge "
                        f"{p} -> {label}")
                incoming.append((p, val))
            phi.incoming = incoming

    _ensure_unique_names(f)
    return SSAConstructResult(f, dom, variables, phi_blocks, removed)


def _ensure_unique_names(f: Function) -> None:
    seen: set[str] = set()

    def add(name: str, loc):
        if name in seen:
            raise VerifyError(f"SSA value {name} defined more than once", loc)
        seen.add(name)

    for p in f.params:
        add(p, f.loc)
    for label in f.ordered_labels:
        b = f.blocks[label]
        for phi in b.phis:
            add(phi.dest, phi.loc)
        for ins in b.instrs:
            if ins.dest is not None:
                add(ins.dest, ins.loc)
