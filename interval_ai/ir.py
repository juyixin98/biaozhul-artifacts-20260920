"""Integer intermediate representation as a control-flow graph.

The AST is lowered to a CFG of basic blocks of side-effecting integer
"instructions".  Branches become pairs of Assume edges.  Every instruction
keeps the source Span of the construct it came from, so analysis alarms can
point at the original division / index expression.

The IR has no expressions and no temporaries: scalar variables are assigned
straight from an RValue tree of small operators, which is exactly the shape
the interval transfer function consumes.
"""

from __future__ import annotations

from dataclasses import dataclass, field

from . import ast_nodes as ast
from .errors import AnalysisError, Span


# ---- r-values (side-effect free integer expressions) ----------------------

@dataclass(frozen=True)
class RConst:
    value: int


@dataclass(frozen=True)
class RVar:
    name: str
    span: Span


@dataclass(frozen=True)
class RArrayLoad:
    name: str
    index: "RExpr"
    length: int
    span: Span               # span of the whole a[e] expression
    op_span: Span            # span of "[" (the indexing operation)


@dataclass(frozen=True)
class RNeg:
    operand: "RExpr"
    span: Span


@dataclass(frozen=True)
class RBin:
    op: str
    left: "RExpr"
    right: "RExpr"
    span: Span               # whole expression
    op_span: Span            # the operator token (alarm site for / %)


RExpr = RConst | RVar | RArrayLoad | RNeg | RBin


# ---- instructions ----------------------------------------------------------

@dataclass(frozen=True)
class IAssign:
    name: str
    value: RExpr
    span: Span
    name_span: Span


@dataclass(frozen=True)
class IArrayStore:
    name: str
    index: RExpr
    value: RExpr
    length: int
    span: Span
    op_span: Span


@dataclass(frozen=True)
class IInput:
    name: str
    span: Span
    name_span: Span


@dataclass(frozen=True)
class IHavoc:
    name: str
    span: Span
    name_span: Span


@dataclass(frozen=True)
class ISkip:
    span: Span


# Terminator-style guards live as pairs of Assume instructions at the start
# of the two successor blocks (no separate edge labels needed).
@dataclass(frozen=True)
class IAssume:
    expr: RExpr             # boolean RValue (see below); interpreted by analyzer
    span: Span


# Boolean conditions are expressed as dedicated relational/boolean RValues;
# the analyzer refines intervals from them.
@dataclass(frozen=True)
class RRel:
    op: str                 # < <= > >= == !=
    left: RExpr
    right: RExpr
    span: Span


@dataclass(frozen=True)
class RBool:
    value: bool


@dataclass(frozen=True)
class RNot:
    operand: RExpr          # RRel | RNot | RAnd | ROr
    span: Span


@dataclass(frozen=True)
class RAnd:
    left: RExpr
    right: RExpr
    span: Span


@dataclass(frozen=True)
class ROr:
    left: RExpr
    right: RExpr
    span: Span


CondExpr = RRel | RBool | RNot | RAnd | ROr
AnyRExpr = RExpr | CondExpr


@dataclass(frozen=True)
class Jump:
    target: int


@dataclass(frozen=True)
class Branch:
    cond: CondExpr
    then: int
    else_: int
    span: Span


@dataclass(frozen=True)
class Exit:
    pass


Terminator = Jump | Branch | Exit


@dataclass
class Block:
    id: int
    instrs: list[IAssign | IArrayStore | IInput | IHavoc | ISkip | IAssume] = \
        field(default_factory=list)
    terminator: Terminator = Exit()


@dataclass
class CFG:
    blocks: list[Block]
    entry: int
    vars: list[str]
    arrays: dict[str, int]          # name -> length
    input_vars: set[str]
    var_spans: dict[str, Span]


def _build(program: ast.Program, source: str) -> CFG:
    program = _register_local_decls(program)
    return _Compiler(program, source).compile_program()


