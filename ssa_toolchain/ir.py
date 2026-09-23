"""Integer IR data structures, used before, during and after SSA conversion.

Design
------
* Values: SSA temporaries are written ``%name``.  Memory slots for source
  variables keep their bare name (``x``) and are accessed with explicit
  ``load`` / ``store`` instructions before promotion.
* Every non-terminator instruction has at most one ``dest``.
* Constants are always materialised by a ``const`` instruction, so operands
  elsewhere are uniformly value names.
* Terminators: ``ret``, ``jmp`` and ``br``.  After phi insertion the
  jmp/br edges carry the argument list matching the destination block's
  phis.
* ``phi`` nodes live at the top of a block with ``[pred value]`` pairs.

All nodes optionally carry a source :class:`~ssa_toolchain.errors.Loc`.
Synthetic nodes created during SSA construction / destruction have no loc.
"""
from __future__ import annotations

from dataclasses import dataclass, field
from typing import Optional

from .errors import Loc


@dataclass
class Phi:
    dest: str                       # SSA name, e.g. "%x.phi0"
    var: str                        # original variable name this phi merges
    incoming: list[tuple[str, str]]  # [(pred_label, value_name)]
    loc: Optional[Loc] = None

    def copy(self) -> "Phi":
        return Phi(self.dest, self.var, list(self.incoming), self.loc)


@dataclass
class Instr:
    op: str                         # const|copy|binop|load|store|call|print
    dest: Optional[str]
    operands: list[str] = field(default_factory=list)
    attrs: dict = field(default_factory=dict)
    loc: Optional[Loc] = None

    def copy(self) -> "Instr":
        return Instr(self.op, self.dest, list(self.operands), dict(self.attrs), self.loc)


@dataclass
class Terminator:
    kind: str                       # ret | jmp | br
    # ret: operands == [value] or []
    # jmp: target, args
    # br:  cond, target_t, args_t, target_f, args_f
    operands: list[str] = field(default_factory=list)
    target: Optional[str] = None
    args: list[str] = field(default_factory=list)
    cond: Optional[str] = None
    target_t: Optional[str] = None
    args_t: list[str] = field(default_factory=list)
    target_f: Optional[str] = None
    args_f: list[str] = field(default_factory=list)
    loc: Optional[Loc] = None

    def copy(self) -> "Terminator":
        return Terminator(
            self.kind, list(self.operands), self.target, list(self.args),
            self.cond, self.target_t, list(self.args_t),
            self.target_f, list(self.args_f), self.loc,
        )

    def successors(self) -> list[str]:
        if self.kind == "jmp":
            return [self.target]  # type: ignore[list-item]
        if self.kind == "br":
            return [self.target_t, self.target_f]  # type: ignore[list-item]
        return []

    def edge_args(self, succ: str) -> list[str]:
        if self.kind == "jmp":
            return list(self.args)
        if succ == self.target_t:
            return list(self.args_t)
        return list(self.args_f)

    def set_edge_args(self, succ: str, values: list[str]) -> None:
        if self.kind == "jmp":
            self.args = list(values)
        elif succ == self.target_t:
            self.args_t = list(values)
        else:
            self.args_f = list(values)


def make_ret(value: Optional[str], loc: Optional[Loc] = None) -> Terminator:
    return Terminator(kind="ret", operands=([value] if value is not None else []), loc=loc)


def make_jmp(target: str, args: Optional[list[str]] = None,
             loc: Optional[Loc] = None) -> Terminator:
    return Terminator(kind="jmp", target=target, args=list(args or []), loc=loc)


def make_br(cond: str, t: str, f: str,
            args_t: Optional[list[str]] = None,
            args_f: Optional[list[str]] = None,
            loc: Optional[Loc] = None) -> Terminator:
    return Terminator(
        kind="br", cond=cond, target_t=t, target_f=f,
        args_t=list(args_t or []), args_f=list(args_f or []), loc=loc,
    )


@dataclass
class Block:
    name: str
    phis: list[Phi] = field(default_factory=list)
    instrs: list[Instr] = field(default_factory=list)
    term: Optional[Terminator] = None
    loc: Optional[Loc] = None

    def copy(self) -> "Block":
        return Block(
            self.name,
            [p.copy() for p in self.phis],
            [i.copy() for i in self.instrs],
            self.term.copy() if self.term else None,
            self.loc,
        )


