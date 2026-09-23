"""Closure conversion: annotated AST -> explicit-environment stack IR.

Strategy (see :mod:`sclang.ir` for the opcodes):

* the resolver's ``free`` lists become explicit environment vectors;
* a binding is given a *cell* exactly when the resolver marks it boxed
  (a named function, or any local/parameter captured by a nested function).
  The Cell is created once — before the owning block's statements run — and
  is passed by identity, so closures share updates instead of copying
  values and a hoisted closure never snapshots an uninitialized binding;
* when a nested closure is created, each free-variable environment slot is
  filled from either the current frame's local storage (``(0, slot)``) or
  forwarded from the current function's own environment (``(1, index)``),
  preserving Cell identity transitively;
* nested block scopes vanish — every binding is a numbered slot of its
  owning function frame — so the VM is purely stack/frame based and contains
  *no* AST or source-language logic.
"""

from . import ast_nodes as ast
from .errors import CompileError
from .ir import IRFunction, IRModule, OP
from .resolver import Binding, FunctionInfo, ResolutionResult


_OP_FOR_BINOP = {
    "==": OP["EQ"], "!=": OP["NE"],
    "<": OP["LT"], "<=": OP["LE"], ">": OP["GT"], ">=": OP["GE"],
    "+": OP["ADD"], "-": OP["SUB"], "*": OP["MUL"],
    "/": OP["DIV"], "%": OP["MOD"],
}


