"""Tree-walking reference interpreter for ScL source programs.

This interpreter runs directly off the AST using resolver metadata,
*before* any closure conversion. It is the **oracle** for the
differential test-suite: the converted program, executed by
:mod:`sclang.vm`, must print identical output on every program.

Runtime model. Two independent structures:

* **block scopes** — a stack of ``name -> Binding`` maps mirroring the
  resolver's lexical scopes. A spelling resolves to exactly one
  :class:`Binding`; shadowing is resolved here, never by name lookup into
  frames.
* **frames** — one per function invocation. A frame owns a flat ``slots``
  list (indexed by ``Binding.slot``) plus a ``free_env`` map of the
  :class:`Binding` objects captured from enclosing invocations (in the
  resolver-computed ``free`` order).

Reads/writes walk the *frame* chain by owning function id and check each
intermediate frame's ``free_env``, so transitive capture works even when an
intermediate function never mentions the variable (sibling functions share
a lexical depth but are distinguished by unique function id). Every binding
captured by a nested function lives in a shared :class:`Cell` created before
the block runs; the same Cell is handed to every closure, so sibling
closures observe each other's writes — exactly the semantics the closure
conversion must reproduce.
"""

from dataclasses import dataclass, field
from typing import Optional

from . import ast_nodes as ast
from .errors import RuntimeError_, Span
from .resolver import Binding, FunctionInfo, ResolutionResult, resolve_program


@dataclass
class Cell:
    value: object = None


@dataclass
class Closure:
    fi: FunctionInfo
    node: object
    free_env: dict[Binding, object]
    name: str


class ReturnSignal(Exception):
    def __init__(self, value):
        self.value = value


@dataclass
class Frame:
    fi: FunctionInfo
    slots: list[object] = field(default_factory=list)
    parent: "Optional[Frame]" = None
    free_env: dict[Binding, object] = field(default_factory=dict)


