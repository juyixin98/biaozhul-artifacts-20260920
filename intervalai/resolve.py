"""Semantic analysis / name resolution.

The resolver validates the raw AST:

* every identifier must resolve to a declared scalar or array;
* scalar/array kinds must match their use (``x[0]`` requires an array);
* expression typing is checked strictly (arithmetic over ints, comparisons
  and logical connectives over booleans; only integers live in variables);
* array sizes are positive integer literals and initializers, when present,
  give exactly that many integer literals;
* declarations are unique.

Resolved expression/statement nodes gain an ``etype`` attribute
(``"int"`` / ``"bool"``); ``Var`` nodes gain ``kind`` (``"scalar"`` /
``"array"``).  ``_Block`` markers produced by the parser are flattened away.
"""

from . import ast_nodes as ast
from .errors import SemanticError
from .parser import _Block


class Resolver:
    def __init__(self, prog: ast.Program):
        self.prog = prog
        self.scalars = {}
        self.arrays = {}

    def resolve(self):
        # ---- declarations
        for d in self.prog.var_decls:
            if d.name in self.scalars or d.name in self.arrays:
                raise SemanticError(f"duplicate declaration of {d.name!r}", d.loc)
            self.scalars[d.name] = d
        for a in self.prog.arr_decls:
            if a.name in self.scalars or a.name in self.arrays:
                raise SemanticError(f"duplicate declaration of {a.name!r}", a.loc)
            if a.size <= 0:
                raise SemanticError("array size must be a positive integer literal",
                                    a.size_loc or a.loc)
            if len(a.elems) == 0:
                a.elems = [0] * a.size
            elif len(a.elems) != a.size:
                raise SemanticError(
                    f"array {a.name!r} declared with size {a.size} but "
                    f"{len(a.elems)} initializer(s) given", a.loc)
            self.arrays[a.name] = a

        # ---- variable initializers
        # A non-constant initializer expression is syntactic sugar for an
        # assignment that runs before the program body (in declaration order).
        leading = []
        for d in self.prog.var_decls:
            if d.is_input or d.init is None:
                continue
            c = _const_value(d.init)
            if c is not None:
                d.init = c
            else:
                if self.expr(d.init) != "int":
                    raise SemanticError(
                        f"initializer of {d.name!r} must be an integer expression",
                        d.loc)
                leading.append(ast.Assign(ast.Var(d.name, d.loc), d.init, d.loc))
                d.init = None

        # ---- statements (blocks flattened)
        self.prog.body = self.stmts(leading + self.prog.body)
        return self.prog

    def stmts(self, ss):
        out = []
        for s in ss:
            if isinstance(s, _Block):
                out.extend(self.stmts(s.stmts))
            else:
                out.append(self.stmt(s))
        return out

    def stmt(self, s):
        if isinstance(s, ast.Assign):
            self.lvalue(s.target)
            vt = self.expr(s.value)
            if vt != "int":
                raise SemanticError(
                    "only integer expressions can be assigned to variables",
                    getattr(s.value, "loc", s.loc))
        elif isinstance(s, ast.If):
            if self.expr(s.cond) != "bool":
                raise SemanticError("if condition must be boolean", s.cond.loc)
            s.then_body = self.stmts(s.then_body)
            if s.else_body is not None:
                s.else_body = self.stmts(s.else_body)
        elif isinstance(s, ast.While):
            if self.expr(s.cond) != "bool":
                raise SemanticError("while condition must be boolean", s.cond.loc)
            s.body = self.stmts(s.body)
        else:
            raise SemanticError(f"unknown statement node {type(s).__name__}",
                                getattr(s, "loc", None))
        return s

    def lvalue(self, e):
        if isinstance(e, ast.Var):
            if e.name not in self.scalars:
                if e.name in self.arrays:
                    raise SemanticError(
                        f"cannot assign array {e.name!r} without an index", e.loc)
                raise SemanticError(f"undeclared variable {e.name!r}", e.loc)
            e.kind = "scalar"
        elif isinstance(e, ast.ArrayRef):
            if e.name in self.scalars:
                raise SemanticError(f"{e.name!r} is not an array", e.loc)
            if e.name not in self.arrays:
                raise SemanticError(f"undeclared array {e.name!r}", e.loc)
            if self.expr(e.index) != "int":
                raise SemanticError("array index must be an integer expression",
                                    e.index.loc)
        else:
            raise SemanticError("invalid assignment target", getattr(e, "loc", None))

    def expr(self, e) -> str:
        if isinstance(e, ast.IntLit):
            e.etype = "int"
        elif isinstance(e, ast.BoolLit):
            e.etype = "bool"
        elif isinstance(e, ast.Var):
            if e.name in self.scalars:
                e.kind = "scalar"
            elif e.name in self.arrays:
                raise SemanticError(
                    f"array {e.name!r} used without an index", e.loc)
            else:
                raise SemanticError(f"undeclared variable {e.name!r}", e.loc)
            e.etype = "int"
        elif isinstance(e, ast.ArrayRef):
            if e.name in self.scalars:
                raise SemanticError(f"{e.name!r} is a scalar variable", e.loc)
            if e.name not in self.arrays:
                raise SemanticError(f"undeclared array {e.name!r}", e.loc)
            if self.expr(e.index) != "int":
                raise SemanticError("array index must be an integer expression",
                                    e.index.loc)
            e.etype = "int"
        elif isinstance(e, ast.Unary):
            t = self.expr(e.expr)
            if e.op == "-":
                if t != "int":
                    raise SemanticError("unary '-' requires an integer", e.loc)
                e.etype = "int"
            else:  # not
                if t != "bool":
                    raise SemanticError("'not' requires a boolean", e.loc)
                e.etype = "bool"
        elif isinstance(e, ast.Binary):
            lt = self.expr(e.lhs)
            rt = self.expr(e.rhs)
            if e.op in ("+", "-", "*", "/", "%"):
                if lt != "int" or rt != "int":
                    raise SemanticError(
                        f"operator {e.op!r} requires integer operands", e.loc)
                e.etype = "int"
            elif e.op in ("==", "!=", "<", "<=", ">", ">="):
                if lt != "int" or rt != "int":
                    raise SemanticError(
                        f"operator {e.op!r} compares integers", e.loc)
                e.etype = "bool"
            else:  # && / ||
                if lt != "bool" or rt != "bool":
                    raise SemanticError(
                        f"operator {e.op!r} requires boolean operands", e.loc)
                e.etype = "bool"
        else:
            raise SemanticError(f"unknown expression node {type(e).__name__}",
                                getattr(e, "loc", None))
        return e.etype


def resolve(prog: ast.Program) -> ast.Program:
    return Resolver(prog).resolve()


def _const_value(e):
    """Return the Python int if ``e`` is a compile-time integer literal
    (allowing a leading unary minus), else None."""
    if isinstance(e, ast.IntLit):
        return e.value
    if isinstance(e, ast.Unary) and e.op == "-" and isinstance(e.expr, ast.IntLit):
        return -e.expr.value
    return None
