"""SSA control-flow-graph IR for LattLang.

Programs are a single procedure made of labelled basic blocks.  Before SSA
construction variable operands are plain names; after
:func:`lattlang.ssa.build_ssa` every name is a versioned SSA value.

Instruction opcodes:
    const  x = const N
    copy   x = copy y
    unary  x = <neg|not> y
    binary x = <add|sub|mul|div|mod|eq|ne|lt|le|gt|ge|and|or> y z
    print  print x

Terminators:
    jmp L
    br x L1 L2          # L1 when x != 0, L2 when x == 0
    ret                 # normal program end
    unreachable         # execution reaches an op that must trap (e.g. /0)

Instructions carry the originating source :class:`Span` where one exists.
Text IR appends it as ``#@ sl:sc-el:ec`` so positions survive a round trip.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field

from .errors import LangError, Span, synthetic_span

UNARY_OPS = {"neg", "not"}
BINARY_OPS = {
    "add": "+", "sub": "-", "mul": "*", "div": "/", "mod": "%",
    "eq": "==", "ne": "!=", "lt": "<", "le": "<=", "gt": ">", "ge": ">=",
    "and": "&&", "or": "||",
}
SYMBOL_TO_OPCODE = {v: k for k, v in BINARY_OPS.items()}
# Opcodes that may trap at runtime (never folded/removed speculatively).
TRAP_OPS = {"div", "mod"}

_IDENT_RE = re.compile(r"^[A-Za-z_$][A-Za-z0-9_.$]*$")
_LABEL_RE = re.compile(r"^[A-Za-z_][A-Za-z0-9_]*$")
_LOC_RE = re.compile(
    r"#@\s*(\d+):(\d+)-(\d+):(\d+)(?:\s+off=(\d+)\s+len=(\d+))?\s*$")


@dataclass(frozen=True)
class Imm:
    value: int


# SSA operands: either a variable name or an immediate integer.
Operand = str | Imm


@dataclass
class Instruction:
    kind: str            # "const" | "copy" | "unary" | "binary" | "print"
    dest: str | None
    args: list[Operand]
    op: str | None = None
    span: Span = field(default_factory=synthetic_span)

    @property
    def may_trap(self) -> bool:
        return self.kind == "binary" and self.op in TRAP_OPS


@dataclass
class Terminator:
    kind: str            # "jmp" | "br" | "ret" | "unreachable"
    targets: list[str] = field(default_factory=list)
    cond: Operand | None = None
    span: Span = field(default_factory=synthetic_span)


@dataclass
class Block:
    label: str
    phis: dict[str, list[Operand]] = field(default_factory=dict)
    # phi order matters for stable printing; dict preserves insertion order
    instrs: list[Instruction] = field(default_factory=list)
    term: Terminator = field(default_factory=lambda: Terminator("unreachable"))
    # filled by index_prog()
    preds: list[str] = field(default_factory=list)
    succs: list[str] = field(default_factory=list)


@dataclass
class IRProgram:
    blocks: list[Block]
    entry: str = "entry"
    # names assigned anywhere (pre-SSA variables get an implicit zero init)
    names: set[str] = field(default_factory=set)

    def block(self, label: str) -> Block:
        for b in self.blocks:
            if b.label == label:
                return b
        raise KeyError(label)

    def labels(self) -> set[str]:
        return {b.label for b in self.blocks}


def index_prog(prog: IRProgram) -> None:
    """Recompute predecessor/successor lists from terminators."""
    labels = {b.label for b in prog.blocks}
    for b in prog.blocks:
        b.succs = [t for t in b.term.targets if t in labels]
        b.preds = []
    for b in prog.blocks:
        for s in b.succs:
            prog.block(s).preds.append(b.label)


# --------------------------------------------------------------------------
# Text printing
# --------------------------------------------------------------------------

def _fmt_operand(o: Operand) -> str:
    return str(o.value) if isinstance(o, Imm) else o


def _loc_comment(span: Span) -> str:
    if span.synthetic:
        return ""
    extra = f" off={span.offset} len={span.length}" if span.offset or span.length else ""
    return f"  #@ {span.start_line}:{span.start_col}-{span.end_line}:{span.end_col}{extra}"


def print_ir(prog: IRProgram) -> str:
    lines: list[str] = []
    for b in prog.blocks:
        lines.append(f"{b.label}:")
        for dest, args in b.phis.items():
            joined = ", ".join(_fmt_operand(a) for a in args)
            lines.append(f"  {dest} = phi [{joined}]")
        for ins in b.instrs:
            lines.append("  " + _fmt_instruction(ins))
        t = b.term
        if t.kind == "jmp":
            body = f"jmp {t.targets[0]}"
        elif t.kind == "br":
            body = (f"br {_fmt_operand(t.cond)} "
                    f"{t.targets[0]} {t.targets[1]}")
        elif t.kind == "ret":
            body = "ret"
        else:
            body = "unreachable"
        lines.append("  " + body + _loc_comment(t.span))
    return "\n".join(lines) + "\n"


def _fmt_instruction(ins: Instruction) -> str:
    if ins.kind == "const":
        body = f"{ins.dest} = const {_fmt_operand(ins.args[0])}"
    elif ins.kind == "copy":
        body = f"{ins.dest} = copy {_fmt_operand(ins.args[0])}"
    elif ins.kind == "unary":
        body = f"{ins.dest} = {ins.op} {_fmt_operand(ins.args[0])}"
    elif ins.kind == "binary":
        body = (f"{ins.dest} = {ins.op} "
                f"{_fmt_operand(ins.args[0])} {_fmt_operand(ins.args[1])}")
    elif ins.kind == "print":
        body = f"print {_fmt_operand(ins.args[0])}"
    else:  # pragma: no cover - defensive
        raise AssertionError(f"unknown instruction kind {ins.kind}")
    return body + _loc_comment(ins.span)


# --------------------------------------------------------------------------
# Text parsing (round trips printer output; used for fixtures and testing)
# --------------------------------------------------------------------------

def _parse_operand(text: str) -> Operand:
    text = text.strip()
    if re.fullmatch(r"-?\d+", text):
        return Imm(int(text))
    if _IDENT_RE.match(text):
        return text
    raise LangError(f"invalid IR operand {text!r}", "ir")


def _parse_span(line: str) -> tuple[Span, str]:
    m = _LOC_RE.search(line)
    if not m:
        return synthetic_span(), line.rstrip()
    sl, sc, el, ec = (int(g) for g in m.groups()[:4])
    off, length = m.group(5), m.group(6)
    span = Span(sl, sc, el, ec, int(off or 0), int(length or 0))
    return span, line[:m.start()].rstrip()


def parse_ir(text: str, entry: str | None = None) -> IRProgram:
    blocks: list[Block] = []
    cur: Block | None = None
    names: set[str] = set()

    def require_block() -> Block:
        if cur is None:
            raise LangError("IR instruction before any block label", "ir")
        return cur

    for raw in text.splitlines():
        line = raw.strip()
        if not line or line.startswith("#"):
            continue
        if line.endswith(":"):
            label = line[:-1]
            if not _LABEL_RE.fullmatch(label):
                raise LangError(f"invalid block label {label!r}", "ir")
            cur = Block(label)
            blocks.append(cur)
            continue
        span, body = _parse_span(line)
        b = require_block()
        m = re.match(r"([A-Za-z_$][\w.$]*)\s*=\s*phi\s*\[(.*)\]$", body)
        if m:
            dest, argstr = m.group(1), m.group(2)
            args = [_parse_operand(p) for p in argstr.split(",")] if argstr.strip() else []
            b.phis[dest] = args
            names.add(dest)
            continue
        m = re.match(r"([A-Za-z_$][\w.$]*)\s*=\s*(\w+)\s*(.*)$", body)
        defined = False
        if m:
            dest, head, rest = m.group(1), m.group(2), m.group(3).strip()
            if head == "const":
                ins = Instruction("const", dest, [_parse_operand(rest)], span=span)
                defined = True
            elif head == "copy":
                ins = Instruction("copy", dest, [_parse_operand(rest)], span=span)
                defined = True
            elif head in UNARY_OPS:
                ins = Instruction("unary", dest, [_parse_operand(rest)], head, span)
                defined = True
            elif head in BINARY_OPS:
                parts = rest.split()
                if len(parts) != 2:
                    raise LangError(f"binary op needs 2 operands: {body!r}", "ir")
                ins = Instruction("binary", dest,
                                  [_parse_operand(parts[0]), _parse_operand(parts[1])],
                                  head, span)
                defined = True
            if defined:
                b.instrs.append(ins)
                names.add(dest)
                for a in ins.args:
                    if isinstance(a, str):
                        names.add(a)
                continue
        # terminators / print
        parts = body.split()
        if parts[0] == "jmp" and len(parts) == 2:
            b.term = Terminator("jmp", [parts[1]], span=span)
        elif parts[0] == "br" and len(parts) == 4:
            b.term = Terminator("br", [parts[2], parts[3]],
                                _parse_operand(parts[1]), span)
        elif parts[0] == "ret" and len(parts) == 1:
            b.term = Terminator("ret", span=span)
        elif parts[0] == "unreachable":
            b.term = Terminator("unreachable", span=span)
        elif parts[0] == "print" and len(parts) == 2:
            b.instrs.append(Instruction("print", None, [_parse_operand(parts[1])],
                                        span=span))
            if isinstance(_parse_operand(parts[1]), str):
                names.add(parts[1])
        else:
            raise LangError(f"unparseable IR line: {body!r}", "ir")

    if entry is None:
        entry = blocks[0].label if blocks else "entry"
    prog = IRProgram(blocks, entry, names)
    index_prog(prog)
    _validate(prog)
    return prog


def _validate(prog: IRProgram) -> None:
    if not prog.blocks:
        raise LangError("IR contains no blocks", "ir")
    labels = {b.label for b in prog.blocks}
    if prog.entry not in labels:
        raise LangError(f"missing entry block {prog.entry!r}", "ir")
    for b in prog.blocks:
        t = b.term
        for target in t.targets:
            if target not in labels:
                raise LangError(f"block {b.label!r} jumps to missing label {target!r}",
                                "ir")
        for args in b.phis.values():
            if len(args) != len(b.preds):
                raise LangError(
                    f"phi arity mismatch in block {b.label!r}: "
                    f"{len(args)} args vs {len(b.preds)} preds",
                    "ir")
