"""Tree-walking reference interpreter for LattLang ASTs.

Used as the ground-truth observable semantics in equivalence tests:
integer ``print`` output (one value per line) and division-by-zero errors
with the source span of the failing operator.  Every variable reads as 0
before its first assignment.
"""

from __future__ import annotations

from dataclasses import dataclass

from . import ast_nodes as ast
from .errors import LangError
from .semantics import (
    ZeroDivisionTrapped,
    apply_binary,
    apply_unary,
)

MAX_STEPS = 1_000_000


@dataclass
class RunResult:
    output: list[str]
    steps: int
    error: LangError | None = None

    @property
    def ok(self) -> bool:
        return self.error is None

    def stdout(self) -> str:
        return "\n".join(self.output) + ("\n" if self.output else "")


def run_ast(program: ast.Program, max_steps: int = MAX_STEPS) -> RunResult:
    env: dict[str, int] = {}
    output: list[str] = []
    state = {"steps": 0}

    def budget() -> None:
        state["steps"] += 1
        if state["steps"] > max_steps:
            raise LangError("step budget exceeded (possible infinite loop)",
                            "runtime")

    def eval_expr(e: ast.Expr) -> int:
        budget()
        if isinstance(e, ast.IntLit):
            return e.value
        if isinstance(e, ast.BoolLit):
            return int(e.value)
        if isinstance(e, ast.Var):
            return env.get(e.name, 0)
        if isinstance(e, ast.Unary):
            return apply_unary("neg" if e.op == "-" else "not",
                               eval_expr(e.value))
        if isinstance(e, ast.Binary):
            a = eval_expr(e.left)
            b = eval_expr(e.right)
            try:
                return apply_binary(_opcode(e.op), a, b)
            except ZeroDivisionTrapped:
                raise LangError("division or modulo by zero", "runtime",
                                e.span) from None
        raise LangError(f"cannot evaluate {type(e).__name__}", "runtime")  # pragma: no cover

    def run_stmts(stmts: list[ast.Stmt]) -> None:
        for s in stmts:
            budget()
            if isinstance(s, ast.Assign):
                env[s.target] = eval_expr(s.value)
            elif isinstance(s, ast.Print):
                output.append(str(eval_expr(s.value)))
            elif isinstance(s, ast.If):
                if eval_expr(s.cond) != 0:
                    run_stmts(s.then)
                else:
                    run_stmts(s.else_)
            elif isinstance(s, ast.While):
                while eval_expr(s.cond) != 0:
                    run_stmts(s.body)

    try:
        run_stmts(program.body)
    except LangError as e:
        return RunResult(output, state["steps"], e)
    return RunResult(output, state["steps"])


def _opcode(symbol: str) -> str:
    from .ir import SYMBOL_TO_OPCODE
    return SYMBOL_TO_OPCODE[symbol]
