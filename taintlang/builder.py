"""AST -> IR lowering.

The lowerer turns the statement-oriented AST into the block/register IR
defined in :mod:`taintlang.ir`.  The one non-trivial lowering decision:

*Any* call inside an expression (including nested calls such as
``sink(wrapper(source()))``) forces block splitting: operands are evaluated
in order, a fresh continuation block is created, and the user-function call
becomes a :class:`~taintlang.ir.CallTerm` terminator whose continuation is
that fresh block.  This makes interprocedural edges explicit in the CFG.

Marker calls (source/sanitize/sink) do not cross a function boundary and are
emitted inline as dedicated instructions.
"""

from __future__ import annotations

from typing import Optional

from . import ast_nodes as ast
from .config import Config
from .errors import BuildError
from .ir import (
    BinOp, Block, Br, CallTerm, Const, Copy, FunctionIR, Jmp, ProgramIR, Ret,
    SinkInstr, SanitizeInstr, SourceInstr, UnOp, UnknownCall,
)
from .location import Span


class _FuncLowerer:
    def __init__(self, func: ast.FuncDecl, config: Config,
                 known_funcs: set[str], site_alloc, instr_alloc):
        self.func = func
        self.config = config
        self.known = known_funcs
        self.alloc_site = site_alloc
        self.alloc_instr = instr_alloc
        self.temp_count = 0
        self.label_count = 0
        self.declared: set[str] = set(func.params)
        self.blocks: list[Block] = []
        self.current: Optional[Block] = None
        self.call_sites: list = []

    # -- helpers --------------------------------------------------------
    def _new_label(self, hint: str) -> str:
        label = f"{self.func.name}.{hint}{self.label_count}"
        self.label_count += 1
        return label

    def _new_temp(self) -> str:
        t = f"%{self.temp_count}"
        self.temp_count += 1
        return t

    def _new_instr_uid(self) -> int:
        return self.alloc_instr()

    def _new_block(self, span: Span, label: Optional[str] = None) -> Block:
        if label is None:
            label = self._new_label("b")
        block = Block(label, span)
        self.blocks.append(block)
        self.current = block
        return block

    def _terminated(self) -> bool:
        return self.current is None or self.current.terminator is not None

    def _emit(self, instr) -> None:
        if self._terminated():
            # statements following a terminator are unreachable; create a
            # fresh (dead) block so they still get lowered deterministically
            self._new_block(instr.span)
        self.current.instructions.append(instr)

    def _require_var(self, name: str, span: Span) -> str:
        if name not in self.declared:
            raise BuildError(f"variable '{name}' used before declaration", span)
        return name

    # -- entry point ----------------------------------------------------
    def lower(self) -> FunctionIR:
        entry = self._new_block(self.func.span, f"{self.func.name}.entry")
        self.lower_block(self.func.body)
        if not self._terminated():
            # implicit "return;" at end of body
            self.current.terminator = Ret(self.func.body.span, None)
        return FunctionIR(
            name=self.func.name,
            span=self.func.span,
            params=self.func.params,
            entry=entry.label,
            blocks=tuple(self.blocks),
        )

    # -- statements -----------------------------------------------------
    def lower_block(self, block: ast.Block) -> None:
        for stmt in block.statements:
            self.lower_stmt(stmt)

    def lower_stmt(self, stmt: ast.Stmt) -> None:
        if isinstance(stmt, ast.VarDecl):
            if stmt.name in self.declared:
                raise BuildError(f"variable '{stmt.name}' already declared", stmt.span)
            self.declared.add(stmt.name)
            if stmt.init is not None:
                reg, _ = self.lower_expr(stmt.init)
                self._emit(Copy(self._new_instr_uid(), stmt.span, stmt.name, reg))
            return

        if isinstance(stmt, ast.Assign):
            reg, _ = self.lower_expr(stmt.value)
            self._require_var(stmt.name, stmt.span)
            self._emit(Copy(self._new_instr_uid(), stmt.span, stmt.name, reg))
            return

        if isinstance(stmt, ast.ExprStmt):
            self.lower_expr(stmt.expr)
            return

        if isinstance(stmt, ast.ReturnStmt):
            value = None
            if stmt.value is not None:
                value, _ = self.lower_expr(stmt.value)
            block_to_term = self.current
            if self._terminated():
                block_to_term = self._new_block(stmt.span)
            block_to_term.terminator = Ret(stmt.span, value)
            self.current = None
            return

        if isinstance(stmt, ast.IfStmt):
            return self.lower_if(stmt)

        if isinstance(stmt, ast.WhileStmt):
            return self.lower_while(stmt)

        raise BuildError(f"unsupported statement {type(stmt).__name__}", stmt.span)

    def lower_if(self, stmt: ast.IfStmt) -> None:
        cond, _ = self.lower_expr(stmt.cond)
        then_lbl = self._new_label("then")
        else_lbl = self._new_label("else")
        end_lbl = self._new_label("endif")
        if self._terminated():
            self._new_block(stmt.span)
        self.current.terminator = Br(stmt.span, cond, then_lbl, else_lbl)

        self._new_block(stmt.then_block.span, then_lbl)
        self.lower_block(stmt.then_block)
        if not self._terminated():
            self.current.terminator = Jmp(stmt.span, end_lbl)

        self._new_block((stmt.else_block or stmt.then_block).span, else_lbl)
        if stmt.else_block is not None:
            self.lower_block(stmt.else_block)
        if not self._terminated():
            self.current.terminator = Jmp(stmt.span, end_lbl)

        self._new_block(stmt.span, end_lbl)

    def lower_while(self, stmt: ast.WhileStmt) -> None:
        head_lbl = self._new_label("whilehead")
        body_lbl = self._new_label("whilebody")
        end_lbl = self._new_label("whileend")
        if not self._terminated():
            self.current.terminator = Jmp(stmt.span, head_lbl)
        self._new_block(stmt.span, head_lbl)
        cond, _ = self.lower_expr(stmt.cond)
        self.current.terminator = Br(stmt.span, cond, body_lbl, end_lbl)

        self._new_block(stmt.body.span, body_lbl)
        self.lower_block(stmt.body)
        if not self._terminated():
            self.current.terminator = Jmp(stmt.span, head_lbl)

        self._new_block(stmt.span, end_lbl)

    # -- expressions ----------------------------------------------------
    def lower_expr(self, expr: ast.Expr) -> tuple[str, Optional[Block]]:
        """Return (register holding value, final continuation block).

        The continuation block is non-None only when evaluation ended on a
        user-function call terminator; subsequent code is emitted there.
        """
        if isinstance(expr, ast.NumberLit):
            reg = self._new_temp()
            self._emit(Const(self._new_instr_uid(), expr.span, reg, expr.value))
            return reg, None

        if isinstance(expr, ast.StringLit):
            reg = self._new_temp()
            self._emit(Const(self._new_instr_uid(), expr.span, reg, expr.raw))
            return reg, None

        if isinstance(expr, ast.BoolLit):
            reg = self._new_temp()
            self._emit(Const(self._new_instr_uid(), expr.span, reg,
                             "true" if expr.value else "false"))
            return reg, None

        if isinstance(expr, ast.VarRef):
            return self._require_var(expr.name, expr.span), None

        if isinstance(expr, ast.Unary):
            operand, cont = self.lower_expr(expr.operand)
            reg = self._new_temp()
            self._emit(UnOp(self._new_instr_uid(), expr.span, reg, expr.op, operand))
            return reg, cont

        if isinstance(expr, ast.Binary):
            left, _ = self.lower_expr(expr.left)
            right, cont = self.lower_expr(expr.right)
            reg = self._new_temp()
            self._emit(BinOp(self._new_instr_uid(), expr.span, reg, expr.op, left, right))
            return reg, cont

        if isinstance(expr, ast.Call):
            return self.lower_call(expr)

        raise BuildError(f"unsupported expression {type(expr).__name__}", expr.span)

    def _lower_args(self, call: ast.Call) -> list[str]:
        regs: list[str] = []
        for arg in call.args:
            reg, _ = self.lower_expr(arg)
            regs.append(reg)
        return regs

    def lower_call(self, call: ast.Call) -> tuple[str, Optional[Block]]:
        name = call.callee

        if name in self.config.sources:
            if call.args:
                raise BuildError(f"source marker '{name}' takes no arguments", call.span)
            reg = self._new_temp()
            self._emit(SourceInstr(self._new_instr_uid(), call.span, reg, name))
            return reg, None

        if name in self.config.sanitizers:
            if len(call.args) != 1:
                raise BuildError(f"sanitizer '{name}' takes exactly one argument", call.span)
            (arg,) = self._lower_args(call)
            reg = self._new_temp()
            self._emit(SanitizeInstr(self._new_instr_uid(), call.span, reg, arg, name))
            return reg, None

        if name in self.config.sinks:
            if len(call.args) != 1:
                raise BuildError(f"sink '{name}' takes exactly one argument", call.span)
            (arg,) = self._lower_args(call)
            self._emit(SinkInstr(self._new_instr_uid(), call.span, arg, name))
            # expression value is an untainted placeholder
            reg = self._new_temp()
            self._emit(Const(self._new_instr_uid(), call.span, reg, "0"))
            return reg, None

        if name in self.known:
            return self._lower_user_call(call, name)

        return self._lower_unknown_call(call, name)

    def _lower_user_call(self, call: ast.Call, name: str) -> tuple[str, Optional[Block]]:
        args = self._lower_args(call)
        cont_label = self._new_label("cont")
        span = call.span
        if self._terminated():
            self._new_block(span)
        result_reg = self._new_temp()
        site_uid = self.alloc_site()
        self.call_sites.append((site_uid, self.func.name, name, span))
        self.current.terminator = CallTerm(
            span=span, callee=name, args=tuple(args),
            return_reg=result_reg, cont=cont_label, site_uid=site_uid,
        )
        cont_block = self._new_block(span, cont_label)
        return result_reg, cont_block

    def _lower_unknown_call(self, call: ast.Call, name: str) -> tuple[str, Optional[Block]]:
        args = self._lower_args(call)
        reg = self._new_temp()
        self._emit(UnknownCall(self._new_instr_uid(), call.span, reg, name, tuple(args)))
        return reg, None


