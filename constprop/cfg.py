"""AST -> 可化简 CFG（pre-SSA）。

生成的 IR 里源变量可以被多次定值；后续 :mod:`constprop.ssa` 负责插入
φ 节点并把它转成 SSA。短路逻辑运算符 ``&&`` / ``||`` 在此阶段就被
显式降低为条件分支，结果在 join 块汇合（结果为 0/1）。
"""

from __future__ import annotations

from . import ast as A
from .model import CFG, Inst, Operand
from .source import SourceText


class CFGBuilder:
    def __init__(self, source: SourceText):
        self.src = source
        self.cfg = CFG()
        self.entry = self.cfg.new_block("entry")
        self.cur = self.entry
        self._tmp = 0
        self._label = 0

    def fresh_tmp(self) -> str:
        self._tmp += 1
        return f"$t{self._tmp}"

    def fresh_join_tmp(self) -> str:
        """跨分支合流的临时值（短路逻辑结果）：需要参与 φ 插入。"""
        self._tmp += 1
        return f"$j{self._tmp}"

    def fresh_label(self, hint: str) -> str:
        self._label += 1
        return f"{hint}.{self._label}"

    def block(self, hint: str):
        return self.cfg.new_block(self.fresh_label(hint))

    def emit(self, inst: Inst):
        assert self.cur.terminator is None, "emitting into terminated block"
        self.cur.insts.append(inst)
        return inst

    def switch(self, b) -> None:
        self.cur = b

    def jump(self, dst: str) -> None:
        if self.cur.terminator is None:
            self.cur.insts.append(Inst("jmp", blocks=[dst]))
            self.cfg.add_edge(self.cur.label, dst)

    # ---------- 程序 / 语句 ----------

    def build(self, program: A.Program) -> CFG:
        for stmt in program.body:
            self.compile_stmt(stmt)
        self.emit(Inst("exit"))
        return self.cfg

    def compile_stmt(self, stmt: A.Stmt) -> None:
        if isinstance(stmt, A.Block):
            for s in stmt.body:
                self.compile_stmt(s)
        elif isinstance(stmt, A.Assign):
            val = self.compile_expr(stmt.value)
            self.emit(Inst("copy", target=stmt.name, operands=[val],
                           span=stmt.span, debug_name=stmt.name))
        elif isinstance(stmt, A.Print):
            val = self.compile_expr(stmt.value)
            self.emit(Inst("print", operands=[val], span=stmt.span))
        elif isinstance(stmt, A.If):
            self.compile_if(stmt)
        elif isinstance(stmt, A.While):
            self.compile_while(stmt)
        else:  # pragma: no cover - 解析器不会产生别的语句
            raise AssertionError(f"unknown statement {type(stmt)}")

    def compile_if(self, stmt: A.If) -> None:
        cond = self.compile_expr(stmt.cond)
        then_b = self.block("then")
        else_b = self.block("else")
        end_b = self.block("endif")
        self.emit(Inst("br", operands=[cond],
                       blocks=[then_b.label, else_b.label],
                       span=stmt.span))
        self.cfg.add_edge(self.cur.label, then_b.label)
        self.cfg.add_edge(self.cur.label, else_b.label)

        self.switch(then_b)
        self.compile_stmt(stmt.then)
        self.jump(end_b.label)

        self.switch(else_b)
        if stmt.otherwise is not None:
            self.compile_stmt(stmt.otherwise)
        self.jump(end_b.label)

        self.switch(end_b)

    def compile_while(self, stmt: A.While) -> None:
        head_b = self.block("while.cond")
        body_b = self.block("while.body")
        end_b = self.block("while.end")
        # 前驱跳入 head
        self.jump(head_b.label)

        self.switch(head_b)
        cond = self.compile_expr(stmt.cond)
        self.emit(Inst("br", operands=[cond],
                       blocks=[body_b.label, end_b.label],
                       span=stmt.span))
        self.cfg.add_edge(head_b.label, body_b.label)
        self.cfg.add_edge(head_b.label, end_b.label)

        self.switch(body_b)
        self.compile_stmt(stmt.body)
        self.jump(head_b.label)

        self.switch(end_b)

    # ---------- 表达式 ----------

    def compile_expr(self, expr: A.Expr) -> Operand:
        if isinstance(expr, A.IntLit):
            return expr.value
        if isinstance(expr, A.BoolLit):
            return int(expr.value)
        if isinstance(expr, A.Var):
            # 直接引用变量名；SSA 重命名阶段解析为具体版本
            return expr.name
        if isinstance(expr, A.Unary):
            a = self.compile_expr(expr.operand)
            tmp = self.fresh_tmp()
            self.emit(Inst(expr.op, target=tmp, operands=[a],
                           span=expr.span, debug_name=tmp))
            return tmp
        if isinstance(expr, A.Binary):
            a = self.compile_expr(expr.left)
            b = self.compile_expr(expr.right)
            tmp = self.fresh_tmp()
            self.emit(Inst(expr.op, target=tmp, operands=[a, b],
                           span=expr.span, debug_name=tmp))
            return tmp
        if isinstance(expr, A.Logical):
            return self.compile_logical(expr)
        raise AssertionError(f"unknown expression {type(expr)}")  # pragma: no cover

    def compile_logical(self, expr: A.Logical) -> Operand:
        """降低短路逻辑（结果统一为 0/1）::

            r = a && b   ->  br a, L.eval, L.false
                             L.eval: <b>; r = (b != 0); jmp L.join
                             L.false: lit r = 0;        jmp L.join
            r = a || b   ->  br a, L.true, L.eval
                             L.true:  lit r = 1;        jmp L.join
                             L.eval: <b>; r = (b != 0); jmp L.join

        左值 a 决定控制流（undef 左值由 br 观察点报错）；右值 b 在被
        求值时若为 undef，则 ``!= 0`` 结果也是 undef（惰性传播，不报错）。
        """
        result = self.fresh_join_tmp()
        a = self.compile_expr(expr.left)

        eval_b = self.block("log.eval")
        short = self.block("log.short")
        join = self.block("log.join")

        if expr.op == "&&":
            taken, not_taken = eval_b, short
            short_val = 0
        else:
            taken, not_taken = short, eval_b
            short_val = 1

        self.emit(Inst("br", operands=[a],
                       blocks=[taken.label, not_taken.label],
                       span=expr.span))
        self.cfg.add_edge(self.cur.label, taken.label)
        self.cfg.add_edge(self.cur.label, not_taken.label)

        self.switch(eval_b)
        b = self.compile_expr(expr.right)
        # 规范化为 0/1：b != 0（undef 沿比较传播为 undef）
        norm = self.fresh_tmp()
        self.emit(Inst("!=", target=norm, operands=[b, 0],
                       span=expr.span, debug_name=norm))
        self.emit(Inst("copy", target=result, operands=[norm],
                       span=expr.span, debug_name=result))
        self.jump(join.label)

        self.switch(short)
        self.emit(Inst("lit", target=result, operands=[short_val],
                       span=expr.span, debug_name=result))
        self.jump(join.label)

        self.switch(join)
        return result


def build_cfg(program: A.Program, source: SourceText) -> CFG:
    return CFGBuilder(source).build(program)
