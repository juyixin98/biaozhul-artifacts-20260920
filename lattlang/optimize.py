"""Safety-preserving optimizer driven by the SCCP analysis result.

Pass pipeline (fixpoint over structural reachability after SCCP prunes
edges):

1. **Conditional branch folding** — a branch on a constant becomes a jump;
   a branch on TOP (only reachable through a definitely-trapping path)
   becomes ``unreachable``.
2. **Unreachable block removal** — blocks no longer structurally reachable
   from entry are deleted (this includes blocks SCCP proved dead, which may
   contain ``print`` calls the program never performs), and phi arg lists
   are rebuilt against the remaining predecessor edges.
3. **Constant folding** — uses of proven-constant SSA names are replaced by
   immediate operands.  ``div``/``mod`` with a *constant zero* divisor is
   never folded (the program must still trap when executed); with a proven
   nonzero divisor the operation is total and folds freely.
4. **Dead-code elimination** — pure instructions whose result is unused are
   removed.  ``print``, potential-trap ops, phis and terminators are roots
   where required.

Every edit is recorded in ``changes`` with its source span.
"""

from __future__ import annotations

import copy
from dataclasses import dataclass

from .analysis import SCCPResult, run_sccp
from .ir import IRProgram, Imm, Instruction, Operand, index_prog


@dataclass
class OptimizationReport:
    program: IRProgram
    changes: list[dict]

    def change_count(self) -> int:
        return len(self.changes)


def optimize(prog: IRProgram, result: SCCPResult | None = None) -> OptimizationReport:
    opt = copy.deepcopy(prog)
    res = result or run_sccp(prog)
    changes: list[dict] = []

    _fold_terminators(opt, res, changes)
    _prune_and_rebuild(opt, res, changes)
    _fold_constants(opt, res, changes)
    index_prog(opt)
    _dead_code_elim(opt, changes)
    index_prog(opt)
    return OptimizationReport(opt, changes)


# --------------------------------------------------------------------------
# 1+2: branch folding, block pruning, phi rebuilding
# --------------------------------------------------------------------------

def _fold_terminators(prog: IRProgram, res: SCCPResult,
                      changes: list[dict]) -> None:
    for b in prog.blocks:
        if b.label not in res.reachable:
            continue
        t = b.term
        if t.kind != "br":
            continue
        v = res.value_of(t.cond)
        if v.is_const():
            taken = t.targets[0] if v.value != 0 else t.targets[1]
            dropped = t.targets[1] if v.value != 0 else t.targets[0]
            t.targets = [taken]
            t.kind = "jmp"
            changes.append({
                "kind": "fold_branch",
                "block": b.label,
                "condition": v.value,
                "target": taken,
                "dropped_edge": dropped,
                "span": t.span.to_json(),
            })
        elif v.is_top():
            t.kind = "unreachable"
            t.targets = []
            t.cond = None
            changes.append({
                "kind": "unreachable_terminator",
                "block": b.label,
                "span": t.span.to_json(),
            })


def _structural_reachable(prog: IRProgram) -> set[str]:
    seen = {prog.entry}
    stack = [prog.entry]
    while stack:
        for s in prog.block(stack.pop()).succs:
            if s not in seen:
                seen.add(s)
                stack.append(s)
    return seen


def _prune_and_rebuild(prog: IRProgram, res: SCCPResult,
                       changes: list[dict]) -> None:
    index_prog(prog)
    alive = _structural_reachable(prog)

    for b in prog.blocks:
        if b.label not in alive:
            reason = ("sccp_unreachable" if b.label not in res.reachable
                      else "stranded_by_branch_folding")
            changes.append({
                "kind": "remove_unreachable_block",
                "block": b.label,
                "reason": reason,
                "prints_removed": sum(1 for i in b.instrs if i.kind == "print"),
            })

    # Rebuild phis BEFORE dropping blocks: arguments must be selected using
    # the old predecessor order, since slot i pairs with old preds[i].
    _rebuild_phis(prog, alive, res, changes)

    # Keep only blocks structurally reachable after CFG edge pruning.
    prog.blocks = [b for b in prog.blocks if b.label in alive]
    index_prog(prog)


