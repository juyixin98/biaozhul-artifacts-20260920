"""Lexical-scope analysis.

The resolver walks the AST *without executing it* and computes:

* a :class:`Binding` for every declared name (``let``, parameter, named
  function) with a unique local slot in its owning function frame;
* for every function (the program itself is function depth 0), its ordered
  list of **free variables** — names used inside but defined in an enclosing
  function — propagated transitively so intermediate functions forward
  environments;
* whether each binding is **captured** by a nested function and **mutated**
  anywhere. Boxing (heap Cell) is decided by

      boxed  ⇔  binding is a named function  ∨  captured by a nested fn

  A captured binding needs a *stable storage location*: hoisted closures
  come into existence at block entry, before the captured ``let`` has run,
  so capturing a plain slot's value would snapshot ``nil``. Putting every
  captured binding in a pre-allocated Cell makes capture-by-identity
  correct; the extra ``mutated`` bit is still recorded and exposed for
  inspection (a captured-but-never-mutated binding is a *read-only* Cell).
  Mutated locals that never escape stay as plain slots, which is the
  precise-boxing optimization that matters.

Result annotations are attached to AST nodes (``node.res``); no source
position information is discarded.
"""

from dataclasses import dataclass, field
from typing import Optional

from . import ast_nodes as ast
from .errors import CompileError, Span


@dataclass(eq=False)
class Binding:
    name: str
    owner: int                 # unique func_id of the owning function
    kind: str                  # "let" | "param" | "fn" | "builtin"
    slot: int                  # local slot in the owner's frame (-1 builtin)
    span: Span
    mutated: bool = False
    captured: bool = False
    boxed: bool = False


@dataclass(eq=False)
class FunctionInfo:
    func_id: int               # unique id (discovery order); main is 0
    depth: int                 # lexical nesting depth (siblings share this)
    node: object
    name: str
    params: list[Binding] = field(default_factory=list)
    # free variables, in order of first reference
    free: list[Binding] = field(default_factory=list)
    free_boxed: list[bool] = field(default_factory=list)
    slots: int = 0
    name_binding: Optional[Binding] = None

    def __post_init__(self):
        self.free_set: set[Binding] = set()

    def add_free(self, b: Binding) -> bool:
        """Add a transitive free variable; return True if newly added."""
        if b in self.free_set:
            return False
        self.free_set.add(b)
        self.free.append(b)
        return True


@dataclass
class ResolutionResult:
    main: FunctionInfo
    functions: list[FunctionInfo]
    bindings: list[Binding]


