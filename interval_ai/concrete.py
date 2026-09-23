"""Concrete reference interpreter for IntervalLang.

Executes programs over mathematical integers (Python ints) and raises
``RuntimeErr`` on division/modulo by zero or array index out of bounds.
Input is read from a caller-supplied list of integer values: each ``input x;``
statement consumes one value in textual order.

The concrete semantics intentionally mirror the abstract transfer: division
truncates toward zero and ``%`` is the matching truncated remainder.
"""

from __future__ import annotations

from dataclasses import dataclass

from . import ast_nodes as ast
from .errors import RuntimeErr
from .intervals import trunc_div, trunc_rem
from .parser import parse_source


@dataclass
class ConcreteResult:
    env: dict[str, int]
    arrays: dict[str, list[int]]
    consumed_inputs: int
    halted: bool = False
    error: RuntimeErr | None = None


class Interpreter:
    def __init__(self, program: ast.Program, inputs: list[int] | None = None,
                 step_limit: int = 1_000_000):
        self.program = program
        self.inputs = list(inputs or [])
        self.ip = 0
        self.steps = 0
        self.step_limit = step_limit
        self.env: dict[str, int] = {}
        self.arrays: dict[str, list[int]] = {}
        self.array_len: dict[str, int] = {}

    def run(self) -> ConcreteResult:
        try:
            for d in self.program.decls:
                if d.kind == "array":
                    # Arrays start zero-initialized.
                    self.arrays[d.name] = [0] * d.length  # type: ignore[arg-type]
                    self.array_len[d.name] = d.length  # type: ignore[assignment]
                elif d.kind == "input":
                    self.env[d.name] = self.read_input(d.name, d.span)
                elif d.init is not None:
                    self.env[d.name] = self.eval(d.init)
                else:
                    self.env[d.name] = 0
            self.exec_block(self.program.body)
            return ConcreteResult(self.env, self.arrays, self.ip, halted=True)
        except _Halt:
            return ConcreteResult(self.env, self.arrays, self.ip, halted=True)
        except RuntimeErr as e:
            return ConcreteResult(self.env, self.arrays, self.ip,
                                  halted=True, error=e)

    def read_input(self, name: str, span):
        if self.ip >= len(self.inputs):
            raise RuntimeErr(
                "input", f"no input value available for {name!r}", span=span)
        v = self.inputs[self.ip]
        self.ip += 1
        return v

    def tick(self) -> None:
        self.steps += 1
        if self.steps > self.step_limit:
            raise RuntimeErr("timeout",
                             f"step limit {self.step_limit} exceeded "
                             f"(possible non-terminating program)")

    # ---- statements ------------------------------------------------------

    def exec_block(self, block: ast.Block) -> None:
        for s in block.stmts:
            self.exec_stmt(s)

    def exec_stmt(self, s: ast.Stmt) -> None:
        self.tick()
        if isinstance(s, ast.Assign):
            self.env[s.name] = self.eval(s.value)
        elif isinstance(s, ast.ArrayStore):
            idx = self.eval(s.index)
            self.check_index(s.name, idx, s.name_span)
            self.arrays[s.name][idx] = self.eval(s.value)
        elif isinstance(s, ast.InputStmt):
            self.env[s.name] = self.read_input(s.name, s.name_span)
        elif isinstance(s, ast.HavocStmt):
            # Havoc in concrete runs consumes an input too, so the behavior
            # stays deterministic under differential testing.
            self.env[s.name] = self.read_input(s.name, s.name_span)
        elif isinstance(s, ast.Skip):
            pass
        elif isinstance(s, ast.LocalDecl):
            # Local scalar: declare on first sight (0 default), optionally
            # initialize.  Redeclaration resets to the initializer/0.
            self.env[s.name] = self.eval(s.init) if s.init is not None else 0
        elif isinstance(s, ast.If):
            if self.truth(self.eval(s.cond)):
                self.exec_block(s.then)
            elif s.else_ is not None:
                self.exec_block(s.else_)
        elif isinstance(s, ast.While):
            guard = 0
            while self.truth(self.eval(s.cond)):
                guard += 1
                if guard > self.step_limit:
                    raise RuntimeErr("timeout", "loop step limit exceeded",
                                     span=s.span)
                self.exec_block(s.body)
        else:  # pragma: no cover
            raise AssertionError(f"unhandled statement {s!r}")

    def check_index(self, name: str, idx: int, span) -> None:
        n = self.array_len[name]
        if idx < 0 or idx >= n:
            raise RuntimeErr(
                "index_out_of_bounds",
                f"index {idx} out of bounds for array {name!r} of length {n}",
                op="[]", span=span)

    # ---- expressions -----------------------------------------------------

    def eval(self, e: ast.Expr) -> int | bool:
        if isinstance(e, ast.IntLit):
            return e.value
        if isinstance(e, ast.BoolLit):
            return e.value
        if isinstance(e, ast.Var):
            return self.env[e.name]
        if isinstance(e, ast.ArrayLoad):
            idx = self.eval(e.index)
            self.check_index(e.name, idx, e.name_span)
            return self.arrays[e.name][idx]
        if isinstance(e, ast.Unary):
            v = self.eval(e.operand)
            if e.op == "-":
                return -v
            return not self.truth(v)
        if isinstance(e, ast.Binary):
            if e.op == "&&":
                return self.truth(self.eval(e.left)) and \
                    self.truth(self.eval(e.right))
            if e.op == "||":
                return self.truth(self.eval(e.left)) or \
                    self.truth(self.eval(e.right))
            a = self.eval(e.left)
            b = self.eval(e.right)
            if e.op in ("/", "%"):
                if b == 0:
                    raise RuntimeErr(
                        "div_by_zero",
                        ("modulo by zero" if e.op == "%"
                         else "division by zero"),
                        op=e.op, span=e.op_span)
                if e.op == "/":
                    return trunc_div(a, b)
                return trunc_rem(a, b)
            return self.apply_binop(e.op, a, b)
        raise AssertionError(f"unhandled expression {e!r}")  # pragma: no cover

    @staticmethod
    def apply_binop(op, a, b):
        if op == "+":
            return a + b
        if op == "-":
            return a - b
        if op == "*":
            return a * b
        if op == "<":
            return a < b
        if op == "<=":
            return a <= b
        if op == ">":
            return a > b
        if op == ">=":
            return a >= b
        if op == "==":
            return a == b
        if op == "!=":
            return a != b
        raise AssertionError(f"bad op {op!r}")  # pragma: no cover

    @staticmethod
    def truth(v) -> bool:
        if isinstance(v, bool):
            return v
        return v != 0


class _Halt(Exception):
    pass


def run_program(program: ast.Program,
                inputs: list[int] | None = None) -> ConcreteResult:
    return Interpreter(program, inputs).run()


def run_source(source: str,
               inputs: list[int] | None = None) -> ConcreteResult:
    return run_program(parse_source(source), inputs)
