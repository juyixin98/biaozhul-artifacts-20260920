"""Scope analysis and escape analysis for Slang.

This pass walks the AST and answers, for every use of a name, where that name
lives; it also decides which local bindings must be *boxed* into a heap cell
because they are captured by a nested function.

Scoping rules implemented here (the reference interpreters obey the same
rules):

* ``let`` is block scoped; a nested block may shadow an outer name.
* Function parameters are locals of the function body.
* Function declarations are hoisted to the nearest enclosing FUNCTION scope:
  their names are visible throughout the body, including from nested blocks
  and from textually earlier code, but reading the name before its
  declaration statement executes is a runtime (temporal-dead-zone) error.
* A named function expression's name is visible only inside its own body.

Escape analysis:

* A binding referenced from a nested function is ``boxed``: at runtime it is
  stored in a heap cell shared by all capturing closures.  Assignments
  through one closure become visible to the others.
* Every frame lists the free variables its body refers to (``free``) and the
  capture slots that thread cells through intermediate functions
  (``captures``).
"""

from __future__ import annotations

from dataclasses import dataclass, field

from . import ast_nodes as ast
from .errors import CompileError


@dataclass
class VarInfo:
    """One binding of a name in some function frame."""

    name: str
    frame: "FunctionFrame"
    slot: int
    name_loc: object = None
    boxed: bool = False
    is_param: bool = False
    is_self: bool = False          # self-name of a named function expression
    is_fn: bool = False            # hoisted function declaration
    decl: object = None


@dataclass
class Binding:
    kind: str                      # "local" | "free" | "builtin"
    var: VarInfo | None = None
    builtin: str | None = None
    captured_via: int | None = None  # free: slot in THIS frame's closure env


@dataclass
class Capture:
    var: VarInfo
    from_frame: str
    slot: int


@dataclass
class FreeRef:
    var: VarInfo
    owner: str
    slot: int


@dataclass
class FunctionFrame:
    id: str
    name: str
    node: object
    parent: "FunctionFrame | None"
    scopes: list = field(default_factory=list)   # block scopes: dict[name, VarInfo]
    slots: list = field(default_factory=list)    # VarInfo in slot order
    boxed_slots: list = field(default_factory=list)
    captures: list = field(default_factory=list)
    free: list = field(default_factory=list)
    nested: list = field(default_factory=list)
    self_slot: int | None = None
    self_name: str | None = None
    loc: object = None