def _register_local_decls(program: ast.Program) -> ast.Program:
    """Register locally declared scalars at program scope.

    Interval analysis needs a fixed set of scalar variables from block 0
    (states are tuples over all variables).  A locally declared name is
    registered with a zero initializer at program scope -- matching the
    concrete semantics where every scalar is zero-allocated at declaration
    even on a path that never enters its branch -- while an initializing
    ``var x = e;`` additionally emits its assignment at the textual location
    so that enclosing guards (e.g. ``if (k>0){ var z=10/k; }``) are honored.
    """
    known = {d.name for d in program.decls}
    zero = ast.IntLit(0, program.span)
    extra: list[ast.Decl] = []

    def conv_block(blk: ast.Block) -> ast.Block:
        new_stmts = []
        for s in blk.stmts:
            if isinstance(s, ast.LocalDecl):
                if s.name not in known:
                    known.add(s.name)
                    extra.append(ast.Decl(s.name, s.span, "var", init=zero))
                if s.init is not None:
                    new_stmts.append(ast.Assign(
                        s.name, s.init, s.span, s.span))
                else:
                    new_stmts.append(ast.Skip(s.span))
            elif isinstance(s, ast.If):
                else_b = None if s.else_ is None else conv_block(s.else_)
                new_stmts.append(ast.If(s.cond, conv_block(s.then), else_b,
                                        s.span))
            elif isinstance(s, ast.While):
                new_stmts.append(ast.While(s.cond, conv_block(s.body),
                                           s.span))
            else:
                new_stmts.append(s)
        return ast.Block(new_stmts, blk.span)

    body = conv_block(program.body)
    return ast.Program(list(program.decls) + extra, body, program.span)