class ClosureConverter:
    def __init__(self, program: ast.Program, resolution: ResolutionResult):
        self.program = program
        self.res = resolution
        self.module = IRModule()
        # FunctionInfo resolver node -> IRFunction
        self.ir_of: dict[FunctionInfo, IRFunction] = {}
        self.cur_ir: IRFunction | None = None
        self.cur_fi: FunctionInfo | None = None
        self.main_ir: IRFunction | None = None

    # -- scaffolding ------------------------------------------------------

    def convert(self) -> IRModule:
        # 1. pre-create all IR functions. Function ids must be unique even
        # for sibling functions (resolver depth is shared by siblings), so
        # use discovery order. Keep a depth->FunctionInfo map for closures.
        for irid, fi in enumerate(self.res.functions):
            irf = IRFunction(
                id=irid,
                name=fi.name,
                param_count=len(fi.params),
                slots=fi.slots,
                free=[{"name": b.name, "boxed": b.boxed} for b in fi.free],
                params=[{"name": b.name,
                         "span": _span6(b.span)} for b in fi.params],
            )
            self.ir_of[fi] = irf
            self.module.functions.append(irf)
        self.id_of_fi = {fi: self.ir_of[fi].id for fi in self.res.functions}

        # 2. emit code into each function
        for fi in self.res.functions:
            self.cur_fi = fi
            self.cur_ir = self.ir_of[fi]
            if fi.node is self.program:
                self.main_ir = self.cur_ir
                self._emit_main(fi)
            else:
                self._emit_function(fi)
        return self.module

    # -- emit helpers -----------------------------------------------------

    def emit(self, opcode, *operands, span=None):
        self.cur_ir.emit(opcode, *operands, span=span)

    def const(self, value, span=None) -> int:
        idx = self.module.add_const(value)
        self.emit(OP["CONST"], idx, span=span)
        return idx

    def _jump(self, op, span=None) -> int:
        """Emit a jump with a placeholder target; return patch index."""
        self.emit(op, 0, span=span)
        return self.cur_ir.here() - 1

    def _patch(self, code_index: int):
        self.cur_ir.code[code_index] = self.cur_ir.here()

    # -- variable access --------------------------------------------------

    def _free_index(self, b: Binding) -> int:
        for i, fb in enumerate(self.cur_fi.free):
            if fb is b:
                return i
        raise CompileError(  # pragma: no cover - defensive
            f"internal: {b.name!r} not in free list of {self.cur_fi.name}",
            b.span)

    def _emit_read_binding(self, b: Binding, span):
        """Push the value of binding ``b`` in the current function."""
        if b.owner == self.cur_fi.func_id:
            if b.boxed:
                self.emit(OP["LOAD_CELL"], b.slot, span=span)
            else:
                self.emit(OP["LOCAL"], b.slot, span=span)
        else:
            idx = self._free_index(b)
            if b.boxed:
                self.emit(OP["READ_FREE"], idx, span=span)
            else:
                # immutable capture: the env slot holds the raw value
                self.emit(OP["PUSH_FREE"], idx, span=span)

    def _emit_write_binding(self, b: Binding, span):
        """Pop a value into binding ``b``."""
        if b.owner == self.cur_fi.func_id:
            if b.boxed:
                self.emit(OP["STORE_CELL"], b.slot, span=span)
            else:
                self.emit(OP["SET_LOCAL"], b.slot, span=span)
        else:
            idx = self._free_index(b)
            if b.boxed:
                self.emit(OP["WRITE_FREE"], idx, span=span)
            else:
                # Resolver guarantees this never happens (captured+mutated
                # would have been boxed); emit a defensive trap.
                raise CompileError(
                    f"internal: writing non-boxed captured variable {b.name!r}",
                    span)

    # -- function bodies --------------------------------------------------

    def _emit_main(self, fi: FunctionInfo):
        self._emit_prologue_box_params(fi)
        self._emit_block(self.program, hoisted_scopes_ok=True)
        self.emit(OP["NIL"], span=self.program.span)
        self.emit(OP["RET"], span=self.program.span)

    def _emit_function(self, fi: FunctionInfo):
        self._emit_prologue_box_params(fi)
        self._emit_block(fi.node.body, is_body=True)
        self.emit(OP["NIL"], span=fi.node.span)
        self.emit(OP["RET"], span=fi.node.span)

    def _emit_prologue_box_params(self, fi: FunctionInfo):
        """Pre-allocate Cells for every boxed slot in this function frame.

        Boxing is decided by the resolver: named functions and any local or
        parameter captured by a nested function are boxed. Cells are created
        *before* any statement runs so closures built during a block's hoist pass
        capture the very same Cell a later ``let`` initializes — cell
        identity never changes for the lifetime of the frame. Boxed
        parameters additionally need their incoming argument wrapped.
        """
        param_slots = {pb.slot for pb in fi.params}
        for pb in fi.params:
            if pb.boxed:
                self.emit(OP["MAKE_CELL"], pb.slot, span=pb.span)
        seen = set(param_slots)
        for b in self.res.bindings:
            if b.owner == fi.func_id and b.boxed and b.slot not in seen:
                seen.add(b.slot)
                # slot starts as nil -> wrap into Cell(nil)
                self.emit(OP["MAKE_CELL"], b.slot, span=b.span)

    def _emit_block(self, block_node, is_body=False, hoisted_scopes_ok=False):
        # Hoisted named functions: create each closure in source order and
        # store it into its (pre-cell) function slot.
        for stmt in block_node.body:
            if isinstance(stmt, ast.FunctionStmt):
                self._emit_make_closure(stmt.res, span=stmt.span)
                nb = stmt.res.name_binding
                self.emit(OP["STORE_CELL"], nb.slot, span=stmt.name_span)
        for stmt in block_node.body:
            if isinstance(stmt, ast.FunctionStmt):
                continue
            self._stmt(stmt)

    # -- statements -------------------------------------------------------

    def _stmt(self, node: ast.Stmt):
        if isinstance(node, ast.Let):
            b: Binding = node.res
            if node.init is not None:
                self._expr(node.init)
            else:
                self.emit(OP["NIL"], span=node.span)
            if b.boxed:
                # the Cell was pre-allocated in the frame prologue; write
                # through it so any closure already capturing it observes
                # the initializer
                self.emit(OP["STORE_CELL"], b.slot, span=node.span)
            else:
                self.emit(OP["SET_LOCAL"], b.slot, span=node.span)
        elif isinstance(node, ast.Block):
            self._emit_block(node)
        elif isinstance(node, ast.If):
            self._expr(node.cond)
            else_jump = self._jump(OP["JIF_FALSE"], span=node.span)
            self._emit_block(node.then)
            end_jump = None
            if node.otherwise is not None:
                end_jump = self._jump(OP["JMP"], span=node.span)
            self._patch(else_jump)
            if node.otherwise is not None:
                self._emit_block(node.otherwise)
                self._patch(end_jump)
        elif isinstance(node, ast.While):
            top = self.cur_ir.here()
            self._expr(node.cond)
            exit_jump = self._jump(OP["JIF_FALSE"], span=node.span)
            self._emit_block(node.body)
            self.emit(OP["JMP"], top, span=node.span)
            self._patch(exit_jump)
        elif isinstance(node, ast.Return):
            if node.value is not None:
                self._expr(node.value)
            else:
                self.emit(OP["NIL"], span=node.span)
            self.emit(OP["RET"], span=node.span)
        elif isinstance(node, ast.ExprStmt):
            self._expr(node.expr)
            self.emit(OP["POP"], span=node.span)
        else:  # pragma: no cover
            raise AssertionError(f"unknown stmt {type(node)}")

    # -- expressions ------------------------------------------------------

    def _expr(self, node: ast.Expr):
        if isinstance(node, ast.IntLit):
            self.const(node.value, span=node.span)
        elif isinstance(node, ast.StrLit):
            self.const(node.value, span=node.span)
        elif isinstance(node, ast.BoolLit):
            self.emit(OP["TRUE"] if node.value else OP["FALSE"],
                      span=node.span)
        elif isinstance(node, ast.NilLit):
            self.emit(OP["NIL"], span=node.span)
        elif isinstance(node, ast.Var):
            self._emit_read_binding(node.res, node.span)
        elif isinstance(node, ast.Assign):
            self._expr(node.value)
            self._emit_write_binding(node.res, node.span)
            # assignment is an expression yielding the assigned value
            self._emit_read_binding(node.res, node.span)
        elif isinstance(node, ast.Unary):
            self._expr(node.operand)
            self.emit(OP["NEG"] if node.op == "-" else OP["NOT"],
                      span=node.op_span)
        elif isinstance(node, ast.Binary):
            self._binary(node)
        elif isinstance(node, ast.Call):
            self._expr(node.callee)
            for a in node.args:
                self._expr(a)
            self.emit(OP["CALL"], len(node.args), span=node.span)
        elif isinstance(node, ast.FunExpr):
            self._emit_make_closure(node.res, span=node.span)
        elif isinstance(node, ast.PrintExpr):
            for a in node.args:
                self._expr(a)
            self.emit(OP["PRINT"], len(node.args), span=node.span)
        else:  # pragma: no cover
            raise AssertionError(f"unknown expr {type(node)}")

    def _binary(self, node: ast.Binary):
        if node.op == "and":
            # left on stack; if falsy keep it as the result, else replace it
            self._expr(node.left)
            self.emit(OP["DUP"], span=node.op_span)
            take_left = self._jump(OP["JIF_FALSE"], span=node.op_span)
            self.emit(OP["POP"], span=node.op_span)
            self._expr(node.right)
            self._patch(take_left)
            return
        if node.op == "or":
            # left on stack; if truthy keep it, else pop and evaluate right
            self._expr(node.left)
            self.emit(OP["DUP"], span=node.op_span)
            take_right = self._jump(OP["JIF_FALSE"], span=node.op_span)
            self.emit(OP["JMP"], 0, span=node.op_span)
            take_left = self.cur_ir.here() - 1
            self._patch(take_right)
            self.emit(OP["POP"], span=node.op_span)
            self._expr(node.right)
            self._patch(take_left)
            return
        self._expr(node.left)
        self._expr(node.right)
        self.emit(_OP_FOR_BINOP[node.op], span=node.op_span)

    # -- closure construction --------------------------------------------

    def _emit_make_closure(self, fi: FunctionInfo, span):
        """Emit MAKE_CLOSURE with an environment vector for ``fi``.

        For every free binding of ``fi``, describe where the *raw* storage
        lives in the currently-executing (parent) function:

        * ``(0, slot)`` — it's one of the current frame's local slots;
        * ``(1, index)`` — it's forwarded from the current function's own
          free environment at ``index`` (transitive capture).
        """
        entries: list[tuple[int, int]] = []
        for b in fi.free:
            if b.owner == self.cur_fi.func_id:
                entries.append((0, b.slot))
            else:
                entries.append((1, self._free_index(b)))
        self.emit(OP["MAKE_CLOSURE"], self.id_of_fi[fi], len(entries),
                  span=span)
        for kind, idx in entries:
            self.cur_ir.code.append(kind)
            self.cur_ir.code.append(idx)
            self.cur_ir.span.append(_span6(span))
            self.cur_ir.span.append(_span6(span))


def _span6(span) -> list[int]:
    if span is None:
        return [0, 0, 0, 0, 0, 0]
    return [span.start, span.end, span.line, span.col,
            span.end_line, span.end_col]


def convert_program(program: ast.Program,
                    resolution: ResolutionResult) -> IRModule:
    return ClosureConverter(program, resolution).convert()