def build(program: ast.Program, config: Config, source: str) -> ProgramIR:
    names = [f.name for f in program.functions]
    duplicates = {n for n in names if names.count(n) > 1}
    if duplicates:
        first = next(f for f in program.functions if f.name in duplicates)
        raise BuildError(f"duplicate function name {sorted(duplicates)[0]!r}", first.span)

    builtin_conflicts = set(names) & set(config.builtins)
    if builtin_conflicts:
        f = next(f for f in program.functions if f.name in builtin_conflicts)
        raise BuildError(
            f"function '{f.name}' shadows a built-in marker name", f.span)

    known = set(names)
    funcs: list[FunctionIR] = []
    call_sites: list = []
    site_counter = [0]
    instr_counter = [0]

    def alloc_site() -> int:
        sid = site_counter[0]
        site_counter[0] += 1
        return sid

    def alloc_instr() -> int:
        uid = instr_counter[0]
        instr_counter[0] += 1
        return uid

    arity = {f.name: len(f.params) for f in program.functions}
    for func in program.functions:
        lowerer = _FuncLowerer(func, config, known, alloc_site, alloc_instr)
        ir_func = lowerer.lower()
        funcs.append(ir_func)
        call_sites.extend(lowerer.call_sites)

    # global arity check (caller and callee now both known)
    for site_uid, caller, callee, span in call_sites:
        if arity.get(callee) is not None:
            n_args = None
            for fn in funcs:
                for b in fn.blocks:
                    t = b.terminator
                    if isinstance(t, CallTerm) and t.site_uid == site_uid:
                        n_args = len(t.args)
            if n_args != arity[callee]:
                raise BuildError(
                    f"call to '{callee}' passes {n_args} argument(s) "
                    f"but it expects {arity[callee]}", span)

    program_ir = ProgramIR(
        functions=tuple(funcs),
        call_sites=tuple(call_sites),
        source=source,
    )
    _fill_span_text(program_ir, source)
    return program_ir


