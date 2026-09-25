"""Control-flow graph construction.

Edge kinds (both are explicit CfgEdges and both participate in analysis):

* ``normal``    - ordinary control transfer
* ``exception`` - an error leaves a node that can raise

A node can raise when it is a ``throw`` statement or a call to a function
declared ``throws``. At construction time each such node gets an exception edge
to the handler of its *lexically enclosing* try body (the catch header of the
innermost try containing the node), or to the function's ``except_exit`` when
there is no enclosing catch. Nodes inside a catch body resolve against the
outer handler, so re-throws propagate correctly.

Normal control flow is built separately via dangling normal endpoints, with
labels (``true`` / ``false`` / ``back`` / ``entered-catch``) so recorded paths
can be rendered deterministically.
"""
from __future__ import annotations

from dataclasses import dataclass, field
from typing import Dict, List, Optional, Tuple

from . import ast_nodes as ast
from .locations import SourceFile, Span


NORMAL = "normal"
EXCEPTION = "exception"

# A dangling normal endpoint: (source node, edge label).
Endpoint = Tuple[str, str]


@dataclass
class CfgNode:
    id: str
    kind: str
    span: Span
    label: str
    target: Optional[str] = None
    resource_kind: Optional[str] = None
    call: Optional[ast.CallExpr] = None
    expr: Optional[ast.Expr] = None
    error_var: Optional[str] = None


@dataclass(frozen=True)
class CfgEdge:
    src: str
    dst: str
    kind: str
    label: str = ""


@dataclass
class FunctionCfg:
    name: str
    span: Span
    nodes: Dict[str, CfgNode] = field(default_factory=dict)
    order: List[str] = field(default_factory=list)
    edges: List[CfgEdge] = field(default_factory=list)
    entry: str = ""
    exit: str = ""
    except_exit: str = ""
    resource_vars: List[str] = field(default_factory=list)


class Pending:
    """Dangling normal endpoints waiting for a successor."""

    def __init__(self, endpoints: Optional[List[Endpoint]] = None):
        self.endpoints: List[Endpoint] = list(endpoints or [])

    @classmethod
    def one(cls, src: str, label: str = "") -> "Pending":
        return cls([(src, label)])

    def merge(self, other: "Pending") -> None:
        self.endpoints.extend(other.endpoints)


@dataclass
class StmtFlow:
    """Single normal entry node plus dangling normal exits."""

    entry: str
    pending: Pending


