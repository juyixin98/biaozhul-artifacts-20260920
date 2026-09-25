"""Abstract syntax tree for ResFlow.

Every node carries a :class:`~resflow.lexer.Location` so source positions are
preserved all the way into the control-flow graph and the final diagnostics.
"""

from dataclasses import dataclass, field

from .lexer import Location


@dataclass
class Expr:
    loc: Location


@dataclass
class IntLit(Expr):
    value: int = 0


@dataclass
class BoolLit(Expr):
    value: bool = False


@dataclass
class StrLit(Expr):
    value: str = ""


@dataclass
class VarRef(Expr):
    name: str = ""


@dataclass
class Unary(Expr):
    op: str = ""
    operand: Expr = None


@dataclass
class Binary(Expr):
    op: str = ""
    left: Expr = None
    right: Expr = None


@dataclass
class Stmt:
    loc: Location


@dataclass
class VarDecl(Stmt):
    name: str = ""
    init: Expr = None


@dataclass
class Assign(Stmt):
    target: str = ""
    value: Expr = None


@dataclass
class Acquire(Stmt):
    resource: str = ""


@dataclass
class Release(Stmt):
    resource: str = ""


@dataclass
class Use(Stmt):
    resource: str = ""


@dataclass
class ReturnStmt(Stmt):
    value: Expr = None


@dataclass
class ThrowStmt(Stmt):
    message: str = ""


@dataclass
class IfStmt(Stmt):
    cond: Expr = None
    then_body: list = field(default_factory=list)
    else_body: list = field(default_factory=list)


@dataclass
class WhileStmt(Stmt):
    cond: Expr = None
    body: list = field(default_factory=list)


@dataclass
class TryStmt(Stmt):
    body: list = field(default_factory=list)
    message: str = ""          # exception identifier bound in the catch block
    handler: list = field(default_factory=list)
    catch_loc: Location = None


@dataclass
class Function:
    name: str
    params: list              # list[str]
    body: list                # list[Stmt]
    loc: Location
    close_loc: Location = None  # location of the closing brace

    def local_names(self):
        """All statically visible names: parameters plus ``let`` bindings."""
        names = list(self.params)
        for stmt in self.body:
            self._collect_names(stmt, names)
        return names

    @staticmethod
    def _collect_names(stmt, names):
        if isinstance(stmt, VarDecl) and stmt.name not in names:
            names.append(stmt.name)
        for nested in _child_blocks(stmt):
            for s in nested:
                Function._collect_names(s, names)


def _child_blocks(stmt):
    if isinstance(stmt, IfStmt):
        yield stmt.then_body
        yield stmt.else_body
    elif isinstance(stmt, WhileStmt):
        yield stmt.body
    elif isinstance(stmt, TryStmt):
        yield stmt.body
        yield stmt.handler


def const_eval(expr):
    """Fold an expression of literal constants.

    Returns a Python ``int``/``bool``/``str`` or ``None`` when the value
    cannot be decided statically (e.g. it names a variable).  The analysis
    uses this to skip infeasible branches deterministically; nothing else
    about program semantics depends on it.
    """
    try:
        if isinstance(expr, (IntLit, BoolLit, StrLit)):
            return expr.value
        if isinstance(expr, Unary):
            v = const_eval(expr.operand)
            if v is None:
                return None
            if expr.op == "!":
                return not bool(v)
            if expr.op == "-":
                return -v
            return None
        if isinstance(expr, Binary):
            l = const_eval(expr.left)
            r = const_eval(expr.right)
            if l is None or r is None:
                return None
            op = expr.op
            if op == "+":
                return l + r
            if op == "-":
                return l - r
            if op == "*":
                return l * r
            if op == "/":
                return int(l / r)
            if op == "%":
                return l % r
            if op == "==":
                return l == r
            if op == "!=":
                return l != r
            if op == "<":
                return l < r
            if op == "<=":
                return l <= r
            if op == ">":
                return l > r
            if op == ">=":
                return l >= r
            if op == "&&":
                return bool(l) and bool(r)
            if op == "||":
                return bool(l) or bool(r)
    except (TypeError, ValueError, ZeroDivisionError):
        return None
    return None
