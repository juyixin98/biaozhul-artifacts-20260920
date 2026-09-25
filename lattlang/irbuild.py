"""Lowers an AST into the non-SSA CFG IR.

Structured control flow becomes labelled blocks; variables are plain names
at this stage (``lattlang.ssa`` versions them afterwards).  Every named
variable is implicitly initialized to 0 at program entry; the SSA renamer
materializes that initial value, so no initialization instructions are
emitted here.
"""

from __future__ import annotations

from . import ast_nodes as ast
from .errors import LangError
from .ir import (
    Block,
    Imm,
    IRProgram,
    Instruction,
    Operand,
    SYMBOL_TO_OPCODE,
    Terminator,
    index_prog,
)


class IRBuilder:
    def __init__(self) -> None:
        self.blocks: list[Block] = []
        self.names: set[str] = set()
        self._block_counter = 0
        self._temp_counter = 0
        self.entry = self.new_block("entry")

    def new_block(self, hint: str = "b") -> Block:
        self._block_counter += 1
        label = f"b{self._block_counter}_{hint}"
        block = Block(label)
        self.blocks.append(block)
        return block

    def new_temp(self) -> str:
        self._temp_counter += 1
        name = f"$t{self._temp_counter}"
        self.names.add(name)
        return name

    def emit(self, block: Block, ins: Instruction) -> str:
        block.instrs.append(ins)
        if ins.dest is not None:
            self.names.add(ins.dest)
        for a in ins.args:
            if isinstance(a, str):
                self.names.add(a)
        return ins.dest  # type: ignore[return-value]

    def build(self, program: ast.Program) -> IRProgram:
        exit_block = self.new_block("exit")
        cur = self.lower_stmts(program.body, self.entry, exit_block)
        if cur.term.kind == "unreachable":
            cur.term = Terminator("jmp", [exit_block.label])
        exit_block.term = Terminator("ret")
        prog = IRProgram(self._order_blocks(exit_block), self.entry.label,
                         self.names)
        index_prog(prog)
        return prog

    def _order_blocks(self, exit_block: Block) -> list[Block]:
        """Order blocks by CFG discovery from entry (exit kept last)."""
        seen: set[str] = set()
        ordered: list[Block] = []

        def visit(label: str) -> None:
            if label in seen:
                return
            seen.add(label)
            b = next(x for x in self.blocks if x.label == label)
            ordered.append(b)
            for s in b.term.targets:
                visit(s)

        visit(self.entry.label)
        if exit_block.label not in seen:
            ordered.append(exit_block)
            seen.add(exit_block.label)
        for b in self.blocks:
            if b.label not in seen:
                ordered.append(b)
        return ordered

    # -- statements --------------------------------------------------------

    def lower_stmts(self, stmts: list[ast.Stmt], start: Block,
                    after: Block) -> Block:
        """Lower a statement list; return the block control flows into next.

        Simple statements accumulate in the current block.  A fresh
        continuation block is opened after each if/while construct.
        """
        cur = start
        for stmt in stmts:
            if isinstance(stmt, (ast.If, ast.While)):
                cont = self.new_block("cont")
                self.lower_stmt(stmt, cur, cont)
                cur = cont
            else:
                cur = self.lower_stmt(stmt, cur, after)
        if cur.term.kind == "unreachable":
            cur.term = Terminator("jmp", [after.label])
        return after

    def lower_stmt(self, stmt: ast.Stmt, cur: Block, after: Block) -> Block:
        if isinstance(stmt, ast.Assign):
            value = self.lower_expr(stmt.value, cur)
            copy = Instruction("copy", stmt.target, [value], span=stmt.span)
            self.emit(cur, copy)
            self.names.add(stmt.target)
            return cur
        if isinstance(stmt, ast.Print):
            value = self.lower_expr(stmt.value, cur)
            self.emit(cur, Instruction("print", None, [value], span=stmt.span))
            return cur
        if isinstance(stmt, ast.If):
            return self.lower_if(stmt, cur, after)
        if isinstance(stmt, ast.While):
            return self.lower_while(stmt, cur, after)
        raise LangError(f"cannot lower statement {type(stmt).__name__}", "ir")  # pragma: no cover

    def lower_if(self, stmt: ast.If, cur: Block, after: Block) -> Block:
        cond = self.lower_expr(stmt.cond, cur)
        then_block = self.new_block("then")
        else_block = self.new_block("else")
        cur.term = Terminator("br", [then_block.label, else_block.label],
                              cond, stmt.cond.span)
        if stmt.then:
            self.lower_stmts(stmt.then, then_block, after)
        else:
            self._terminate_jmp(then_block, after)
        if stmt.else_:
            self.lower_stmts(stmt.else_, else_block, after)
        else:
            self._terminate_jmp(else_block, after)
        return after

    def lower_while(self, stmt: ast.While, cur: Block, after: Block) -> Block:
        head = self.new_block("while_head")
        body_block = self.new_block("while_body")
        self._terminate_jmp(cur, head)
        cond = self.lower_expr(stmt.cond, head)
        head.term = Terminator("br", [body_block.label, after.label],
                               cond, stmt.cond.span)
        if stmt.body:
            self.lower_stmts(stmt.body, body_block, head)
        else:
            self._terminate_jmp(body_block, head)
        return after

    # -- expressions -------------------------------------------------------

    def lower_expr(self, expr: ast.Expr, block: Block) -> Operand:
        if isinstance(expr, ast.IntLit):
            dest = self.new_temp()
            self.emit(block, Instruction("const", dest, [Imm(expr.value)],
                                         span=expr.span))
            return dest
        if isinstance(expr, ast.BoolLit):
            dest = self.new_temp()
            self.emit(block, Instruction("const", dest, [Imm(int(expr.value))],
                                         span=expr.span))
            return dest
        if isinstance(expr, ast.Var):
            self.names.add(expr.name)
            return expr.name
        if isinstance(expr, ast.Unary):
            v = self.lower_expr(expr.value, block)
            dest = self.new_temp()
            op = "neg" if expr.op == "-" else "not"
            self.emit(block, Instruction("unary", dest, [v], op, expr.span))
            return dest
        if isinstance(expr, ast.Binary):
            left = self.lower_expr(expr.left, block)
            right = self.lower_expr(expr.right, block)
            dest = self.new_temp()
            op = SYMBOL_TO_OPCODE[expr.op]
            self.emit(block, Instruction("binary", dest, [left, right],
                                         op, expr.span))
            return dest
        raise LangError(f"cannot lower expression {type(expr).__name__}", "ir")  # pragma: no cover

    # -- helpers -----------------------------------------------------------

    @staticmethod
    def _terminate_jmp(block: Block, target: Block) -> Block:
        if block.term.kind == "unreachable":
            block.term = Terminator("jmp", [target.label])
        return target


def build_ir(program: ast.Program) -> IRProgram:
    return IRBuilder().build(program)