class Cfgbuilder:
    def __init__(self, source: SourceFile, funcs: Dict[str, ast.Function]):
        self.source = source
        self.funcs = funcs
        self.counter = 0

    def new_id(self, prefix: str) -> str:
        self.counter += 1
        return f"{prefix}{self.counter}"

    def text_of(self, span: Span) -> str:
        return self.source.text[span.start.offset:span.end.offset]

    def handler_target(self, cfg: FunctionCfg, ctx: List[str]) -> str:
        """Catch header of the innermost enclosing try, else except_exit."""
        return ctx[-1] if ctx else cfg.except_exit

    def build_all(self) -> Dict[str, FunctionCfg]:
        return {fn.name: self.build_function(fn) for fn in self.funcs.values()}

    def build_function(self, fn: ast.Function) -> FunctionCfg:
        self.counter = 0  # ids deterministic per function
        cfg = FunctionCfg(name=fn.name, span=fn.span)
        entry = self.add_node(cfg, "entry", fn.body.span, f"entry {fn.name}")
        exitn = self.add_node(cfg, "exit", fn.span, f"exit {fn.name}")
        except_exit = self.add_node(cfg, "except_exit", fn.span, f"uncaught-exit {fn.name}")
        cfg.entry, cfg.exit, cfg.except_exit = entry, exitn, except_exit

        resource_vars: List[str] = []
        self.collect_resource_vars(fn.body, resource_vars)
        cfg.resource_vars = resource_vars

        ctx: List[str] = []
        body = self.build_block(cfg, fn.body, ctx)
        self.add_edge(cfg, entry, body.entry, NORMAL, "")
        for src, label in body.pending.endpoints:
            self.add_edge(cfg, src, exitn, NORMAL, label)
        return cfg

    def collect_resource_vars(self, block: ast.Block, acc: List[str]) -> None:
        for stmt in block.statements:
            self.collect_resource_vars_stmt(stmt, acc)

    def collect_resource_vars_stmt(self, stmt: ast.Stmt, acc: List[str]) -> None:
        if isinstance(stmt, ast.AcquireStmt):
            if stmt.target not in acc:
                acc.append(stmt.target)
        elif isinstance(stmt, ast.IfStmt):
            self.collect_resource_vars(stmt.then_block, acc)
            if stmt.else_block is not None:
                self.collect_resource_vars(stmt.else_block, acc)
        elif isinstance(stmt, ast.WhileStmt):
            self.collect_resource_vars(stmt.body, acc)
        elif isinstance(stmt, ast.TryStmt):
            self.collect_resource_vars(stmt.try_block, acc)
            self.collect_resource_vars(stmt.catch_block, acc)

    # ---------- node / edge helpers ----------

    def add_node(self, cfg: FunctionCfg, kind: str, span: Span, label: str, **payload) -> str:
        node_id = self.new_id(f"n{kind}_")
        cfg.nodes[node_id] = CfgNode(id=node_id, kind=kind, span=span, label=label, **payload)
        cfg.order.append(node_id)
        return node_id

    def add_edge(self, cfg: FunctionCfg, src: str, dst: str, kind: str, label: str = "") -> None:
        cfg.edges.append(CfgEdge(src=src, dst=dst, kind=kind, label=label))

    def chain(self, cfg: FunctionCfg, before: Pending, entry: str) -> None:
        for src, label in before.endpoints:
            self.add_edge(cfg, src, entry, NORMAL, label)

    # ---------- blocks / statements ----------

    def build_block(self, cfg: FunctionCfg, block: ast.Block, ctx: List[str]) -> StmtFlow:
        """Build a block with a dedicated head node (single normal entry)."""
        head = self.add_node(cfg, "skip", block.span, "block")
        if not block.statements:
            skip = self.add_node(cfg, "skip", block.span, "empty-block")
            self.add_edge(cfg, head, skip, NORMAL, "")
            return StmtFlow(head, Pending.one(skip))
        first = self.build_stmt(cfg, block.statements[0], ctx)
        self.add_edge(cfg, head, first.entry, NORMAL, "")
        flow = first
        for stmt in block.statements[1:]:
            nxt = self.build_stmt(cfg, stmt, ctx)
            self.chain(cfg, flow.pending, nxt.entry)
            flow = nxt
        return StmtFlow(head, flow.pending)

    def build_stmt(self, cfg: FunctionCfg, stmt: ast.Stmt, ctx: List[str]) -> StmtFlow:
        if isinstance(stmt, ast.AcquireStmt):
            nid = self.add_node(
                cfg, "acquire", stmt.span,
                f"let {stmt.target} = acquire(\"{stmt.kind}\")",
                target=stmt.target, resource_kind=stmt.kind,
            )
            return StmtFlow(nid, Pending.one(nid))

        if isinstance(stmt, (ast.LetCallStmt, ast.ExprStmt)):
            call = stmt.call
            label = (
                f"let {stmt.target} = {self.text_of(call.span)}"
                if isinstance(stmt, ast.LetCallStmt)
                else self.text_of(stmt.span)
            )
            nid = self.add_node(cfg, "call", call.span, label, call=call)
            pending = Pending.one(nid)
            if self.funcs[call.name].throws:
                # Explicit exception edge to the enclosing handler.
                self.add_edge(cfg, nid, self.handler_target(cfg, ctx), EXCEPTION, "threw")
            return StmtFlow(nid, pending)

        if isinstance(stmt, ast.ReleaseStmt):
            nid = self.add_node(cfg, "release", stmt.span, f"release {stmt.target}", target=stmt.target)
            return StmtFlow(nid, Pending.one(nid))

        if isinstance(stmt, ast.UseStmt):
            nid = self.add_node(cfg, "use", stmt.span, f"use({stmt.target})", target=stmt.target)
            return StmtFlow(nid, Pending.one(nid))

        if isinstance(stmt, ast.ReturnStmt):
            nid = self.add_node(cfg, "return", stmt.span, self.text_of(stmt.span), expr=stmt.value)
            self.add_edge(cfg, nid, cfg.exit, NORMAL, "")
            return StmtFlow(nid, Pending())  # no fall-through

        if isinstance(stmt, ast.ThrowStmt):
            nid = self.add_node(cfg, "throw", stmt.span, f"throw \"{stmt.message}\"")
            self.add_edge(cfg, nid, self.handler_target(cfg, ctx), EXCEPTION, "threw")
            return StmtFlow(nid, Pending())

        if isinstance(stmt, ast.IfStmt):
            return self.build_if(cfg, stmt, ctx)
        if isinstance(stmt, ast.WhileStmt):
            return self.build_while(cfg, stmt, ctx)
        if isinstance(stmt, ast.TryStmt):
            return self.build_try(cfg, stmt, ctx)
        raise ValueError(f"unsupported statement {type(stmt).__name__}")

    def build_if(self, cfg: FunctionCfg, stmt: ast.IfStmt, ctx: List[str]) -> StmtFlow:
        cond = self.add_node(
            cfg, "condition", stmt.cond.span, self.text_of(stmt.cond.span), expr=stmt.cond,
        )
        then_flow = self.build_block(cfg, stmt.then_block, ctx)
        self.add_edge(cfg, cond, then_flow.entry, NORMAL, "true")
        merged = Pending()
        merged.merge(then_flow.pending)
        if stmt.else_block is not None:
            else_flow = self.build_block(cfg, stmt.else_block, ctx)
            self.add_edge(cfg, cond, else_flow.entry, NORMAL, "false")
            merged.merge(else_flow.pending)
        else:
            merged.endpoints.append((cond, "false"))
        return StmtFlow(cond, merged)

    def build_while(self, cfg: FunctionCfg, stmt: ast.WhileStmt, ctx: List[str]) -> StmtFlow:
        head = self.add_node(
            cfg, "while", stmt.cond.span, f"while ({self.text_of(stmt.cond.span)})", expr=stmt.cond,
        )
        body_flow = self.build_block(cfg, stmt.body, ctx)
        self.add_edge(cfg, head, body_flow.entry, NORMAL, "true")
        for src, label in body_flow.pending.endpoints:
            self.add_edge(cfg, src, head, NORMAL, "back")
        # false leaves the loop; raising nodes inside already point at the
        # enclosing handler (their ctx included the enclosing catch, if any).
        after = Pending([(head, "false")])
        return StmtFlow(head, after)

    def build_try(self, cfg: FunctionCfg, stmt: ast.TryStmt, ctx: List[str]) -> StmtFlow:
        try_head = self.add_node(cfg, "try", stmt.span, "try")
        catch_head = self.add_node(
            cfg, "catch", stmt.span, f"catch ({stmt.error_var})", error_var=stmt.error_var,
        )
        # Try body: this catch is the innermost handler.
        try_ctx = ctx + [catch_head]
        body_flow = self.build_block(cfg, stmt.try_block, try_ctx)
        self.add_edge(cfg, try_head, body_flow.entry, NORMAL, "")
        after = Pending(body_flow.pending.endpoints)

        # Catch body: outer handlers apply, so re-throws propagate past us.
        catch_flow = self.build_block(cfg, stmt.catch_block, ctx)
        self.add_edge(cfg, catch_head, catch_flow.entry, NORMAL, "entered-catch")
        after.merge(catch_flow.pending)
        return StmtFlow(try_head, after)


def build_cfg(source: SourceFile, funcs: Dict[str, ast.Function]) -> Dict[str, FunctionCfg]:
    return Cfgbuilder(source, funcs).build_all()