class Resolver:
    def __init__(self, program: ast.Program):
        self.program = program
        self.block_scopes: list[dict[str, Binding]] = []
        self.funcs: list[FunctionInfo] = []   # active-function stack
        self.all_funcs: list[FunctionInfo] = []
        self.all_bindings: list[Binding] = []
        # Reserved built-in names (currently "print" is a keyword; the
        # machinery stays so other globals can be added).
        self._builtin_scope: dict[str, Binding] = {}

    # -- scope helpers ----------------------------------------------------

    def _push_scope(self):
        self.block_scopes.append({})

    def _pop_scope(self):
        self.block_scopes.pop()

    def _declare(self, name: str, binding: Binding, span: Span):
        scope = self.block_scopes[-1]
        if name in scope:
            raise CompileError(
                f"{name!r} is already declared in this scope; shadowing is "
                f"only allowed in a nested block",
                span)
        scope[name] = binding
        self.all_bindings.append(binding)

    def _lookup(self, name: str, span: Span) -> Binding:
        for scope in reversed(self.block_scopes):
            if name in scope:
                return scope[name]
        if name in self._builtin_scope:
            return self._builtin_scope[name]
        raise CompileError(f"unbound variable {name!r}", span)

    def _alloc_slot(self, fi: FunctionInfo) -> int:
        slot = fi.slots
        fi.slots += 1
        return slot

    # -- entry ------------------------------------------------------------

    def resolve(self) -> ResolutionResult:
        main = FunctionInfo(func_id=0, depth=0, node=self.program,
                            name="<main>")
        self.funcs.append(main)
        self.all_funcs.append(main)
        self._push_scope()  # main's (single) scope
        self._resolve_stmts_with_hoisting(self.program, self.program.body)
        self._pop_scope()

        # finalize boxing decisions now that all capture/mutation info exists
        for b in self.all_bindings:
            if b.kind == "builtin":
                continue
            b.boxed = b.kind == "fn" or b.captured
        for fi in self.all_funcs:
            fi.free_boxed = [b.boxed for b in fi.free]

        self.program.res = main
        return ResolutionResult(main=main, functions=list(self.all_funcs),
                                bindings=list(self.all_bindings))

    def _hoist_scan(self, stmts: list[ast.Stmt]) -> list[ast.FunctionStmt]:
        """Find block-level function declarations (in source order)."""
        return [s for s in stmts if isinstance(s, ast.FunctionStmt)]

    def _resolve_stmts_with_hoisting(self, block_node, stmts: list[ast.Stmt]):
        """Resolve statements in a block scope, hoisting fn declarations.

        Function names are bound before anything else so a function can call
        a sibling defined later (mutual recursion); the resolver itself is
        order-independent for calls, but hoisting also allocates slots and
        lets a name shadow an outer binding for the *whole* block.
        """
        fi = self.funcs[-1]
        hoisted: list[str] = []
        for f in self._hoist_scan(stmts):
            if f.name in self.block_scopes[-1]:
                existing = self.block_scopes[-1][f.name]
                raise CompileError(
                    f"{f.name!r} is already declared in this scope",
                    f.name_span)
            slot = self._alloc_slot(fi)
            b = Binding(name=f.name, owner=fi.func_id, kind="fn",
                        slot=slot,
                        span=f.name_span, boxed=True)
            self._declare(f.name, b, f.name_span)
            hoisted.append(f.name)
        # remember for the compiler (closures installed at block entry)
        if isinstance(block_node, (ast.Block, ast.Program)):
            block_node.hoisted = hoisted

        for s in stmts:
            self._stmt(s)

    # -- statements -------------------------------------------------------

    def _stmt(self, node: ast.Stmt):
        if isinstance(node, ast.Let):
            self._let(node)
        elif isinstance(node, ast.FunctionStmt):
            self._function(node, name_binding=self.block_scopes[-1][node.name])
        elif isinstance(node, ast.Block):
            self._push_scope()
            self._resolve_stmts_with_hoisting(node, node.body)
            self._pop_scope()
        elif isinstance(node, ast.If):
            self._expr(node.cond)
            self._push_scope()
            self._resolve_stmts_with_hoisting(node.then, node.then.body)
            self._pop_scope()
            if node.otherwise is not None:
                self._push_scope()
                self._resolve_stmts_with_hoisting(
                    node.otherwise, node.otherwise.body)
                self._pop_scope()
        elif isinstance(node, ast.While):
            self._expr(node.cond)
            self._push_scope()
            self._resolve_stmts_with_hoisting(node.body, node.body.body)
            self._pop_scope()
        elif isinstance(node, ast.Return):
            if node.value is not None:
                self._expr(node.value)
        elif isinstance(node, ast.ExprStmt):
            self._expr(node.expr)
        else:  # pragma: no cover - defensive
            raise AssertionError(f"unknown statement {type(node)}")

    def _let(self, node: ast.Let):
        fi = self.funcs[-1]
        if node.init is not None:
            self._expr(node.init)
        slot = self._alloc_slot(fi)
        b = Binding(name=node.name, owner=fi.func_id, kind="let",
                    slot=slot,
                    span=node.name_span)
        self._declare(node.name, b, node.name_span)
        node.res = b

    def _function(self, node: "ast.FunctionStmt | ast.FunExpr",
                  name_binding: Optional[Binding]):
        fi = FunctionInfo(func_id=len(self.all_funcs),
                          depth=len(self.funcs), node=node,
                          name=name_binding.name if name_binding else "<lambda>")
        self.funcs.append(fi)
        self.all_funcs.append(fi)
        self._push_scope()  # parameter scope (outside body block)
        for pname in node.params:
            slot = self._alloc_slot(fi)
            pb = Binding(name=pname, owner=fi.func_id, kind="param",
                         slot=slot,
                         span=node.span)
            self._declare(pname, pb, node.span)
            fi.params.append(pb)
        if name_binding is not None:
            fi.name_binding = name_binding
        # body block introduces its own nested scope
        self._push_scope()
        self._resolve_stmts_with_hoisting(node.body, node.body.body)
        self._pop_scope()
        self._pop_scope()
        self.funcs.pop()
        node.res = fi
        return fi

    # -- expressions ------------------------------------------------------

    def _expr(self, node: ast.Expr):
        if isinstance(node, ast.IntLit) or isinstance(node, ast.StrLit) \
                or isinstance(node, ast.BoolLit) or isinstance(node, ast.NilLit):
            return
        if isinstance(node, ast.Var):
            node.res = self._use(node.name, node.span)
        elif isinstance(node, ast.Assign):
            self._expr(node.value)
            b = self._lookup(node.target.name, node.target.span)
            if b.kind == "builtin":
                raise CompileError(
                    f"cannot assign to built-in {b.name!r}", node.target.span)
            b.mutated = True
            node.target.res = b
            node.res = b
            # the assignment also reads the enclosing binding
            self._use_binding(b, node.target.span)
        elif isinstance(node, ast.Binary):
            self._expr(node.left)
            self._expr(node.right)
        elif isinstance(node, ast.Unary):
            self._expr(node.operand)
        elif isinstance(node, ast.Call):
            self._expr(node.callee)
            for a in node.args:
                self._expr(a)
        elif isinstance(node, ast.FunExpr):
            self._function(node, name_binding=None)
        elif isinstance(node, ast.PrintExpr):
            for a in node.args:
                self._expr(a)
        else:  # pragma: no cover - defensive
            raise AssertionError(f"unknown expression {type(node)}")

    def _use(self, name: str, span: Span) -> Binding:
        b = self._lookup(name, span)
        self._use_binding(b, span)
        return b

    def _use_binding(self, b: Binding, span: Span):
        """Record a use of ``b`` in the currently-resolving function.

        The owning function sees the name as a local; every function strictly
        between the owner and the current one on the lexical chain receives
        it as a transitive free variable so the environment is forwarded.
        Comparison is by lexical *depth*; ownership is by unique func_id.
        """
        if b.kind == "builtin" or b.owner < 0:
            return
        owner_depth = self.all_funcs[b.owner].depth
        cur = self.funcs[-1].depth
        if cur == owner_depth:
            return
        b.captured = True
        # The current function and every intermediate ancestor forward the
        # binding through their environments (first-reference order kept).
        for d in range(cur, owner_depth, -1):
            self.funcs[d].add_free(b)


def resolve_program(program: ast.Program) -> ResolutionResult:
    return Resolver(program).resolve()
