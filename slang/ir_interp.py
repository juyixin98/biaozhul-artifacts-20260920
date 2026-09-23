"""Independent interpreter for the closure-converted IR.

This module never looks at the AST or analyzer: it executes only the IR in
:mod:`slang.ir_nodes`.  That separation is what makes the end-to-end test
meaningful — lexing, parsing, scope analysis and closure conversion are
exercised before this interpreter ever runs, and it shares NO code with the
source interpreter (:mod:`slang.source_interp`) except the value helpers.

Runtime representation:

* a :class:`Cell` is a one-slot heap box shared by every closure that captured
  it;
* an :class:`IClosure` pairs a lifted function with its explicit free vector
  (a list of Cells);
* local slots are private to each activation record.
"""

from __future__ import annotations

from dataclasses import dataclass, field

from . import ir_nodes as ir
from .errors import RuntimeErr
from .values import eval_binary, eval_unary, format_value, is_truthy

_UNSET = object()


class Cell:
    __slots__ = ("value",)
    slang_type = "cell"

    def __init__(self):
        self.value = _UNSET


@dataclass
class IClosure:
    fid: str
    name: str
    free: list
    slang_type: str = "closure"

    @property
    def display_name(self):
        return self.name


@dataclass
class Builtin:
    name: str
    fn: object
    slang_type: str = "builtin"


@dataclass
class Frame:
    fn: ir.IFunc
    locals: list = field(default_factory=list)
    free: list = field(default_factory=list)   # cells from closure


class _Return(Exception):
    def __init__(self, value):
        self.value = value