@dataclass
class Function:
    name: str
    params: list[str]                # SSA names "%p0", ...
    entry: str
    blocks: dict[str, Block]        # insertion-ordered
    ordered_labels: list[str] = field(default_factory=list)
    loc: Optional[Loc] = None

    def add_block(self, block: Block) -> Block:
        if block.name in self.blocks:
            raise ValueError(f"duplicate block {block.name}")
        self.blocks[block.name] = block
        self.ordered_labels.append(block.name)
        return block

    def new_block_name(self, hint: str) -> str:
        if hint not in self.blocks:
            return hint
        i = 1
        while f"{hint}{i}" in self.blocks:
            i += 1
        return f"{hint}{i}"

    def preds(self) -> dict[str, list[str]]:
        out: dict[str, list[str]] = {l: [] for l in self.ordered_labels}
        for l in self.ordered_labels:
            term = self.blocks[l].term
            if term is None:
                continue
            for s in term.successors():
                if s in out:
                    out[s].append(l)
        return out

    def copy(self) -> "Function":
        f = Function(self.name, list(self.params), self.entry,
                     {}, list(self.ordered_labels), self.loc)
        for l in self.ordered_labels:
            f.blocks[l] = self.blocks[l].copy()
        return f


@dataclass
class Module:
    funcs: dict[str, Function] = field(default_factory=dict)

    def add_function(self, f: Function) -> Function:
        self.funcs[f.name] = f
        return f

    def copy(self) -> "Module":
        m = Module()
        for name, f in self.funcs.items():
            m.funcs[name] = f.copy()
        return m


# --------------------------------------------------------------------- dump

BIN_OPS = {"+", "-", "*", "/", "%", "==", "!=", "<", "<=", ">", ">=",
           "&", "|", "^", "<<", ">>", "&&", "||"}


def _fmt_edge(label: str, args: list[str]) -> str:
    if args:
        return f"{label}({', '.join(args)})"
    return label


def dump_function(f: Function) -> str:
    lines = [f"func {f.name}({', '.join(f.params)}) {{"]
    for label in f.ordered_labels:
        b = f.blocks[label]
        lines.append(f"{label}:")
        for phi in b.phis:
            pairs = " ".join(f"[{l} {v}]" for l, v in phi.incoming)
            lines.append(f"  {phi.dest} = phi {pairs}")
        for ins in b.instrs:
            lines.append("  " + _dump_instr(ins))
        lines.append("  " + _dump_term(b.term))
    lines.append("}")
    return "\n".join(lines)


def _dump_instr(ins: Instr) -> str:
    lhs = f"{ins.dest} = " if ins.dest else ""
    if ins.op == "const":
        return f"{lhs}const {ins.attrs['value']}"
    if ins.op == "copy":
        return f"{lhs}copy {ins.operands[0]}"
    if ins.op == "binop":
        return f"{lhs}{ins.attrs['binop']} {ins.operands[0]}, {ins.operands[1]}"
    if ins.op == "load":
        return f"{lhs}load {ins.attrs['var']}"
    if ins.op == "store":
        return f"store {ins.operands[0]} -> {ins.attrs['var']}"
    if ins.op == "call":
        args = ", ".join(ins.operands)
        return f"{lhs}call {ins.attrs['name']}({args})"
    if ins.op == "print":
        return f"print {ins.operands[0]}"
    return f"{lhs}{ins.op} {', '.join(ins.operands)}"


def _dump_term(t: Optional[Terminator]) -> str:
    if t is None:
        return "<unterminated>"
    if t.kind == "ret":
        return ("ret " + t.operands[0]) if t.operands else "ret"
    if t.kind == "jmp":
        return f"jmp {_fmt_edge(t.target, t.args)}"
    return (f"br {t.cond}, {_fmt_edge(t.target_t, t.args_t)}, "
            f"{_fmt_edge(t.target_f, t.args_f)}")


def dump_module(m: Module) -> str:
    return "\n\n".join(dump_function(m.funcs[n]) for n in m.funcs)