class Interpreter:
    def __init__(self, program: ast.Program,
                 resolution: Optional[ResolutionResult] = None,
                 capture_output: bool = True):
        self.program = program
        self.resolution = resolution or resolve_program(program)
        self.capture_output = capture_output
        self.output: list[str] = []
        self.frame: Optional[Frame] = None
        self.scopes: list[dict[str, Binding]] = []

    # -- value helpers ----------------------------------------------------

    @staticmethod
    def format_value(v) -> str:
        if v is None:
            return "nil"
        if v is True:
            return "true"
        if v is False:
            return "false"
        if isinstance(v, Closure):
            return f"<fn {v.name}>"
        return str(v)

    def _truthy(self, v) -> bool:
        if v is None or v is False:
            return False
        if v is True:
            return True
        if isinstance(v, int) and not isinstance(v, bool):
            return v != 0
        raise RuntimeError_(
            f"value {self.format_value(v)!r} cannot be used as a condition")

    def _int(self, v, span: Span, what: str = "operand"):
        if not isinstance(v, int) or isinstance(v, bool):
            raise RuntimeError_(
                f"{what} must be an integer, got {self.format_value(v)}", span)
        return v

    # -- binding resolution ----------------------------------------------

    def _push_scope(self, preset: Optional[dict[str, Binding]] = None):
        self.scopes.append(dict(preset or {}))

    def _pop_scope(self):
        self.scopes.pop()

    def _lookup_binding(self, name: str, span: Span) -> Binding:
        for scope in reversed(self.scopes):
            if name in scope:
                return scope[name]
        raise RuntimeError_(f"unbound variable {name!r}", span)

    @staticmethod
    def _raw(frame: Frame, b: Binding):
        """Read storage for ``b`` without dereferencing a Cell.

        Walk the invocation chain by unique function id: intermediate frames
        resolve the binding through their captured ``free_env``; the owner
        frame resolves it from its local slot. Closures capture the storage
        entry itself so a boxed Cell is shared rather than snapshotted.
        """
        f = frame
        while f.fi.func_id != b.owner:
            if b in f.free_env:
                return f.free_env[b]
            f = f.parent
            assert f is not None, f"dangling capture {b.name!r}"
        return f.slots[b.slot]

    @staticmethod
    def _read(frame: Frame, b: Binding):
        v = Interpreter._raw(frame, b)
        return v.value if isinstance(v, Cell) else v

    @staticmethod
    def _write(frame: Frame, b: Binding, value):
        f = frame
        while f.fi.func_id != b.owner:
            if b in f.free_env:
                entry = f.free_env[b]
                if isinstance(entry, Cell):
                    entry.value = value
                else:
                    f.free_env[b] = value
                return
            f = f.parent
            assert f is not None, f"dangling capture {b.name!r}"
        entry = f.slots[b.slot]
        if isinstance(entry, Cell):
            entry.value = value
        else:
            f.slots[b.slot] = value

    def read(self, b: Binding):
        return self._read(self.frame, b)

    def write(self, b: Binding, value):
        self._write(self.frame, b, value)

    # -- entry ------------------------------------------------------------

    def run(self):
        main = self.resolution.main
        self.frame = Frame(fi=main, slots=[None] * main.slots)
        self._install_boxed_cells(main)
        self._push_scope()
        try:
            self._exec_block(self.program)
        except ReturnSignal as ret:
            return ret.value
        finally:
            self._pop_scope()
        return None

    def _install_boxed_cells(self, fi: FunctionInfo):
        for b in self.resolution.bindings:
            if b.owner == fi.func_id and b.boxed:
                self.frame.slots[b.slot] = Cell(None)

    # -- statements -------------------------------------------------------

    def _exec_block(self, block_node):
        self._push_scope()
        try:
            # Hoist pass: create closures for block-level function
            # declarations before any statement runs, in source order.
            for stmt in block_node.body:
                if isinstance(stmt, ast.FunctionStmt):
                    fi: FunctionInfo = stmt.res
                    self.write(fi.name_binding, self._make_closure(stmt, fi))
            for stmt in block_node.body:
                if isinstance(stmt, ast.FunctionStmt):
                    continue
                self._stmt(stmt)
        finally:
            self._pop_scope()

    def _stmt(self, node: ast.Stmt):
        if isinstance(node, ast.Let):
            value = self._eval(node.init) if node.init else None
            self._declare_local(node.res, value)
        elif isinstance(node, ast.FunctionStmt):
            pass  # installed by the enclosing block's hoist pass
        elif isinstance(node, ast.Block):
            self._exec_block(node)
        elif isinstance(node, ast.If):
            if self._truthy(self._eval(node.cond)):
                self._exec_block(node.then)
            elif node.otherwise is not None:
                self._exec_block(node.otherwise)
        elif isinstance(node, ast.While):
            while self._truthy(self._eval(node.cond)):
                self._exec_block(node.body)
        elif isinstance(node, ast.Return):
            value = self._eval(node.value) if node.value else None
            raise ReturnSignal(value)
        elif isinstance(node, ast.ExprStmt):
            self._eval(node.expr)
        else:  # pragma: no cover
            raise AssertionError(f"unknown stmt {type(node)}")

    def _declare_local(self, b: Binding, value):
        self.scopes[-1][b.name] = b
        if b.boxed:
            self.frame.slots[b.slot].value = value
        else:
            self.frame.slots[b.slot] = value

    # -- expressions ------------------------------------------------------

    def _eval(self, node: ast.Expr):
        if isinstance(node, (ast.IntLit, ast.StrLit, ast.BoolLit)):
            return node.value
        if isinstance(node, ast.NilLit):
            return None
        if isinstance(node, ast.Var):
            return self.read(node.res)
        if isinstance(node, ast.Assign):
            value = self._eval(node.value)
            self.write(node.res, value)
            return value
        if isinstance(node, ast.Unary):
            v = self._eval(node.operand)
            if node.op == "-":
                return -self._int(v, node.span)
            return not self._truthy(v)
        if isinstance(node, ast.Binary):
            return self._binary(node)
        if isinstance(node, ast.Call):
            callee = self._eval(node.callee)
            args = [self._eval(a) for a in node.args]
            return self._call(callee, args, node.span)
        if isinstance(node, ast.FunExpr):
            return self._make_closure(node, node.res)
        if isinstance(node, ast.PrintExpr):
            parts = [self.format_value(self._eval(a)) for a in node.args]
            text = ", ".join(parts)
            self.output.append(text)
            if not self.capture_output:
                print(text)
            return None
        raise AssertionError(f"unknown expr {type(node)}")  # pragma: no cover

    def _binary(self, node: ast.Binary):
        if node.op == "and":
            left = self._eval(node.left)
            if not self._truthy(left):
                return left
            return self._eval(node.right)
        if node.op == "or":
            left = self._eval(node.left)
            if self._truthy(left):
                return left
            return self._eval(node.right)
        a = self._eval(node.left)
        b = self._eval(node.right)
        if node.op in ("==", "!="):
            if a is None or b is None:
                eq = a is None and b is None
            elif type(a) is not type(b):
                eq = False  # bool and int are distinct types in ScL
            else:
                eq = a == b
            return eq if node.op == "==" else not eq
        if isinstance(a, Closure) or isinstance(b, Closure):
            raise RuntimeError_(f"cannot apply {node.op!r} to a function value",
                                node.op_span)
        ai = self._int(a, node.left.span, "left operand")
        bi = self._int(b, node.right.span, "right operand")
        if node.op in ("<", "<=", ">", ">="):
            return {"<": ai < bi, "<=": ai <= bi,
                    ">": ai > bi, ">=": ai >= bi}[node.op]
        if node.op == "+":
            return ai + bi
        if node.op == "-":
            return ai - bi
        if node.op == "*":
            return ai * bi
        if node.op in ("/", "%"):
            if bi == 0:
                raise RuntimeError_(
                    "integer division by zero" if node.op == "/"
                    else "integer modulo by zero", node.op_span)
            q = abs(ai) // abs(bi)
            q = q if (ai < 0) == (bi < 0) else -q
            return q if node.op == "/" else ai - q * bi
        raise AssertionError(f"unknown op {node.op}")  # pragma: no cover

    # -- calls and closures ----------------------------------------------

    def _capture(self, fi: FunctionInfo) -> dict[Binding, object]:
        """Collect the parent frame's raw storage for a fresh closure.

        Boxed bindings contribute their shared Cell; unboxed captured values
        are copied (they can never be assigned through, per the resolver's
        boxing rule).
        """
        return {b: self._raw(self.frame, b) for b in fi.free}

    def _make_closure(self, node, fi: FunctionInfo) -> Closure:
        return Closure(fi=fi, node=node, free_env=self._capture(fi),
                       name=fi.name)

    def _call(self, callee, args: list, span: Span):
        if not isinstance(callee, Closure):
            raise RuntimeError_(
                f"value {self.format_value(callee)} is not callable", span)
        fi = callee.fi
        if len(args) != len(fi.params):
            raise RuntimeError_(
                f"function {fi.name!r} expects {len(fi.params)} argument(s), "
                f"got {len(args)}", span)

        new_frame = Frame(fi=fi, slots=[None] * fi.slots,
                          parent=self.frame, free_env=dict(callee.free_env))
        for b in self.resolution.bindings:
            if b.owner == fi.func_id and b.boxed:
                new_frame.slots[b.slot] = Cell(None)

        root_scope = {b.name: b for b in fi.free}
        for pb, arg in zip(fi.params, args):
            if pb.boxed:
                new_frame.slots[pb.slot].value = arg
            else:
                new_frame.slots[pb.slot] = arg
            root_scope[pb.name] = pb

        saved = self.frame
        self.frame = new_frame
        self._push_scope(root_scope)
        try:
            self._exec_block(callee.node.body)
        except ReturnSignal as ret:
            return ret.value
        finally:
            self._pop_scope()
            self.frame = saved
        return None


def run_source(source: str, capture_output: bool = True):
    """Parse + resolve + interpret source text; return ``(result, output)``."""
    from .parser import parse_source
    program = parse_source(source)
    interp = Interpreter(program, capture_output=capture_output)
    result = interp.run()
    return result, interp.output