def _fill_span_text(program_ir: ProgramIR, source: str) -> None:
    """Replace every IR span with an equal-position span whose ``text`` is
    the actual covered source slice (positions are retained exactly)."""
    import dataclasses
    from .location import make_span

    def fix_span(span):
        return make_span(source, span.start.offset, span.end.offset)

    new_funcs = []
    for func in program_ir.functions:
        new_blocks = []
        for block in func.blocks:
            new_instrs = [dataclasses.replace(i, span=fix_span(i.span))
                          for i in block.instructions]
            term = block.terminator
            new_term = dataclasses.replace(term, span=fix_span(term.span))
            new_blocks.append(dataclasses.replace(
                block, span=fix_span(block.span),
                instructions=new_instrs, terminator=new_term))
        new_funcs.append(dataclasses.replace(
            func, span=fix_span(func.span), blocks=tuple(new_blocks)))
    new_sites = tuple((s, c, e, fix_span(sp))
                      for s, c, e, sp in program_ir.call_sites)
    object.__setattr__(program_ir, "functions", tuple(new_funcs))
    object.__setattr__(program_ir, "call_sites", new_sites)
    object.__setattr__(program_ir, "_by_name",
                       {f.name: f for f in new_funcs})
    for f in new_funcs:
        object.__setattr__(f, "_by_label", {b.label: b for b in f.blocks})
