"""Integrity checks for SSA-form functions.

``verify_ssa`` enforces the four properties acceptance asks about:

1. **single static assignment** - every value name is defined exactly once
   (parameter, phi result, or instruction result);
2. **dominance** - every use (instruction operand, terminator cond/operand)
   is dominated by its definition;
3. **phi consistency** - the phi order at a block matches the argument
   order on every incoming edge, every predecessor contributes exactly one
   row, and the incoming value is defined on that edge;
4. **CFG sanity** - terminators reference existing blocks, entry exists.
"""
from __future__ import annotations

from ..errors import VerifyError
from ..ir import Function
from .dominance import compute_dominance


def _definitions(f: Function) -> dict[str, str]:
    """value -> human description of its defining block."""
    defs: dict[str, str] = {}

    def add(name: str, where: str):
        if name in defs:
            raise VerifyError(
                f"SSA value {name} is defined more than once "
                f"({defs[name]} and {where})")
        defs[name] = where

    for p in f.params:
        add(p, f.entry)
    for label in f.ordered_labels:
        b = f.blocks[label]
        for phi in b.phis:
            add(phi.dest, label)
        for ins in b.instrs:
            if ins.dest is not None:
                add(ins.dest, label)
    return defs


def verify_ssa(f: Function) -> list[str]:
    """Raise :class:`VerifyError` on the first violation; return notes."""
    if f.entry not in f.blocks:
        raise VerifyError(f"entry block {f.entry!r} does not exist")

    dom = compute_dominance(f, prune_unreachable=False)
    if dom.removed:
        raise VerifyError(
            "function still has unreachable blocks: "
            + ", ".join(dom.removed))

    defs = _definitions(f)

    # -------------------------------------------------- CFG / edge args
    phi_order: dict[str, list[str]] = {
        label: [p.dest for p in f.blocks[label].phis]
        for label in f.ordered_labels
    }
    for label in f.ordered_labels:
        b = f.blocks[label]
        if b.term is None:
            raise VerifyError(f"block {label!r} has no terminator")
        for s in b.term.successors():
            if s not in f.blocks:
                raise VerifyError(
                    f"block {label!r} terminates at missing block {s!r}")
            if s in dom.removed:
                raise VerifyError(
                    f"block {label!r} branches into unreachable block {s!r}")
            n_phi = len(phi_order[s])
            if n_phi:
                args = b.term.edge_args(s)
                if len(args) != n_phi:
                    raise VerifyError(
                        f"edge {label} -> {s} carries {len(args)} args but "
                        f"{s!r} has {n_phi} phis")

    # -------------------------------------------------- phi rows
    for label in f.ordered_labels:
        b = f.blocks[label]
        preds = dom.preds.get(label, [])
        for phi in b.phis:
            if len(phi.incoming) != len(preds):
                raise VerifyError(
                    f"phi {phi.dest} in {label!r} has {len(phi.incoming)} "
                    f"incoming rows but block has {len(preds)} predecessors")
            row_preds = [p for p, _ in phi.incoming]
            if sorted(row_preds) != sorted(preds):
                raise VerifyError(
                    f"phi {phi.dest} predecessors {row_preds} do not match "
                    f"CFG predecessors {preds}")
            for p, val in phi.incoming:
                if val not in defs:
                    raise VerifyError(
                        f"phi {phi.dest} receives undefined value {val} "
                        f"from {p!r}")
                # value must dominate the predecessor block (be usable on
                # that outgoing edge)
                if not dom.dominates(defs[val], p):
                    raise VerifyError(
                        f"phi {phi.dest} incoming {val} from {p!r} is not "
                        f"defined on that edge")

    # -------------------------------------------------- operand dominance
    for label in f.ordered_labels:
        b = f.blocks[label]
        phi_dests = {p.dest for p in b.phis}
        # non-phi instructions
        for ins in b.instrs:
            for o in ins.operands:
                if o not in defs:
                    raise VerifyError(
                        f"instruction in {label!r} uses undefined value {o}")
                if not dom.dominates(defs[o], label):
                    raise VerifyError(
                        f"instruction in {label!r} uses {o} whose definition "
                        f"does not dominate {label!r}")
        t = b.term
        for o in list(t.operands) + ([t.cond] if t.cond else []):
            if o not in defs:
                raise VerifyError(
                    f"terminator in {label!r} uses undefined value {o}")
            if not dom.dominates(defs[o], label):
                raise VerifyError(
                    f"terminator in {label!r} uses {o} whose definition does "
                    f"not dominate {label!r}")
        # edge arguments are defined on the outgoing edge
        for s in t.successors():
            for o in t.edge_args(s):
                if o not in defs:
                    raise VerifyError(
                        f"edge {label} -> {s} passes undefined value {o}")
                if not dom.dominates(defs[o], label):
                    raise VerifyError(
                        f"edge {label} -> {s} passes {o}, not defined on "
                        f"that edge")

    return ["single-definition ok", "dominance ok", "phi consistency ok"]