class Analyzer:
    def __init__(self, program: ast.Program):
        self.program = program
        self.frames: dict[str, FunctionFrame] = {}
        self.resolved: dict[int, object] = {}
        self._counter = 0

    # ------------------------------------------------------------------ API

    def analyze(self):
        main = self._new_frame("main", "main", self.program, None)
        main.loc = self.program.loc
        # Implicit outer scope holding built-ins, then the function body scope.
        main.scopes.append({"print": VarInfo("print", main, -1)})
        main.scopes.append({})
        self.prealloc_frame(main, self.program.stmts)
        for s in self.program.stmts:
            self.stmt(s, main)
        self.finalize(main)
        return AnalysisResult(self.frames, self.resolved)

    # ---------------------------------------------------------- frame setup

    def _new_frame(self, fid, name, node, parent):
        f = FunctionFrame(id=fid, name=name, node=node, parent=parent, loc=getattr(node, "loc", None))
        self.frames[fid] = f
        if parent is not None:
            parent.nested.append(fid)
        return f

    def _fresh_frame_id(self, name):
        self._counter += 1
        base = name if name != "<lambda>" else "lambda"
        return f"{base}_{self._counter}"

    def alloc_slot(self, frame, var: VarInfo):
        var.slot = len(frame.slots)
        frame.slots.append(var)

    def prealloc_frame(self, frame: FunctionFrame, stmts):
        """Determine this frame's local slots before name resolution runs.

        Walking the block structure once (without descending into nested
        functions) pre-binds function-declaration names into the function
        scope and gives every ``let`` its slot in TEXTUAL order.  Resolution
        still happens during the main walk, so a use appearing before a
        declaration is reported correctly; preallocation only guarantees that
        earlier statements can already refer to stable slot numbers.
        """
        function_scope = frame.scopes[-1]

        def walk(stmts, block_scope: dict):
            for s in stmts:
                t = type(s)
                if t is ast.FnDecl:
                    if s.name in function_scope:
                        raise CompileError(
                            f"'{s.name}' is already declared in this function", s.name_loc
                        )
                    var = VarInfo(s.name, frame, -1, name_loc=s.name_loc, is_fn=True, decl=s)
                    s._var_info = var
                    function_scope[s.name] = var
                    self.alloc_slot(frame, var)
                elif t is ast.Let:
                    # Reserve the slot now (textual order) and remember the
                    # binding on the node; the name enters its block scope
                    # when the main walk reaches this statement.
                    var = VarInfo(s.name, frame, -1, name_loc=s.name_loc, decl=s)
                    s._var_info = var
                    self.alloc_slot(frame, var)
                elif t is ast.Block:
                    walk(s.stmts, {})
                elif t is ast.If:
                    walk(s.then.stmts, {})
                    if s.otherwise is not None:
                        if isinstance(s.otherwise, ast.Block):
                            walk(s.otherwise.stmts, {})
                        else:
                            walk([s.otherwise], {})
                elif t is ast.While:
                    walk(s.body.stmts, {})

        walk(stmts, {})

    # ------------------------------------------------------------- lookup

    def lookup_local(self, frame: FunctionFrame, name: str):
        for scope in reversed(frame.scopes):
            if name in scope:
                return scope[name]
        return None

    def ancestors(self, frame: FunctionFrame):
        out = []
        p = frame.parent
        while p is not None:
            out.append(p)
            p = p.parent
        return out

    def resolve(self, frame: FunctionFrame, node: ast.Var) -> Binding:
        var = self.lookup_local(frame, node.name)
        if var is not None:
            if var.slot < 0 and node.name == "print":
                return Binding("builtin", builtin="print")
            return Binding("local", var=var)
        if node.name == "print":
            return Binding("builtin", builtin="print")
        for anc in self.ancestors(frame):
            var = self.lookup_local(anc, node.name)
            if var is not None:
                var.boxed = True
                return self.make_free(frame, var, anc)
        raise CompileError(f"unbound name '{node.name}'", node.name_loc)

    def make_free(self, frame: FunctionFrame, var: VarInfo, owner: FunctionFrame) -> Binding:
        slot = next((fr.slot for fr in frame.free if fr.var is var), None)
        if slot is None:
            slot = len(frame.free)
            frame.free.append(FreeRef(var, owner.id, slot))
        captured_slot = self.add_capture(frame, var, owner)
        return Binding("free", var=var, captured_via=captured_slot)

    def add_capture(self, frame: FunctionFrame, var: VarInfo, owner: FunctionFrame) -> int:
        for c in frame.captures:
            if c.var is var:
                return c.slot
        slot = len(frame.captures)
        frame.captures.append(Capture(var, owner.id, slot))
        # Thread the cell through every frame between owner and this one.
        p = frame.parent
        while p is not None and p is not owner:
            self.add_capture(p, var, owner)
            p = p.parent
        return slot

    # ------------------------------------------------------------- visitors

    def stmt(self, s, frame: FunctionFrame):
        t = type(s)
        if t is ast.Block:
            frame.scopes.append({})
            for x in s.stmts:
                if type(x) is ast.Let:
                    # The initializer is analyzed BEFORE the name enters the
                    # block scope (so `let x = x;` resolves the RHS outward);
                    # then the binding is installed for later statements.
                    if x.init is not None:
                        self.expr(x.init, frame)
                    if x.name in frame.scopes[-1]:
                        raise CompileError(
                            f"'{x.name}' is already declared in this block", x.name_loc
                        )
                    frame.scopes[-1][x.name] = x._var_info
                else:
                    self.stmt(x, frame)
            frame.scopes.pop()
        elif t is ast.Let:
            if s.init is not None:
                self.expr(s.init, frame)
            if s.name in frame.scopes[-1]:
                raise CompileError(
                    f"'{s.name}' is already declared in this block", s.name_loc
                )
            frame.scopes[-1][s.name] = s._var_info
        elif t is ast.FnDecl:
            self.visit_function(s, frame, s.name)
        elif t is ast.Return:
            if s.value is not None:
                self.expr(s.value, frame)
        elif t is ast.If:
            self.expr(s.cond, frame)
            self.stmt(s.then, frame)
            if s.otherwise is not None:
                self.stmt(s.otherwise, frame)
        elif t is ast.While:
            self.expr(s.cond, frame)
            self.stmt(s.body, frame)
        elif t is ast.Assign:
            # Resolve the target first so assignments to a name declared
            # earlier in the same block bind to that slot, not an outer one.
            probe = ast.Var(s.name, name_loc=s.name_loc)
            self.resolved[id(s)] = self.resolve(frame, probe)
            self.expr(s.value, frame)
        elif t is ast.ExprStmt:
            self.expr(s.expr, frame)
        else:  # pragma: no cover
            raise CompileError(f"internal: unknown statement {t.__name__}", getattr(s, "loc", None))

    def visit_function(self, node, parent_frame: FunctionFrame, name: str | None):
        fid = self._fresh_frame_id(name or "lambda")
        f = self._new_frame(fid, name or "<lambda>", node, parent_frame)
        f.scopes.append({})  # body scope
        for p in node.params:
            if p.name in f.scopes[-1]:
                raise CompileError(f"duplicate parameter '{p.name}'", p.name_loc)
            pv = VarInfo(p.name, f, -1, name_loc=p.name_loc, is_param=True, decl=p)
            f.scopes[-1][p.name] = pv
            self.alloc_slot(f, pv)
        if isinstance(node, ast.FnExpr) and node.name is not None:
            sv = VarInfo(node.name, f, -1, name_loc=node.name_loc, is_self=True, decl=node)
            sv.boxed = True
            f.scopes[-1][node.name] = sv
            self.alloc_slot(f, sv)
            f.self_slot = sv.slot
            f.self_name = node.name
        self.prealloc_frame(f, node.body.stmts)
        for x in node.body.stmts:
            self.stmt(x, f)
        self.finalize(f)
        return f

    def expr(self, e, frame: FunctionFrame):
        t = type(e)
        if t in (ast.IntLit, ast.StrLit, ast.BoolLit, ast.NullLit):
            return
        if t is ast.Var:
            self.resolved[id(e)] = self.resolve(frame, e)
        elif t is ast.Unary:
            self.expr(e.operand, frame)
        elif t is ast.Binary:
            self.expr(e.left, frame)
            self.expr(e.right, frame)
        elif t is ast.Call:
            self.expr(e.callee, frame)
            for a in e.args:
                self.expr(a, frame)
        elif t is ast.FnExpr:
            self.visit_function(e, frame, e.name)
        else:  # pragma: no cover
            raise CompileError(f"internal: unknown expression {t.__name__}", getattr(e, "loc", None))

    def finalize(self, frame: FunctionFrame):
        frame.boxed_slots = [i for i, v in enumerate(frame.slots) if v.boxed]