class IRInterpreter:
    def __init__(self, module: ir.IModule, trace: list | None = None):
        self.module = module
        self.funcs = {f.id: f for f in module.funcs}
        self.funcs[module.main.id] = module.main
        self.trace = trace if trace is not None else []
        self.builtins = {"print": Builtin("print", self._builtin_print)}

    # --------------------------------------------------------------- run

    def run(self):
        self._invoke_main()
        return self.trace

    def _invoke_main(self):
        main = self.module.main
        locals_ = [_UNSET] * main.nlocals
        frame = Frame(main, locals=locals_, free=[])
        self.eval_instrs(main.prologue, frame)
        try:
            self.exec_block(main.body, frame)
        except _Return:
            # A top-level return simply ends the program; its value is dropped.
            pass

    def _builtin_print(self, args):
        if len(args) != 1:
            raise RuntimeErr(f"print() expects 1 argument, got {len(args)}")
        line = format_value(args[0])
        self.trace.append(line)
        return None

    # ------------------------------------------------- statement execution

    def exec_block(self, block: ir.IBlock, frame: Frame, dead_slots=()):
        for slot in dead_slots:
            frame.locals[slot] = _UNSET
        for s in block.stmts:
            self.exec_stmt(s, frame)

    def exec_stmt(self, s, frame: Frame):
        k = s.__class__.__name__
        if k == "IBlock":
            # The block() lowering prepends DEAD statements for its own direct
            # let slots; executing them in order performs the reset (this also
            # re-runs on every loop iteration).
            self.exec_block(s, frame)
        elif k == "IExprStmt":
            self.eval_instrs(s.instrs, frame)
        elif k == "IReturn":
            if s.instrs is None:
                raise _Return(None)
            value = self.eval_instrs(s.instrs, frame)
            raise _Return(value)
        elif k == "IIf":
            cond = self.eval_instrs(s.cond, frame)
            if is_truthy(cond):
                self.exec_block(s.then, frame)
            elif s.otherwise is not None:
                self.exec_block(s.otherwise, frame)
        elif k == "IWhile":
            while True:
                cond = self.eval_instrs(s.cond, frame)
                if not is_truthy(cond):
                    break
                self.exec_block(s.body, frame)
        else:  # pragma: no cover
            raise RuntimeErr(f"internal: unknown IR statement {k}")

    # ---------------------------------------------- expression (stack vm)

    def eval_instrs(self, instrs, frame: Frame):
        stack: list = []
        pc = 0
        # Jumps target labels inside the SAME instruction list.
        labels = {ins.arg: i for i, ins in enumerate(instrs) if ins.op == "LABEL"}
        while pc < len(instrs):
            ins = instrs[pc]
            op = ins.op
            if op == "CONST":
                stack.append(self.module.constants[ins.arg])
            elif op == "GET_LOCAL":
                v = frame.locals[ins.arg]
                if v is _UNSET:
                    raise RuntimeErr("cannot read a local variable before it is initialized", ins.loc)
                stack.append(v)
            elif op == "SET_LOCAL":
                frame.locals[ins.arg] = stack.pop()
            elif op == "BOX":
                raw = frame.locals[ins.arg]
                if raw is _UNSET:
                    raise RuntimeErr("cannot box an uninitialized parameter", ins.loc)  # pragma: no cover
                cell = Cell()
                cell.value = raw
                frame.locals[ins.arg] = cell
            elif op == "NEW_CELL":
                frame.locals[ins.arg] = Cell()
            elif op == "DEAD":
                frame.locals[ins.arg] = _UNSET
            elif op == "FRESH_CELL":
                stack.append(Cell())
            elif op == "CELL_DEREF":
                cell = stack.pop()
                if cell.value is _UNSET:
                    raise RuntimeErr("cannot read a variable before it is initialized", ins.loc)
                stack.append(cell.value)
            elif op == "CELL_SET":
                value = stack.pop()
                cell = stack.pop()
                cell.value = value
                stack.append(value)
            elif op == "SWAP":
                stack[-1], stack[-2] = stack[-2], stack[-1]
            elif op == "GET_FREE":
                stack.append(frame.free[ins.arg])
            elif op == "GET_BUILTIN":
                stack.append(self.builtins[ins.arg])
            elif op == "MAKE_CLOSURE":
                fid = ins.arg
                n = len(self.funcs[fid].captures)
                cells = stack[len(stack) - n :] if n else []
                del stack[len(stack) - n :]
                stack.append(IClosure(fid, self.funcs[fid].name, cells))
            elif op == "MAKE_CLOSURE_SELF":
                fid = ins.arg
                fdef = self.funcs[fid]
                # Parent pushed one cell per ordinary capture, plus the fresh
                # self cell last.  The child receives the self cell in both its
                # free vector and its reserved self local slot.
                n = len(fdef.captures) - 1
                popped = list(stack[len(stack) - (n + 1) :])
                del stack[len(stack) - (n + 1) :]
                cells = popped
                self_cell = popped[-1]
                closure = IClosure(fid, fdef.name, cells)
                self_cell.value = closure
                stack.append(closure)
            elif op == "CALL":
                nargs = ins.arg
                args = stack[len(stack) - nargs :]
                del stack[len(stack) - nargs :]
                callee = stack.pop()
                stack.append(self.call_value(callee, args, ins.loc))
            elif op == "UNARY":
                stack.append(eval_unary(ins.arg, stack.pop()))
            elif op == "BINARY":
                b = stack.pop()
                a = stack.pop()
                stack.append(eval_binary(ins.arg, a, b))
            elif op == "POP":
                stack.pop()
            elif op == "JUMP_IF_TRUE" or op == "JUMP_IF_FALSE":
                v = stack[-1]
                t = is_truthy(v)
                if (op == "JUMP_IF_TRUE" and t) or (op == "JUMP_IF_FALSE" and not t):
                    pc = labels[ins.arg]
                    continue
            elif op == "LABEL":
                pass
            else:  # pragma: no cover
                raise RuntimeErr(f"internal: unknown opcode {op}", ins.loc)
            pc += 1
        if not stack:
            return None
        return stack[-1]

    # ------------------------------------------------------------- calls

    def call_value(self, callee, args, loc):
        if isinstance(callee, Builtin):
            return callee.fn(args)
        if not isinstance(callee, IClosure):
            raise RuntimeErr(f"value of type {type_name(callee)} is not callable", loc)
        fdef = self.funcs[callee.fid]
        if len(args) != fdef.nparams:
            raise RuntimeErr(
                f"{fdef.name}() expects {fdef.nparams} arguments but got {len(args)}", loc
            )
        # Local slots start dead; reads before initialization raise.  `let x;`
        # explicitly stores null, and boxed cells begin in their own unset
        # state until the declaration assigns into them.
        frame = Frame(fdef, locals=[_UNSET] * fdef.nlocals, free=list(callee.free))
        for i, a in enumerate(args):
            frame.locals[i] = a
        # The prologue is one sequence: box parameters, reserve cells and
        # construct hoisted function-declaration closures.
        self.eval_instrs(fdef.prologue, frame)
        # A named function expression reads its own name from the dedicated
        # self slot: replace the prologue's empty cell with the real one.
        if fdef.self_slot is not None:
            frame.locals[fdef.self_slot] = callee.free[-1]
        try:
            self.exec_block(fdef.body, frame)
        except _Return as r:
            return r.value
        return None


def type_name(v) -> str:
    t = getattr(v, "slang_type", None)
    return t or type(v).__name__


def run_module(module: ir.IModule, trace: list | None = None) -> list:
    return IRInterpreter(module, trace=trace).run()
