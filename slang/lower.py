"""Closure conversion: AST + scope analysis -> explicit-cell IR.

The input still uses symbolic names; the output (:mod:`slang.ir_nodes`) has
only local slots, heap cells and explicit closure free vectors.

Design:

* Every analyzed function frame becomes a lifted top-level :class:`IFunc`.
* A variable marked ``boxed`` by the analyzer is stored in a :class:`Cell`
  heap object.  Reads become ``CELL_DEREF``, writes become ``CELL_SET``.  All
  closures sharing the variable share the SAME cell, so mutations are visible
  across closures (the key property being demonstrated).
* A nested function no longer reaches into its parent's frame: the parent
  emits code to push the captured cells and ``MAKE_CLOSURE`` binds them into
  the new closure's free vector.
* Captures that transitively pass through an intermediate function are stored
  in that function's own free vector ("closure environment slots").
"""

from __future__ import annotations

from . import ast_nodes as ast
from . import ir_nodes as ir
from .analyzer import Analyzer, AnalysisResult, FunctionFrame, analyze
from .errors import CompileError

UNINIT = object()


class Lower:
    def __init__(self, program: ast.Program, result: AnalysisResult):
        self.program = program
        self.res = result
        self.constants: list = []
        self.const_index: dict = {}
        self.emitted: dict[str, ir.IFunc] = {}
        self.order: list[str] = []
        self.pending_emit: list = []

    # ------------------------------------------------------------ constants

    def c_const(self, value) -> int:
        # Deduplicate immutable JSON-able literals.  bool is checked before
        # int because ``True == 1`` in Python.
        key = ("b", value) if isinstance(value, bool) else (
            ("n", value) if isinstance(value, int) else ("s", value)
        )
        if key in self.const_index:
            return self.const_index[key]
        idx = len(self.constants)
        self.const_index[key] = idx
        self.constants.append(value)
        return idx

    # ---------------------------------------------------------------- entry

    def lower(self) -> ir.IModule:
        main_frame = self.res.frames["main"]
        main = self.emit_function(main_frame, self.program, self.program.stmts, is_main=True)
        return ir.IModule(funcs=[self.emitted[fid] for fid in self.order], main=main, constants=self.constants)

    # ------------------------------------------------------- function emit

    def emit_function(self, frame: FunctionFrame, node, body_stmts, is_main=False) -> ir.IFunc:
        nparams = sum(1 for v in frame.slots if v.is_param)
        nlocals = len(frame.slots)
        box_slots = sorted(frame.boxed_slots)
        if frame.self_slot is not None:
            box_slots = sorted(set(box_slots + [frame.self_slot]))

        # Prologue: box captured parameters.  Captured ``let`` cells are
        # created by their own declaration statements (so reads before the
        # declaration are errors and loop re-entries get fresh cells).
        prologue: list[ir.Instr] = []
        for slot in box_slots:
            if slot < nparams:
                prologue.append(ir.Instr("BOX", slot))
            # non-parameter boxed slots: nothing here; see let_stmt/NEW_CELL.
        # Function-declaration slots that are captured start as cells so that
        # sibling declarations can reference each other during hoisting.
        for var in frame.slots:
            if var.is_fn and var.boxed:
                prologue.append(ir.Instr("NEW_CELL", var.slot))
        # Named-function-expression self slots receive their cell through the
        # closure vector; still create a placeholder cell for safety.
        if frame.self_slot is not None and frame.self_slot >= nparams:
            if not any(i.op == "NEW_CELL" and i.arg == frame.self_slot for i in prologue):
                prologue.append(ir.Instr("NEW_CELL", frame.self_slot))

        captures = [
            {
                "slot": c.slot,
                "name": c.var.name,
                "owner": c.from_frame,
                "self": bool(c.var.is_self),
            }
            for c in frame.captures
        ]
        # A named function expression's self cell occupies an extra free
        # position at the end of the vector; it is back-patched after
        # MAKE_CLOSURE_SELF.
        if frame.self_slot is not None:
            captures.append({"slot": len(captures), "name": frame.self_name, "owner": frame.id, "self": True})

        b = self.function_body_block(body_stmts, frame)
        fn = ir.IFunc(
            id=frame.id,
            name=frame.name,
            nparams=nparams,
            nlocals=nlocals,
            box_slots=box_slots,
            self_slot=frame.self_slot,
            prologue=prologue,
            captures=captures,
            body=b,
            loc=frame.loc,
        )
        self.emitted[frame.id] = fn
        # Lift functions created from FnExpr expressions within this body.
        # Hoisted FnDecl children register themselves as their statements are
        # lowered in block() above.
        pending = self.pending_emit
        self.pending_emit = []
        for child in pending:
            if child.id not in self.emitted:
                self.emit_function(child, child.node, child.node.body.stmts)
        if not is_main:
            self.order.append(frame.id)
        return fn

    # ----------------------------------------------------------- statements

    def function_body_block(self, stmts, frame: FunctionFrame) -> ir.IBlock:
        """Lower a function body's statement list.

        Unlike nested source blocks, the body does NOT reset its own direct
        let slots on entry: the prologue reserved/boxed those slots and each
        declaration initializes them exactly once.
        """
        out = ir.IBlock()
        for s in stmts:
            self.stmt(s, frame, out)
        return out

    def block(self, stmts, frame: FunctionFrame, is_top=False) -> ir.IBlock:
        out = ir.IBlock()
        for s in stmts:
            self.stmt(s, frame, out, is_top=False)
        return out

    def _block_local_let_slots(self, stmts, frame: FunctionFrame) -> list:
        """Slots of lets declared DIRECTLY in this block (not nested blocks)."""
        return [s._var_info.slot for s in stmts if type(s) is ast.Let]

    def stmt(self, s, frame: FunctionFrame, out: ir.IBlock, is_top=False):
        if isinstance(s, ast.Block):
            out.stmts.append(self.block(s.stmts, frame))
        elif isinstance(s, ast.Let):
            self.let_stmt(s, frame, out)
        elif isinstance(s, ast.FnDecl):
            self.fn_decl(s, frame, out)
        elif isinstance(s, ast.Return):
            instrs = None if s.value is None else self.expr(s.value, frame)
            out.stmts.append(ir.IReturn(instrs))
        elif isinstance(s, ast.If):
            cond = self.expr(s.cond, frame)
            then = self.block(s.then.stmts, frame)
            other = None
            if s.otherwise is not None:
                if isinstance(s.otherwise, ast.If):
                    # else-if: wrap the nested IIf in a block so it executes as
                    # one statement (no new scope is implied at IR level).
                    other = ir.IBlock()
                    self.stmt(s.otherwise, frame, other)
                else:
                    other = self.block(s.otherwise.stmts, frame)
            out.stmts.append(ir.IIf(cond, then, other))
        elif isinstance(s, ast.While):
            cond = self.expr(s.cond, frame)
            body = self.block(s.body.stmts, frame)
            out.stmts.append(ir.IWhile(cond, body))
        elif isinstance(s, ast.Assign):
            instrs = self.assign_instrs(s, frame)
            out.stmts.append(ir.IExprStmt(instrs))
        elif isinstance(s, ast.ExprStmt):
            instrs = self.expr(s.expr, frame)
            instrs = instrs + [ir.Instr("POP", loc=s.loc)]
            out.stmts.append(ir.IExprStmt(instrs))
        else:  # pragma: no cover
            raise CompileError(f"internal: cannot lower {type(s).__name__}", getattr(s, "loc", None))

    def let_stmt(self, s: ast.Let, frame: FunctionFrame, out: ir.IBlock):
        var = s._var_info
        slot = var.slot
        # The slot is function-scoped, but the name is visible only from its
        # declaration onward.  Reset it to DEAD right before initializing so a
        # read before the declaration (including on a later loop iteration) is
        # an error rather than a stale value.  Harmless on first entry.
        if var.boxed:
            out.stmts.append(ir.IExprStmt([ir.Instr("NEW_CELL", slot)]))
        else:
            out.stmts.append(ir.IExprStmt([ir.Instr("DEAD", slot)]))
        if s.init is None:
            # `let x;` means x is explicitly null (not "uninitialized").
            init_instrs = [ir.Instr("CONST", self.c_const(None), loc=s.loc)]
        else:
            init_instrs = self.expr(s.init, frame)
        if var.boxed:
            # The cell was just (re)created; store initial value: [cell, value].
            init_instrs = [ir.Instr("GET_LOCAL", slot, loc=s.loc)] + init_instrs + [
                ir.Instr("CELL_SET", loc=s.loc),
            ]
        else:
            init_instrs = init_instrs + [ir.Instr("SET_LOCAL", slot, loc=s.loc)]
        out.stmts.append(ir.IExprStmt(init_instrs))

    def fn_decl(self, s: ast.FnDecl, frame: FunctionFrame, out: ir.IBlock):
        # Name slot was preallocated (hoisted); the closure itself is built
        # when execution reaches the declaration statement.
        var = s._var_info
        child = self.res.frames[self._frame_id_for_node(s)]
        instrs = self.build_closure(child, frame, s.loc, self_binding=None)
        if var.boxed:
            instrs = instrs + [
                ir.Instr("GET_LOCAL", var.slot, loc=s.loc),
                ir.Instr("SWAP", loc=s.loc),
                ir.Instr("CELL_SET", loc=s.loc),
            ]
        else:
            instrs = instrs + [ir.Instr("SET_LOCAL", var.slot, loc=s.loc)]
        out.stmts.append(ir.IExprStmt(instrs))
        # Lift the nested function body after this construction site.
        if child.id not in self.emitted:
            self.emit_function(child, child.node, child.node.body.stmts)

    # ---------------------------------------------------------- expressions

    def expr(self, e, frame: FunctionFrame) -> list:
        if isinstance(e, ast.IntLit):
            return [ir.Instr("CONST", self.c_const(e.value), loc=e.loc)]
        if isinstance(e, ast.StrLit):
            return [ir.Instr("CONST", self.c_const(e.value), loc=e.loc)]
        if isinstance(e, ast.BoolLit):
            return [ir.Instr("CONST", self.c_const(e.value), loc=e.loc)]
        if isinstance(e, ast.NullLit):
            return [ir.Instr("CONST", self.c_const(None), loc=e.loc)]
        if isinstance(e, ast.Var):
            return self.read_var(e, frame)
        if isinstance(e, ast.Unary):
            return self.expr(e.operand, frame) + [ir.Instr("UNARY", e.op, loc=e.loc)]
        if isinstance(e, ast.Binary):
            # && and || short-circuit: lower through branches.
            if e.op in ("&&", "||"):
                return self.short_circuit(e, frame)
            return self.expr(e.left, frame) + self.expr(e.right, frame) + [
                ir.Instr("BINARY", e.op, loc=e.loc)
            ]
        if isinstance(e, ast.Call):
            return self.call(e, frame)
        if isinstance(e, ast.FnExpr):
            child = self.res.frames[self._frame_id_for_node(e)]
            instrs = self.build_closure(child, frame, e.loc, self_binding=None)
            # Lift the body once, after the enclosing function body has been
            # lowered (so its capture metadata is fully populated).
            if not any(f.id == child.id for f in self.pending_emit):
                self.pending_emit.append(child)
            return instrs
        raise CompileError(f"internal: cannot lower expr {type(e).__name__}", getattr(e, "loc", None))  # pragma: no cover

    def call(self, e: ast.Call, frame: FunctionFrame) -> list:
        # Built-in call fast path keeps the IR free of magic locals.
        if isinstance(e.callee, ast.Var):
            b = self.res.resolved.get(id(e.callee))
            if b is not None and b.kind == "builtin":
                out = [ir.Instr("GET_BUILTIN", b.builtin, loc=e.callee.loc)]
                for a in e.args:
                    out += self.expr(a, frame)
                out.append(ir.Instr("CALL", len(e.args), loc=e.loc))
                return out
        out = self.expr(e.callee, frame)
        for a in e.args:
            out += self.expr(a, frame)
        out.append(ir.Instr("CALL", len(e.args), loc=e.loc))
        return out

    def short_circuit(self, e: ast.Binary, frame: FunctionFrame) -> list:
        # Encode short-circuit on the stack machine with branch instructions
        # understood by the IR interpreter:
        #   <left>; JUMP_IF_TRUE/FALSE end; POP; <right>; LABEL end
        # These pseudo-instructions live only inside an expression list.
        label = f"sc_{id(e)}"
        left = self.expr(e.left, frame)
        right = self.expr(e.right, frame)
        jump = "JUMP_IF_TRUE" if e.op == "||" else "JUMP_IF_FALSE"
        return left + [
            ir.Instr(jump, label, loc=e.loc),
            ir.Instr("POP", loc=e.loc),
        ] + right + [ir.Instr("LABEL", label, loc=e.loc)]

    # ------------------------------------------------------------ variables

    def read_var(self, e: ast.Var, frame: FunctionFrame) -> list:
        b = self.res.resolved[id(e)]
        if b.kind == "builtin":
            return [ir.Instr("GET_BUILTIN", b.builtin, loc=e.loc)]
        var = b.var
        if b.kind == "local":
            slot = var.slot
            if var.boxed or var.is_self:
                return [ir.Instr("GET_LOCAL", slot, loc=e.loc), ir.Instr("CELL_DEREF", loc=e.loc)]
            return [ir.Instr("GET_LOCAL", slot, loc=e.loc)]
        # free: read cell from closure environment, dereference it.
        return [ir.Instr("GET_FREE", b.captured_via, loc=e.loc), ir.Instr("CELL_DEREF", loc=e.loc)]

    def assign_instrs(self, s: ast.Assign, frame: FunctionFrame) -> list:
        b = self.res.resolved[id(s)]
        value = self.expr(s.value, frame)
        if b.kind == "builtin":
            raise CompileError(f"cannot assign to built-in '{s.name}'", s.name_loc)
        var = b.var
        if b.kind == "local" and not (var.boxed or var.is_self):
            return value + [ir.Instr("SET_LOCAL", var.slot, loc=s.loc)]
        # Cell target: stack must be [cell, value] before CELL_SET, so push
        # the cell FIRST and the value second.
        if b.kind == "local":
            push_cell = [ir.Instr("GET_LOCAL", var.slot, loc=s.loc)]
        else:
            push_cell = [ir.Instr("GET_FREE", b.captured_via, loc=s.loc)]
        return push_cell + value + [ir.Instr("CELL_SET", loc=s.loc)]

    # ------------------------------------------------------------ closures

    def build_closure(self, child: FunctionFrame, parent: FunctionFrame, loc, self_binding) -> list:
        """Emit the code run in the PARENT to construct ``child``'s closure."""
        instrs: list[ir.Instr] = []
        # The child's free vector order = child.captures (in slot order),
        # followed by the child's self cell if it is a named function expr.
        for c in child.captures:
            var = c.var
            instrs += self.push_owned_cell(var, parent, loc)
        if child.self_slot is not None:
            # Named function expression: parent allocates a fresh empty cell
            # which MAKE_CLOSURE_SELF ties into a cycle.
            instrs.append(ir.Instr("FRESH_CELL", loc=loc))
        op = "MAKE_CLOSURE_SELF" if child.self_slot is not None else "MAKE_CLOSURE"
        instrs.append(ir.Instr(op, child.id, loc=loc))
        return instrs

    def push_owned_cell(self, var, parent: FunctionFrame, loc) -> list:
        """Push the cell holding ``var`` as visible from ``parent`` frame."""
        owner = var.frame
        if owner is parent:
            # The variable lives in this frame; the slot holds the Cell.
            return [ir.Instr("GET_LOCAL", var.slot, loc=loc)]
        # The variable lives above: find the capture slot in THIS frame that
        # threads it along.
        for c in parent.captures:
            if c.var is var:
                return [ir.Instr("GET_FREE", c.slot, loc=loc)]
        raise CompileError(
            f"internal: frame {parent.id} cannot provide captured variable {var.name}", loc
        )  # pragma: no cover

    # -------------------------------------------------------------- helpers

    def _frame_id_for_node(self, node) -> str:
        for fid, f in self.res.frames.items():
            if f.node is node:
                return fid
        raise CompileError("internal: no frame for function node", getattr(node, "loc", None))  # pragma: no cover


def lower_module(program: ast.Program, result: AnalysisResult | None = None) -> ir.IModule:
    if result is None:
        result = analyze(program)
    return Lower(program, result).lower()
