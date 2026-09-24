"""AST traversal helpers."""

from __future__ import annotations

from collections.abc import Iterator

from . import ast_nodes as ast


def walk_expr(expr: ast.Expr) -> Iterator[ast.Expr]:
    """Yield ``expr`` and every nested expression (pre-order)."""
    yield expr
    if isinstance(expr, ast.UnaryOp):
        yield from walk_expr(expr.operand)
    elif isinstance(expr, ast.BinaryOp):
        yield from walk_expr(expr.left)
        yield from walk_expr(expr.right)
    elif isinstance(expr, ast.Call):
        for arg in expr.args:
            yield from walk_expr(arg)


def walk_stmt(stmt: ast.Stmt) -> Iterator[ast.Stmt]:
    """Yield ``stmt`` and every nested statement (pre-order)."""
    yield stmt
    if isinstance(stmt, ast.IfStmt):
        for s in stmt.then_body:
            yield from walk_stmt(s)
        for s in stmt.else_body:
            yield from walk_stmt(s)
    elif isinstance(stmt, ast.WhileStmt):
        for s in stmt.body:
            yield from walk_stmt(s)


def calls_in_stmt(stmt: ast.Stmt) -> Iterator[ast.Call]:
    """Every call expression appearing in a statement (conditions included)."""
    for s in walk_stmt(stmt):
        if isinstance(s, ast.VarDecl) and s.init is not None:
            yield from (e for e in walk_expr(s.init) if isinstance(e, ast.Call))
        elif isinstance(s, ast.Assign):
            yield from (e for e in walk_expr(s.value) if isinstance(e, ast.Call))
        elif isinstance(s, ast.ExprStmt):
            yield from (e for e in walk_expr(s.expr) if isinstance(e, ast.Call))
        elif isinstance(s, ast.ReturnStmt) and s.value is not None:
            yield from (e for e in walk_expr(s.value) if isinstance(e, ast.Call))
        elif isinstance(s, (ast.IfStmt, ast.WhileStmt)):
            yield from (e for e in walk_expr(s.cond) if isinstance(e, ast.Call))
