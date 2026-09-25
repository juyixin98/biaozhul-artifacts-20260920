"""Custom intermediate representation and control-flow graph.

The IR is deliberately small and independent of the surface syntax:

* values flow through named *registers* (source variables and
  compiler-synthesised temporaries ``%n``);
* a function is a graph of :class:`Block` objects linked only through
  terminators (no fall-through, no phi nodes — joins are performed by the
  analyzer at block entry);
* calls are *terminators* (``CallTerm``) so a return edge and a fall-through
  edge never share a block, which keeps interprocedural CFG edges explicit;
* the taint primitives ``source`` / ``sanitize`` / ``sink`` become first
  class instructions rather than opaque calls.

Every instruction carries the source span of the construct it was lowered
from, so analysis paths can be rendered back to source locations.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Optional

from .location import Span


# ---------------------------------------------------------------------------
# Instructions
# ---------------------------------------------------------------------------

@dataclass(frozen=True)
class Instr:
    uid: int
    span: Span


@dataclass(frozen=True)
class Const(Instr):
    dst: str
    value: str  # canonical constant rendering, e.g. 42, "abc", true


@dataclass(frozen=True)
class Copy(Instr):
    dst: str
    src: str


@dataclass(frozen=True)
class BinOp(Instr):
    dst: str
    op: str
    left: str
    right: str


@dataclass(frozen=True)
class UnOp(Instr):
    dst: str
    op: str
    operand: str


@dataclass(frozen=True)
class SourceInstr(Instr):
    """dst = source()  — introduces a fresh taint origin."""

    dst: str
    kind: str


@dataclass(frozen=True)
class SanitizeInstr(Instr):
    """dst = sanitize(src) — strong cleaner; result never inherits taint."""

    dst: str
    src: str
    kind: str


@dataclass(frozen=True)
class SinkInstr(Instr):
    """sink(arg) — report if arg is tainted."""

    arg: str
    kind: str


@dataclass(frozen=True)
class UnknownCall(Instr):
    """Call to a name that is not a declared function or a marker.

    The analyzer has no body to inspect, so it conservatively assumes the
    result is tainted whenever *any* argument is tainted.
    """

    dst: str
    name: str
    args: tuple[str, ...]


# ---------------------------------------------------------------------------
# Terminators
# ---------------------------------------------------------------------------

@dataclass(frozen=True)
class Terminator:
    span: Span


@dataclass(frozen=True)
class Jmp(Terminator):
    target: str


@dataclass(frozen=True)
class Br(Terminator):
    """Branch on register cond; both edges are always considered possible."""

    cond: str
    then_target: str
    else_target: str


@dataclass(frozen=True)
class Ret(Terminator):
    value: Optional[str]


@dataclass(frozen=True)
class CallTerm(Terminator):
    """Call to a user function.

    ``callee`` is None when the called name is not a declared function;
    the analyzer treats such a call as an opaque conservative operation.
    """

    callee: Optional[str]
    args: tuple[str, ...]
    return_reg: Optional[str]  # None when the call result is discarded
    cont: str  # continuation block label
    site_uid: int  # globally unique call-site id


# ---------------------------------------------------------------------------
# Blocks / functions / program
# ---------------------------------------------------------------------------

@dataclass
class Block:
    label: str
    span: Span
    instructions: list[Instr] = field(default_factory=list)
    terminator: Optional[Terminator] = None


@dataclass(frozen=True)
class FunctionIR:
    name: str
    span: Span
    params: tuple[str, ...]
    entry: str
    blocks: tuple[Block, ...]  # in construction order

    def block(self, label: str) -> Block:
        return self._by_label[label]

    def __post_init__(self):
        # object.__setattr__ because the class is frozen; cache lookup table
        object.__setattr__(self, "_by_label", {b.label: b for b in self.blocks})


@dataclass(frozen=True)
class ProgramIR:
    functions: tuple[FunctionIR, ...]
    call_sites: tuple  # tuple of (site_uid, func_name, callee|None, Span)
    source: str

    def function(self, name: str) -> FunctionIR:
        return self._by_name[name]

    def __post_init__(self):
        object.__setattr__(self, "_by_name", {f.name: f for f in self.functions})