def _rebuild_phis(prog: IRProgram, alive: set[str], res: SCCPResult,
                  changes: list[dict]) -> None:
    """Drop phi args whose incoming edge died with a pruned predecessor.

    Iteration uses each block's CURRENT (pre-pruning) predecessor list so the
    slot-to-predicate mapping is preserved.  Surviving args that name values
    defined only in a deleted block fall back to their lattice constant.
    """
    defined = _defined_names(prog)
    for b in prog.blocks:
        if b.label not in alive or not b.phis:
            continue
        old_preds = list(b.preds)
        new_phis: dict[str, list[Operand]] = {}
        for dest, args in b.phis.items():
            rebuilt: list[Operand] = []
            for i, pred in enumerate(old_preds):
                if pred not in alive:
                    continue
                arg = args[i] if i < len(args) else Imm(0)
                if isinstance(arg, str) and arg not in defined:
                    lv = res.lat.get(arg)
                    arg = Imm(lv.value) if lv is not None and lv.is_const() else Imm(0)  # type: ignore[assignment]
                rebuilt.append(arg)
            new_phis[dest] = rebuilt
        b.phis = new_phis


def _defined_names(prog: IRProgram) -> set[str]:
    defined = set()
    for b in prog.blocks:
        defined.update(b.phis.keys())
        for ins in b.instrs:
            if ins.dest is not None:
                defined.add(ins.dest)
    return defined


# --------------------------------------------------------------------------
# 3: constant replacement
# --------------------------------------------------------------------------

def _fold_constants(prog: IRProgram, res: SCCPResult,
                    changes: list[dict]) -> None:
    """Replace uses of proven-constant SSA names with immediates."""
    const_of = {name: lv.value for name, lv in res.lat.items() if lv.is_const()}

    def subst(op: Operand) -> Operand:
        if isinstance(op, Imm):
            return op
        return Imm(const_of[op]) if op in const_of else op

    for b in prog.blocks:
        for dest in list(b.phis.keys()):
            b.phis[dest] = [subst(a) for a in b.phis[dest]]
        for ins in b.instrs:
            ins.args = [subst(a) for a in ins.args]
        if b.term.cond is not None:
            b.term.cond = subst(b.term.cond)

    for b in prog.blocks:
        for ins in b.instrs:
            if ins.dest is None:
                continue
            lv = res.lat.get(ins.dest)
            if lv is None or not lv.is_const():
                continue
            if ins.may_trap:
                divisor = ins.args[1]
                if isinstance(divisor, Imm) and divisor.value == 0:
                    # Definite trap: must remain executable.
                    continue
            changes.append({
                "kind": "constant_fold",
                "block": b.label,
                "dest": ins.dest,
                "value": lv.value,
                "op": ins.kind if ins.op is None else f"{ins.kind}:{ins.op}",
                "span": ins.span.to_json(),
            })


# --------------------------------------------------------------------------
# 4: dead-code elimination
# --------------------------------------------------------------------------

def _dead_code_elim(prog: IRProgram, changes: list[dict]) -> None:
    used = _collect_uses(prog)
    for b in prog.blocks:
        kept: list[Instruction] = []
        for ins in b.instrs:
            if _is_root(ins, used):
                kept.append(ins)
            else:
                changes.append({
                    "kind": "remove_dead_instruction",
                    "block": b.label,
                    "dest": ins.dest,
                    "op": ins.kind if ins.op is None else f"{ins.kind}:{ins.op}",
                    "span": ins.span.to_json(),
                })
        b.instrs = kept
    _remove_dead_phis(prog, changes)


def _is_root(ins: Instruction, used: set[str]) -> bool:
    if ins.kind == "print":
        return True
    if ins.may_trap:
        divisor = ins.args[1]
        # Proven nonzero divisor makes the op total: removable if unused.
        if not (isinstance(divisor, Imm) and divisor.value != 0):
            return True
    if ins.dest is None:
        return True
    return ins.dest in used


def _collect_uses(prog: IRProgram) -> set[str]:
    used: set[str] = set()

    def walk(op: Operand) -> None:
        if isinstance(op, str):
            used.add(op)

    for b in prog.blocks:
        for args in b.phis.values():
            for a in args:
                walk(a)
        for ins in b.instrs:
            for a in ins.args:
                walk(a)
        if b.term.cond is not None:
            walk(b.term.cond)
    return used


def _remove_dead_phis(prog: IRProgram, changes: list[dict]) -> None:
    # Fixed point so phis that only feed dead phis disappear as well.
    changed = True
    while changed:
        changed = False
        used = _collect_uses(prog)
        for b in prog.blocks:
            for dest in list(b.phis.keys()):
                if dest in used:
                    continue
                changes.append({
                    "kind": "remove_dead_phi",
                    "block": b.label,
                    "dest": dest,
                })
                del b.phis[dest]
                changed = True
