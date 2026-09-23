"""Source-level reference interpreter.

Executes the AST directly, independent of the closure converter and IR.
Names resolve through a lexical chain of environments:

* parameters and ``let`` bindings live in block environments;
* function declarations are hoisted into the function-level environment (a
  cell that starts empty and is filled when the declaration executes);
* when a function literal or declaration closes over a mutable binding, that
  binding is materialized as a shared :class:`SCell` so all capturing closures
  observe the same updates.

The boxing criterion is recomputed from the source (``collect_free``); this
interpreter never reads analyzer output.  It shares only :mod:`slang.values`
with the IR interpreter, which is what makes differential testing meaningful.
"""

from __future__ import annotations

from dataclasses import dataclass

from . import ast_nodes as ast
from .errors import RuntimeErr
from .values import eval_binary, eval_unary, format_value, is_truthy

_UNSET = object()
_MISSING = object()


class SCell:
    __slots__ = ("value",)
    slang_type = "cell"

    def __init__(self, value=_UNSET):
        self.value = value


@dataclass
class Env:
    parent: "Env | None"
    vars: dict

    def define(self, name, value):
        self.vars[name] = value

    def lookup(self, name):
        env = self
        while env is not None:
            if name in env.vars:
                return env.vars[name], env
            env = env.parent
        return None, None


@dataclass
class SClosure:
    node: object
    env: Env
    name: str
    slang_type: str = "closure"

    @property
    def display_name(self):
        return self.name


@dataclass
class SBuiltin:
    name: str
    fn: object
    slang_type: str = "builtin"


class _Return(Exception):
    def __init__(self, value):
        self.value = value


