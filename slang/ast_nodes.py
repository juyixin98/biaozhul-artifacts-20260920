"""AST node definitions for Slang.

Every node stores the source span it came from (``loc``) and identifiers also
store their own ``name_loc`` so later phases can report errors precisely.
Nodes are dataclasses and know how to serialize themselves to plain dicts
(used by the JSON service).
"""

from __future__ import annotations

from dataclasses import dataclass, fields

from .errors import Loc


class Node:
    loc: Loc | None

    def to_dict(self) -> dict:
        out: dict = {"type": type(self).__name__}
        for f in fields(self):
            out[f.name] = _dump(getattr(self, f.name))
        return out


def _dump(v):
    if v is None or isinstance(v, (bool, int, str)):
        return v
    if isinstance(v, Loc):
        return v.to_dict()
    if isinstance(v, Node):
        return v.to_dict()
    if isinstance(v, list):
        return [_dump(x) for x in v]
    if isinstance(v, tuple):
        return [_dump(x) for x in v]
    return repr(v)


# ---------------------------------------------------------------- statements


@dataclass
class Program(Node):
    stmts: list
    loc: Loc | None = None


@dataclass
class Param(Node):
    name: str
    name_loc: Loc | None = None
    loc: Loc | None = None


@dataclass
class Block(Node):
    stmts: list
    loc: Loc | None = None


@dataclass
class Let(Node):
    name: str
    init: object           # Expr | None
    name_loc: Loc | None = None
    loc: Loc | None = None


@dataclass
class FnDecl(Node):
    name: str
    params: list           # list[Param]
    body: Block
    name_loc: Loc | None = None
    loc: Loc | None = None


@dataclass
class Return(Node):
    value: object          # Expr | None
    loc: Loc | None = None


@dataclass
class If(Node):
    cond: object
    then: Block
    otherwise: object      # Block | If | None
    loc: Loc | None = None


@dataclass
class While(Node):
    cond: object
    body: Block
    loc: Loc | None = None


@dataclass
class Assign(Node):
    name: str
    value: object
    name_loc: Loc | None = None
    loc: Loc | None = None


@dataclass
class ExprStmt(Node):
    expr: object
    loc: Loc | None = None


# --------------------------------------------------------------- expressions


@dataclass
class IntLit(Node):
    value: int
    loc: Loc | None = None


@dataclass
class StrLit(Node):
    value: str
    loc: Loc | None = None


@dataclass
class BoolLit(Node):
    value: bool
    loc: Loc | None = None


@dataclass
class NullLit(Node):
    loc: Loc | None = None


@dataclass
class Var(Node):
    name: str
    name_loc: Loc | None = None
    loc: Loc | None = None


@dataclass
class Unary(Node):
    op: str
    operand: object
    loc: Loc | None = None


@dataclass
class Binary(Node):
    op: str
    left: object
    right: object
    loc: Loc | None = None


@dataclass
class Call(Node):
    callee: object
    args: list
    loc: Loc | None = None


@dataclass
class FnExpr(Node):
    """A function literal.  ``name`` is non-None for a named function expression."""

    name: str | None
    params: list           # list[Param]
    body: Block
    name_loc: Loc | None = None
    loc: Loc | None = None
