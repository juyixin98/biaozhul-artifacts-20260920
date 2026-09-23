"""Explicit IR produced by closure conversion.

The IR has two layers:

* statements (:class:`IBlock`, :class:`IIf`, :class:`IWhile`, :class:`IReturn`,
  :class:`IExprStmt`) keep structured control flow;
* expressions are flattened into small stack-machine instruction lists
  (:class:`Instr`).

After this pass there are no symbolic variable references anymore.  Every use
is one of:

* ``LOCAL``  - a slot private to the activation,
* ``CELL``   - a heap-allocated box living in a local slot,
* ``FREE``   - a cell received through the closure's explicit free vector.

Functions are closed at creation time (``MAKE_CLOSURE``); nested functions are
lifted to top-level IR functions and receive captured cells as arguments.
"""

from __future__ import annotations

from dataclasses import dataclass, field

# Stack-machine opcodes (sub-expressions).  Stack effects are documented.
#
# CONST idx            [... -> ..., value]
# GET_LOCAL slot       push raw value or the Cell stored in the slot
# SET_LOCAL slot       [..., value -> ...]
# BOX slot             wrap raw local value into a Cell (prologue, params)
# NEW_CELL slot        local slot := fresh empty Cell (prologue)
# DEAD slot            mark a block-local slot uninitialized on block entry (TDZ)
# CELL_DEREF           [..., cell -> ..., value]
# CELL_SET             [..., cell, value -> ...]
# GET_FREE idx         push the Cell at free-vector position idx
# GET_BUILTIN name     push built-in function value
# MAKE_CLOSURE fid     pop len(captures) cells, push closure
# MAKE_CLOSURE_SELF fid  like MAKE_CLOSURE, then tie self cell to the closure
# CALL n               [..., callee, arg1..argn -> ..., result]
# UNARY op            [..., x -> ..., result]
# BINARY op           [..., a, b -> ..., result]
# POP                  discard top

@dataclass
class Instr:
    op: str
    arg: object = None
    loc: object = None

    def to_dict(self) -> dict:
        return {"op": self.op, "arg": _dump_arg(self.arg), "loc": self.loc.to_dict() if self.loc else None}


def _dump_arg(a):
    if hasattr(a, "to_dict"):
        return a.to_dict()
    if isinstance(a, list):
        return [_dump_arg(x) for x in a]
    return a


@dataclass
class IBlock:
    stmts: list = field(default_factory=list)

    def to_dict(self) -> dict:
        return {"kind": "block", "stmts": [s.to_dict() for s in self.stmts]}


@dataclass
class IExprStmt:
    instrs: list

    def to_dict(self) -> dict:
        return {"kind": "expr_stmt", "instrs": [i.to_dict() for i in self.instrs]}


@dataclass
class IReturn:
    instrs: object  # list[Instr] | None

    def to_dict(self) -> dict:
        return {"kind": "return", "instrs": None if self.instrs is None else [i.to_dict() for i in self.instrs]}


@dataclass
class IIf:
    cond: list
    then: IBlock
    otherwise: object  # IBlock | None
    enter_dead: list = field(default_factory=list)   # direct-let slots of `then`
    other_dead: list = field(default_factory=list)   # direct-let slots of `otherwise`

    def to_dict(self) -> dict:
        return {
            "kind": "if",
            "cond": [i.to_dict() for i in self.cond],
            "then": self.then.to_dict(),
            "otherwise": None if self.otherwise is None else self.otherwise.to_dict(),
            "enter_dead": list(self.enter_dead),
            "other_dead": list(self.other_dead),
        }


@dataclass
class IWhile:
    cond: list
    body: IBlock
    enter_dead: list = field(default_factory=list)   # direct-let slots of body

    def to_dict(self) -> dict:
        return {
            "kind": "while",
            "cond": [i.to_dict() for i in self.cond],
            "body": self.body.to_dict(),
            "enter_dead": list(self.enter_dead),
        }


@dataclass
class IFunc:
    id: str
    name: str
    nparams: int
    nlocals: int
    box_slots: list          # slots holding Cells after prologue
    self_slot: int | None    # local slot reserved for a named-fn-expr self name
    prologue: list           # BOX / NEW_CELL instructions
    # captures: [{"slot", "name", "owner", "self"}] in free-vector order
    captures: list
    body: IBlock
    loc: object = None

    def to_dict(self) -> dict:
        return {
            "id": self.id,
            "name": self.name,
            "nparams": self.nparams,
            "nlocals": self.nlocals,
            "box_slots": list(self.box_slots),
            "self_slot": self.self_slot,
            "prologue": [i.to_dict() for i in self.prologue],
            "captures": self.captures,
            "body": self.body.to_dict(),
            "loc": self.loc.to_dict() if self.loc else None,
        }


@dataclass
class IModule:
    funcs: list               # list[IFunc], nested functions lifted to here
    main: IFunc               # the program body is itself a function ("main")
    constants: list           # deduplicated literal pool (JSON values)

    def to_dict(self) -> dict:
        return {
            "constants": self.constants,
            "main": self.main.to_dict(),
            "funcs": [f.to_dict() for f in self.funcs],
        }