class SourceInterpreter:
    def __init__(self, program: ast.Program, trace: list | None = None):
        self.program = program
        self.trace = trace if trace is not None else []
        root = Env(None, {})
        root.define("print", SBuiltin("print", self._builtin_print))
        self.global_env = root

    def _builtin_print(self, args):
        if len(args) != 1:
            raise RuntimeErr(f"print() expects 1 argument, got {len(args)}")
        self.trace.append(format_value(args[0]))
        return None

    # ----------------------------------------------------------------- run

    def run(self) -> list:
        fn_env = self.enter_function(self.program.stmts, self.global_env, params={})
        try:
            self.exec_stmts(self.program.stmts, fn_env)
        except _Return:
            pass
        return self.trace

    # ----------------------------------------------------- function setup

    def enter_function(self, stmts, parent_env: Env, params: dict) -> Env:
        """Create the function-level environment.

        Parameters go in first; then names of every function declaration
        (found across nested blocks) are reserved as empty cells.  The
        closure itself is published only when execution reaches the
        declaration statement, so calls before that point raise a
        temporal-dead-zone error — matching the closure-converted IR.
        """
        env = Env(parent_env, dict(params))
        for d in self.collect_fn_decls(stmts):
            env.define(d.name, SCell())
        return env

    def collect_fn_decls(self, stmts) -> list:
        out: list = []
        for s in stmts:
            if isinstance(s, ast.FnDecl):
                out.append(s)
            elif isinstance(s, ast.Block):
                out.extend(self.collect_fn_decls(s.stmts))
            elif isinstance(s, ast.If):
                out.extend(self.collect_fn_decls(s.then.stmts))
                if s.otherwise is not None:
                    out.extend(
                        self.collect_fn_decls(s.otherwise.stmts)
                        if isinstance(s.otherwise, ast.Block)
                        else self.collect_fn_decls([s.otherwise])
                    )
            elif isinstance(s, ast.While):
                out.extend(self.collect_fn_decls(s.body.stmts))
        return out

    def publish_decl(self, decl: ast.FnDecl, fn_env: Env):
        closure_env, _ = self.capture_env(decl, fn_env)
        fn_env.vars[decl.name].value = SClosure(decl, closure_env, decl.name)

    # ------------------------------------------------------------- boxing

    def capture_env(self, fn_node, env: Env):
        for name in self.collect_free(fn_node):
            holder, owner = env.lookup(name)
            if owner is None or isinstance(holder, SCell):
                continue
            owner.vars[name] = SCell(holder)
        closure_env = env
        self_cell = None
        if isinstance(fn_node, ast.FnExpr) and fn_node.name is not None:
            closure_env = Env(env, {})
            self_cell = SCell()
            closure_env.define(fn_node.name, self_cell)
        return closure_env, self_cell

    def collect_free(self, fn_node) -> set:
        """Names used in the function body that are not its own locals."""
        scopes: list[set] = []
        free: set = set()

        def local(name):
            scopes[-1].add(name)

        def is_local(name):
            return any(name in sc_ for sc_ in scopes)

        def use(name):
            if not is_local(name):
                free.add(name)

        def fn_names(stmts):
            return {s.name for s in stmts if isinstance(s, ast.FnDecl)}

        def stmt(s):
            t = type(s)
            if t is ast.Block:
                scopes.append(fn_names(s.stmts))
                for x in s.stmts:
                    stmt(x)
                scopes.pop()
            elif t is ast.Let:
                if s.init is not None:
                    expr(s.init)
                local(s.name)
            elif t is ast.FnDecl:
                local(s.name)
            elif t is ast.Return:
                if s.value is not None:
                    expr(s.value)
            elif t is ast.If:
                expr(s.cond)
                stmt(s.then)
                if s.otherwise is not None:
                    stmt(s.otherwise)
            elif t is ast.While:
                expr(s.cond)
                stmt(s.body)
            elif t is ast.Assign:
                expr(s.value)
                use(s.name)
            elif t is ast.ExprStmt:
                expr(s.expr)

        def expr(e):
            t = type(e)
            if t is ast.Var:
                use(e.name)
            elif t is ast.Unary:
                expr(e.operand)
            elif t is ast.Binary:
                expr(e.left)
                expr(e.right)
            elif t is ast.Call:
                expr(e.callee)
                for a in e.args:
                    expr(a)
            # Nested fn literals own their scope and are handled separately.

        scopes.append(fn_names(fn_node.body.stmts))
        for p in fn_node.params:
            local(p.name)
        if isinstance(fn_node, ast.FnExpr) and fn_node.name is not None:
            local(fn_node.name)
        for x in fn_node.body.stmts:
            stmt(x)
        free.discard("print")
        return free

    # ------------------------------------------------------- statements

    def exec_stmts(self, stmts, env: Env):
        for s in stmts:
            self.exec_stmt(s, env)

    def exec_stmt(self, s, env: Env):
        t = type(s)
        if t is ast.Block:
            inner = Env(env, {})
            self.exec_stmts(s.stmts, inner)
        elif t is ast.Let:
            value = None if s.init is None else self.eval(s.init, env)
            # Block-scoped define: always bind in THIS block env.  If the name
            # was previously converted to a cell at this same level (capture
            # happens lazily when a nested closure is created), write through.
            current = env.vars.get(s.name, _MISSING)
            if isinstance(current, SCell):
                current.value = value
            else:
                env.define(s.name, value)
        elif t is ast.FnDecl:
            # Name was reserved at function entry; closure is published now.
            self.publish_decl(s, env)
        elif t is ast.Return:
            value = None if s.value is None else self.eval(s.value, env)
            raise _Return(value)
        elif t is ast.If:
            if is_truthy(self.eval(s.cond, env)):
                self.exec_stmt(s.then, env)
            elif s.otherwise is not None:
                self.exec_stmt(s.otherwise, env)
        elif t is ast.While:
            while is_truthy(self.eval(s.cond, env)):
                self.exec_stmt(s.body, env)
        elif t is ast.Assign:
            value = self.eval(s.value, env)
            holder, owner = env.lookup(s.name)
            if owner is None:
                raise RuntimeErr(f"assignment to unbound name '{s.name}'", s.name_loc)
            if isinstance(holder, SCell):
                holder.value = value
            else:
                owner.vars[s.name] = value
        elif t is ast.ExprStmt:
            self.eval(s.expr, env)
        else:  # pragma: no cover
            raise RuntimeErr(f"internal: unknown statement {t.__name__}", getattr(s, "loc", None))

    # ------------------------------------------------------ expressions

    def read(self, name: str, loc, env: Env):
        holder, _ = env.lookup(name)
        if holder is None:
            raise RuntimeErr(f"unbound name '{name}'", loc)
        if isinstance(holder, SCell):
            if holder.value is _UNSET:
                raise RuntimeErr(f"cannot read '{name}' before it is initialized", loc)
            return holder.value
        if holder is _UNSET:
            raise RuntimeErr(f"cannot read '{name}' before it is initialized", loc)
        return holder

    def eval(self, e, env: Env):
        t = type(e)
        if t is ast.IntLit:
            return e.value
        if t is ast.StrLit:
            return e.value
        if t is ast.BoolLit:
            return e.value
        if t is ast.NullLit:
            return None
        if t is ast.Var:
            holder, owner = env.lookup(e.name)
            if owner is None:
                raise RuntimeErr(f"unbound name '{e.name}'", e.name_loc)
            if isinstance(holder, SCell):
                if holder.value is _UNSET:
                    raise RuntimeErr(f"cannot read '{e.name}' before it is initialized", e.name_loc)
                return holder.value
            if holder is _UNSET:
                raise RuntimeErr(f"cannot read '{e.name}' before it is initialized", e.name_loc)
            return holder
        if t is ast.Unary:
            return eval_unary(e.op, self.eval(e.operand, env))
        if t is ast.Binary:
            return self.eval_binary(e, env)
        if t is ast.Call:
            return self.eval_call(e, env)
        if t is ast.FnExpr:
            closure_env, self_cell = self.capture_env(e, env)
            clo = SClosure(e, closure_env, e.name or "<lambda>")
            if self_cell is not None:
                self_cell.value = clo
            return clo
        raise RuntimeErr(f"internal: unknown expression {t.__name__}", getattr(e, "loc", None))  # pragma: no cover

    def eval_binary(self, e: ast.Binary, env: Env):
        if e.op == "&&":
            left = self.eval(e.left, env)
            return left if not is_truthy(left) else self.eval(e.right, env)
        if e.op == "||":
            left = self.eval(e.left, env)
            return left if is_truthy(left) else self.eval(e.right, env)
        return eval_binary(e.op, self.eval(e.left, env), self.eval(e.right, env))

    def eval_call(self, e: ast.Call, env: Env):
        callee = self.eval(e.callee, env)
        args = [self.eval(a, env) for a in e.args]
        if isinstance(callee, SBuiltin):
            return callee.fn(args)
        if not isinstance(callee, SClosure):
            raise RuntimeErr("value is not callable", getattr(e, "loc", None))
        node = callee.node
        if len(args) != len(node.params):
            nm = getattr(node, "name", None) or "<lambda>"
            raise RuntimeErr(
                f"{nm}() expects {len(node.params)} arguments but got {len(args)}", getattr(e, "loc", None)
            )
        params = {p.name: a for p, a in zip(node.params, args)}
        body_env = self.enter_function(node.body.stmts, callee.env, params)
        try:
            self.exec_stmts(node.body.stmts, body_env)
        except _Return as r:
            return r.value
        return None


def run_program(program: ast.Program, trace: list | None = None) -> list:
    return SourceInterpreter(program, trace=trace).run()