class _Compiler:
    def __init__(self, program: ast.Program, source: str):
        self.program = program
        self.source = source
        self.cfg = CFG(blocks=[], entry=0, vars=[], arrays={},
                       input_vars=set(), var_spans={})
        self.declared: set[str] = set()

    def fresh(self) -> Block:
        blk = Block(len(self.cfg.blocks))
        self.cfg.blocks.append(blk)
        return blk

    def require_var(self, name: str, span: Span) -> None:
        if name not in self.declared:
            raise AnalysisError(f"undeclared variable {name!r}", span)

    def require_array(self, name: str, span: Span) -> int:
        if name not in self.cfg.arrays:
            raise AnalysisError(
                f"{name!r} is not an array (arrays must be declared with "
                f"'input array')", span)
        return self.cfg.arrays[name]

    def require_not_array(self, name: str, span: Span) -> None:
        if name in self.cfg.arrays:
            raise AnalysisError(f"{name!r} is an array, not a scalar", span)

    def compile_program(self) -> CFG:
        entry = self.fresh()

        # Declarations.
        for d in self.program.decls:
            if d.name in self.declared:
                raise AnalysisError(f"duplicate declaration of {d.name!r}",
                                    d.span)
            self.declared.add(d.name)
            self.cfg.var_spans[d.name] = d.span
            if d.kind == "array":
                self.cfg.arrays[d.name] = d.length  # type: ignore[arg-type]
                continue
            self.cfg.vars.append(d.name)
            if d.kind == "input":
                self.cfg.input_vars.add(d.name)
                entry.instrs.append(IInput(d.name, d.span, d.span))
            elif d.init is not None:
                entry.instrs.append(
                    IAssign(d.name, self.lower_expr(d.init, entry),
                            d.span, d.span))
            # No initializer: the variable is left Bottom until its first
            # assignment (locally declared vars initialize at their decl).

        # Body: emit statements into a list of (block, successor-link) work;
        # we thread blocks explicitly.
        final = self.emit_block(self.program.body, entry)
        final.terminator = Exit()
        self.cfg.entry = entry.id
        return self.cfg

    # Statements are emitted into "chains": emit_block returns a list of
    # (block, link) where link is ("jump", id) / ("branch", cond, t, e) /
    # ("end",) describing how each produced block leaves control flow.
    # A simpler concrete approach is used: statements return the block into
    # which subsequent statements should be emitted (creating blocks as
    # needed), terminated blocks return None.

    def emit_block(self, block: ast.Block, cur: Block) -> Block:
        """Emit a statement block starting in ``cur``; return exit block(s)
        are handled by jump/branch construction in helpers."""
        for s in block.stmts:
            cur = self.emit_stmt(s, cur)
            if cur is None:
                # Control flow ended (cannot happen mid-block except errors);
                # restart in a fresh block that nothing jumps to yet.
                cur = self.fresh()
        return cur

    def emit_stmt(self, s: ast.Stmt, cur: Block) -> Block | None:
        if isinstance(s, ast.Assign):
            self.require_var(s.name, s.name_span)
            self.require_not_array(s.name, s.name_span)
            cur.instrs.append(
                IAssign(s.name, self.lower_expr(s.value, cur),
                        s.span, s.name_span))
            return cur

        if isinstance(s, ast.ArrayStore):
            length = self.require_array(s.name, s.name_span)
            cur.instrs.append(IArrayStore(
                s.name,
                self.lower_expr(s.index, cur),
                self.lower_expr(s.value, cur),
                length, s.span, s.name_span))
            return cur

        if isinstance(s, ast.InputStmt):
            self.require_var(s.name, s.name_span)
            self.require_not_array(s.name, s.name_span)
            self.cfg.input_vars.add(s.name)
            cur.instrs.append(IInput(s.name, s.span, s.name_span))
            return cur

        if isinstance(s, ast.HavocStmt):
            self.require_var(s.name, s.name_span)
            self.require_not_array(s.name, s.name_span)
            cur.instrs.append(IHavoc(s.name, s.span, s.name_span))
            return cur

        if isinstance(s, ast.Skip):
            cur.instrs.append(ISkip(s.span))
            return cur

        if isinstance(s, ast.If):
            return self.emit_if(s, cur)

        if isinstance(s, ast.While):
            return self.emit_while(s, cur)

        raise AnalysisError("unhandled statement", s.span)  # pragma: no cover

    def emit_if(self, s: ast.If, cur: Block) -> Block:
        then_blk = self.fresh()
        join = self.fresh()
        else_blk = self.fresh() if s.else_ is not None else join
        cur.terminator = Branch(self.lower_cond(s.cond, cur),
                                then_blk.id, else_blk.id, s.cond.span)
        then_end = self.emit_block(s.then, then_blk)
        if then_end is not None and isinstance(then_end.terminator, Exit):
            then_end.terminator = Jump(join.id)
        if s.else_ is not None:
            else_end = self.emit_block(s.else_, else_blk)
            if else_end is not None and isinstance(else_end.terminator, Exit):
                else_end.terminator = Jump(join.id)
        return join

    def emit_while(self, s: ast.While, cur: Block) -> Block:
        header = self.fresh()
        cur.terminator = Jump(header.id)
        body_blk = self.fresh()
        after = self.fresh()
        header.terminator = Branch(self.lower_cond(s.cond, header),
                                   body_blk.id, after.id, s.cond.span)
        body_end = self.emit_block(s.body, body_blk)
        if body_end is not None and isinstance(body_end.terminator, Exit):
            body_end.terminator = Jump(header.id)
        return after

    # ---- expression lowering --------------------------------------------

    def lower_expr(self, e: ast.Expr, blk: Block) -> RExpr:
        if isinstance(e, ast.IntLit):
            return RConst(e.value)
        if isinstance(e, ast.Var):
            self.require_var(e.name, e.span)
            return RVar(e.name, e.span)
        if isinstance(e, ast.ArrayLoad):
            length = self.require_array(e.name, e.name_span)
            return RArrayLoad(e.name, self.lower_expr(e.index, blk),
                              length, e.span, e.name_span)
        if isinstance(e, ast.Unary):
            if e.op == "-":
                return RNeg(self.lower_expr(e.operand, blk), e.span)
            # ! on an integer expression treats nonzero as true; parser
            # mostly produces ! over comparisons, which lands in lower_cond.
            raise AnalysisError(
                "boolean '!' used in integer expression", e.span)
        if isinstance(e, ast.Binary):
            if e.op in ("&&", "||"):
                raise AnalysisError(
                    f"boolean operator {e.op!r} used in integer expression",
                    e.span)
            if e.op in ("<", "<=", ">", ">=", "==", "!="):
                raise AnalysisError(
                    "comparison used in integer position", e.span)
            return RBin(e.op,
                        self.lower_expr(e.left, blk),
                        self.lower_expr(e.right, blk),
                        e.span, e.op_span)
        if isinstance(e, ast.BoolLit):
            raise AnalysisError("boolean used in integer expression", e.span)
        raise AnalysisError("unhandled expression", e.span)  # pragma: no cover

    def lower_cond(self, e: ast.Expr, blk: Block) -> CondExpr:
        if isinstance(e, ast.BoolLit):
            return RBool(e.value)
        if isinstance(e, ast.Unary) and e.op == "!":
            return RNot(self.lower_cond(e.operand, blk), e.span)
        if isinstance(e, ast.Binary) and e.op == "&&":
            return RAnd(self.lower_cond(e.left, blk),
                        self.lower_cond(e.right, blk), e.span)
        if isinstance(e, ast.Binary) and e.op == "||":
            return ROr(self.lower_cond(e.left, blk),
                       self.lower_cond(e.right, blk), e.span)
        if isinstance(e, ast.Binary) and e.op in (
                "<", "<=", ">", ">=", "==", "!="):
            return RRel(e.op,
                        self.lower_expr(e.left, blk),
                        self.lower_expr(e.right, blk), e.span)
        # Non-boolean expression as condition: nonzero means true.  Encode as
        # e != 0.
        return RRel("!=", self.lower_expr(e, blk), RConst(0), e.span)


def build_cfg(program: ast.Program, source: str) -> CFG:
    return _build(program, source)
