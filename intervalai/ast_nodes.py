"""Abstract syntax tree for Imp.

Every node stores its source :class:`~intervalai.source.Loc` span so that
analysis alarms and execution errors can point at the exact source construct.
"""

from dataclasses import dataclass, field
from typing import List, Optional

from .source import Loc


# ---------------------------------------------------------------- expressions

@dataclass
class IntLit:
    value: int
    loc: Loc


@dataclass
class BoolLit:
    value: bool
    loc: Loc


@dataclass
class Var:
    name: str
    loc: Loc


@dataclass
class ArrayRef:
    name: str
    index: object          # Expr
    loc: Loc


@dataclass
class Unary:
    op: str                # "-" or "not"
    expr: object
    loc: Loc


@dataclass
class Binary:
    op: str                # arithmetic / comparison / logical, canonical spelling
    lhs: object
    rhs: object
    loc: Loc


# ----------------------------------------------------------------- statements

@dataclass
class Assign:
    target: object         # Var | ArrayRef
    value: object          # Expr
    loc: Loc


@dataclass
class If:
    cond: object
    then_body: List[object]
    else_body: Optional[List[object]]
    loc: Loc


@dataclass
class While:
    cond: object
    body: List[object]
    loc: Loc


# --------------------------------------------------------------- declarations

@dataclass
class VarDecl:
    name: str
    init: object          # None, an IntLit (incl. negative), or an Expr
    is_input: bool
    loc: Loc


@dataclass
class ArrDecl:
    name: str
    size: int
    elems: List[int]
    loc: Loc              # whole declaration
    size_loc: Loc = field(repr=False, default=None)


@dataclass
class Program:
    var_decls: List[VarDecl]
    arr_decls: List[ArrDecl]
    body: List[object]
    loc: Loc