class AnalysisResult:
    def __init__(self, frames: dict, resolved: dict):
        self.frames = frames
        self.resolved = resolved

    def binding_to_dict(self, node) -> dict:
        b = self.resolved.get(id(node))
        if b is None:
            return {"kind": "unresolved"}
        if b.kind == "builtin":
            return {"kind": "builtin", "name": b.builtin}
        out = {
            "kind": b.kind,
            "name": b.var.name,
            "frame": b.var.frame.id,
            "slot": b.var.slot,
            "boxed": b.var.boxed,
        }
        if b.kind == "free":
            out["captured_via"] = b.captured_via
        return out

    def frame_to_dict(self, f: FunctionFrame) -> dict:
        return {
            "id": f.id,
            "name": f.name,
            "parent": f.parent.id if f.parent else None,
            "params": [{"name": v.name, "slot": v.slot, "boxed": v.boxed} for v in f.slots if v.is_param],
            "slots": [
                {"name": v.name, "slot": v.slot, "boxed": v.boxed, "is_param": v.is_param, "is_fn": v.is_fn}
                for v in f.slots
            ],
            "boxed_slots": f.boxed_slots,
            "captures": [{"name": c.var.name, "slot": c.slot, "owner": c.from_frame} for c in f.captures],
            "free": [{"name": fr.var.name, "slot": fr.slot, "owner": fr.owner} for fr in f.free],
            "self_slot": f.self_slot,
            "nested": f.nested,
            "loc": f.loc.to_dict() if f.loc else None,
        }

    def to_dict(self) -> dict:
        return {"frames": {fid: self.frame_to_dict(f) for fid, f in self.frames.items()}}


def analyze(program: ast.Program) -> AnalysisResult:
    return Analyzer(program).analyze()
