"""Semantic checks performed after parsing.

* Function names and parameter names are unique.
* Every call resolves to a declared function with the right arity.
* Every variable referenced by an expression is a parameter, a ``let``-bound
  variable in the same function, or a ``catch`` error variable in scope.

Variables have *function scope* (like hoisted ``let``): the name is visible
anywhere inside the function it is declared in. Catch error variables are the
exception: they are visible only inside their own catch block.
"""
from __future__ import annotations

from typing import Dict, List, Set

from . import ast_nodes as ast
from .errors import SemanticError
from .locations import Span


class SemanticChecker:
    def __init__(self, program: ast.Program):
        self.program = program
        self.funcs: Dict[str, ast.Function] = {}
        # Visitor state for the function currently being checked.
        self.let_names: Set[str] = set()
        self.scope_stack: List[Set[str]] = []
        # >0 while visiting a try body (not a catch body): throwing operations
        # there are handled by that try's catch.
        self.protected_depth = 0
        self.current_fn: ast.Function

    def check(self) -> Dict[str, ast.Function]:
        seen: Set[str] = set()
        for fn in self.program.functions:
            if fn.name in seen:
                raise SemanticError(f"duplicate function {fn.name!r}", fn.span)
            seen.add(fn.name)
            if len(set(fn.params)) != len(fn.params):
                raise SemanticError(f"duplicate parameter in function {fn.name!r}", fn.span)
            self.funcs[fn.name] = fn
        for fn in self.program.functions:
            self.check_function(fn)
        return self.funcs

    # ---------- per-function name collection ----------

    def collect_let_names(self, block: ast.Block, acc: Set[str]) -> None:
        for stmt in block.statements:
            self.collect_let_names_stmt(stmt, acc)

    def collect_let_names_stmt(self, stmt: ast.Stmt, acc: Set[str]) -> None:
        if isinstance(stmt, (ast.AcquireStmt, ast.LetCallStmt)):
            acc.add(stmt.target)
        elif isinstance(stmt, ast.IfStmt):
            self.collect_let_names(stmt.then_block, acc)
            if stmt.else_block is not None:
                self.collect_let_names(stmt.else_block, acc)
        elif isinstance(stmt, ast.WhileStmt):
            self.collect_let_names(stmt.body, acc)
        elif isinstance(stmt, ast.TryStmt):
            self.collect_let_names(stmt.try_block, acc)
            # catch error variable is handled scoped; lets inside catch are hoisted
            self.collect_let_names(stmt.catch_block, acc)

    def check_function(self, fn: ast.Function) -> None:
        self.let_names = set()
        self.scope_stack = [set(fn.params)]
        self.protected_depth = 0
        self.current_fn = fn
        self.collect_let_names(fn.body, self.let_names)
        self.check_block(fn.body)

    def is_visible(self, name: str) -> bool:
        if name in self.let_names:
            return True
        return any(name in scope for scope in self.scope_stack)

    # ---------- statement checks ----------

    def check_block(self, block: ast.Block) -> None:
        for stmt in block.statements:
            self.check_stmt(stmt)

    def check_stmt(self, stmt: ast.Stmt) -> None:
        if isinstance(stmt, ast.AcquireStmt):
            return
        if isinstance(stmt, ast.LetCallStmt):
            self.check_call(stmt.call)
            return
        if isinstance(stmt, ast.ExprStmt):
            self.check_call(stmt.call)
            return
        if isinstance(stmt, ast.ReleaseStmt):
            self.require_var(stmt.target, stmt.span)
            return
        if isinstance(stmt, ast.UseStmt):
            self.require_var(stmt.target, stmt.span)
            return
        if isinstance(stmt, ast.ReturnStmt):
            if stmt.value is not None:
                self.check_expr(stmt.value)
            return
        if isinstance(stmt, ast.ThrowStmt):
            self.require_throws_context(stmt.span, "throw statement")
            return
        if isinstance(stmt, ast.IfStmt):
            self.check_expr(stmt.cond)
            self.check_block(stmt.then_block)
            if stmt.else_block is not None:
                self.check_block(stmt.else_block)
            return
        if isinstance(stmt, ast.WhileStmt):
            self.check_expr(stmt.cond)
            self.check_block(stmt.body)
            return
        if isinstance(stmt, ast.TryStmt):
            self.protected_depth += 1
            try:
                self.check_block(stmt.try_block)
            finally:
                self.protected_depth -= 1
            self.scope_stack.append({stmt.error_var})
            try:
                self.check_block(stmt.catch_block)
            finally:
                self.scope_stack.pop()
            return
        raise SemanticError(f"unsupported statement {type(stmt).__name__}", stmt.span)

    def require_var(self, name: str, span: Span) -> None:
        if not self.is_visible(name):
            raise SemanticError(f"unknown variable {name!r}", span)

    def require_throws_context(self, span: Span, what: str) -> None:
        """Checked-exception rule.

        A throwing operation is legal when the enclosing function is declared
        ``throws`` or when the operation is textually inside a protected try
        body whose catch handles the exception.
        """
        if self.current_fn.throws or self.protected_depth > 0:
            return
        raise SemanticError(
            f"uncaught {what}: enclosing function {self.current_fn.name!r} is not "
            "declared 'throws' and the operation is not inside a try block",
            span,
        )

    # ---------- expression checks ----------

    def check_expr(self, expr: ast.Expr) -> None:
        if isinstance(expr, (ast.IntLit, ast.StrLit, ast.BoolLit, ast.NullLit)):
            return
        if isinstance(expr, ast.VarRef):
            self.require_var(expr.name, expr.span)
            return
        if isinstance(expr, ast.UnaryOp):
            self.check_expr(expr.operand)
            return
        if isinstance(expr, ast.BinaryOp):
            self.check_expr(expr.left)
            self.check_expr(expr.right)
            return
        if isinstance(expr, ast.CallExpr):
            raise SemanticError(
                "function calls are only allowed as a complete statement "
                "(not inside conditions, arguments or return expressions)",
                expr.span,
            )
        raise SemanticError(f"unsupported expression {type(expr).__name__}", expr.span)

    def check_call(self, call: ast.CallExpr) -> None:
        fn = self.funcs.get(call.name)
        if fn is None:
            raise SemanticError(f"call to undeclared function {call.name!r}", call.span)
        if len(call.args) != len(fn.params):
            raise SemanticError(
                f"function {call.name!r} expects {len(fn.params)} argument(s), "
                f"got {len(call.args)}",
                call.span,
            )
        for arg in call.args:
            self.check_expr(arg)
        if fn.throws:
            self.require_throws_context(
                call.span,
                f"call to throwing function {call.name!r}",
            )


def check_program(program: ast.Program) -> Dict[str, ast.Function]:
    return SemanticChecker(program).check()
