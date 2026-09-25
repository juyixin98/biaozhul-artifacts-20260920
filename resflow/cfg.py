"""Control-flow graph construction for ResFlow functions.

The CFG is built by hand from the AST (no compiler framework is used).

Node kinds
----------
entry, exit, statement, return, throw, branch, loop, merge, catch_head,
uncaught

Edge kinds
----------
normal, true, false, exception, back

* ``branch`` nodes fan out with a ``true`` and a ``false`` edge.
* ``loop`` nodes fan out with ``true`` (into the body) and ``false``
  (exit the loop); the body closes with a ``back`` edge.
* ``throw`` nodes leave via a single ``exception`` edge routed to the
  enclosing ``catch_head`` (or to the function-level ``uncaught`` node).
* ``catch_head`` is entered only via exception edges; its outgoing edge
  is ``normal``.

Every node carries the source :class:`~resflow.lexer.Location` of the
construct that created it.
"""

from dataclasses import dataclass, field

from . import ast_nodes as ast


@dataclass
class Node:
    id: int
    kind: str
    loc: object = None
    detail: dict = field(default_factory=dict)


@dataclass
class Edge:
    src: int
    dst: int
    kind: str          # normal | true | false | exception | back
    detail: dict = field(default_factory=dict)


class CFGBuildError(Exception):
    pass


class CFGBuilder:
    def __init__(self, function):
        self.fn = function
        self.nodes = []
        self.edges = []

    # -- primitive operations ---------------------------------------------

    def node(self, kind, loc, **detail):
        n = Node(len(self.nodes), kind, loc, dict(detail))
        self.nodes.append(n)
        return n.id

    def edge(self, src, dst, kind="normal", **detail):
        self.edges.append(Edge(src, dst, kind, dict(detail)))

    # -- public entry point ------------------------------------------------

    def build(self):
        entry = self.node("entry", self.fn.loc, kind_detail="fn_entry")
        exitn = self.node("exit", self.fn.close_loc or self.fn.loc,
                          kind_detail="normal_return")
        uncaught = self.node("uncaught", self.fn.close_loc or self.fn.loc,
                             kind_detail="exception_exit")
        ctx = _BuildCtx(exitn, uncaught)
        starts, exits_ = self._block(self.fn.body, ctx)
        if starts:
            self.edge(entry, starts[0], "normal")
        else:  # empty function body: fall straight through to exit
            self.edge(entry, exitn, "normal")
        for x in exits_:
            self.edge(x, exitn, "normal")
        return CFG(self.fn, self.nodes, self.edges, entry, exitn, uncaught)

    # -- block / statement lowering ---------------------------------------

    def _block(self, stmts, ctx):
        """Return (start_ids, open_exit_ids) for a statement sequence."""
        starts = None
        open_exits = None
        for s in stmts:
            st, ex = self._stmt(s, ctx)
            if not st:
                continue  # pure merge with no entry
            if starts is None:
                starts = st
            else:
                for x in open_exits:
                    self.edge(x, st[0], "normal")
            open_exits = ex
        if starts is None:
            return [], []
        return starts, open_exits

    def _stmt(self, s, ctx):
        t = type(s)
        if t in (ast.VarDecl, ast.Assign):
            return self._simple(s, "noop")
        if t is ast.Acquire:
            return self._simple(s, "acquire", resource=s.resource)
        if t is ast.Release:
            return self._simple(s, "release", resource=s.resource)
        if t is ast.Use:
            return self._simple(s, "use", resource=s.resource)
        if t is ast.ReturnStmt:
            n = self.node("return", s.loc,
                          has_value=s.value is not None)
            self.edge(n, ctx.fn_exit, "normal")
            return [n], []
        if t is ast.ThrowStmt:
            n = self.node("throw", s.loc, message=s.message)
            frame = ctx.current_frame()
            if frame is not None:
                # Wired to the catch_head once it has been created.
                frame.append(n)
            else:
                self.edge(n, ctx.uncaught, "exception", caught=False)
            return [n], []
        if t is ast.IfStmt:
            return self._if(s, ctx, ast.const_eval(s.cond))
        if t is ast.WhileStmt:
            return self._while(s, ctx, ast.const_eval(s.cond))
        if t is ast.TryStmt:
            return self._try(s, ctx)
        raise CFGBuildError(f"cannot lower statement {t.__name__}")

    def _simple(self, s, op, **detail):
        label = {
            ast.Acquire: "acquire", ast.Release: "release",
            ast.Use: "use", ast.VarDecl: "let",
            ast.Assign: "assign",
        }[type(s)]
        node_id = self.node("statement", s.loc, op=op, label=label,
                            **detail)
        name_loc = getattr(s, "name_loc", None)
        if name_loc is not None:
            self.nodes[node_id].detail["resource_location"] = \
                name_loc.to_dict()
        return [node_id], [node_id]

    def _if(self, s, ctx, const_value):
        head = self.node("branch", s.loc, label="if",
                         cond_value=const_value)
        then_st, then_ex = self._block(s.then_body, ctx)
        else_st, else_ex = self._block(s.else_body, ctx)
        if then_st:
            self.edge(head, then_st[0], "true")
        else:
            then_ex = [head]  # empty then: branch itself carries the flow
        if else_st:
            self.edge(head, else_st[0], "false")
        elif s.else_body:
            else_ex = [head]
        # A synthetic join makes "no else" explicit on the graph.
        join = self.node("merge", s.loc, label="endif")
        if not else_st and not s.else_body:
            self.edge(head, join, "false")
        for x in then_ex:
            self.edge(x, join, "normal")
        for x in else_ex:
            self.edge(x, join, "normal")
        return [head], [join]

    def _while(self, s, ctx, const_value):
        head = self.node("loop", s.loc, label="while",
                         cond_value=const_value)
        join = self.node("merge", s.loc, label="endwhile")
        body_st, body_ex = self._block(s.body, ctx)
        if body_st:
            self.edge(head, body_st[0], "true")
            for x in body_ex:
                self.edge(x, head, "back")
        else:
            # Empty body: the true side immediately loops back.
            self.edge(head, head, "true")
        self.edge(head, join, "false")
        return [head], [join]

    def _try(self, s, ctx):
        # A pending-frames stack lets the catch_head node be created after
        # the try body (so node ids follow source order) while throws inside
        # the body still get wired to it.  Nested try blocks nest frames.
        frame = []
        ctx.push_frame(frame)
        try_st, try_ex = self._block(s.body, ctx)
        ctx.pop_frame()
        catchn = self.node("catch_head", s.catch_loc, label="catch",
                           message=s.message)
        for tnode in frame:
            self.edge(tnode, catchn, "exception", caught=True)

        h_st, h_ex = self._block(s.handler, ctx)
        if h_st:
            self.edge(catchn, h_st[0], "normal")
        join = self.node("merge", s.loc, label="endtry")
        if not h_st:
            self.edge(catchn, join, "normal")

        if try_st:
            starts = list(try_st)
        else:
            # Empty try body: nothing can throw and there is no normal
            # flow to re-route; the join keeps the construct reachable.
            starts = [join]
        for x in try_ex:
            self.edge(x, join, "normal")
        for x in h_ex:
            self.edge(x, join, "normal")
        return starts, [join]


class _BuildCtx:
    """Stack of active try frames plus function terminal nodes."""

    def __init__(self, fn_exit, uncaught):
        self.fn_exit = fn_exit
        self.uncaught = uncaught
        self._frames = []

    def push_frame(self, frame):
        self._frames.append(frame)

    def pop_frame(self):
        return self._frames.pop()

    def current_frame(self):
        return self._frames[-1] if self._frames else None


@dataclass
class CFG:
    function: object
    nodes: list
    edges: list
    entry: int
    exit: int
    uncaught: int

    def out_edges(self, node_id, kinds=None):
        result = [e for e in self.edges if e.src == node_id]
        if kinds is not None:
            result = [e for e in result if e.kind in kinds]
        return result

    def to_dict(self):
        return {
            "entry": self.entry,
            "exit": self.exit,
            "uncaught": self.uncaught,
            "nodes": [
                {"id": n.id, "kind": n.kind,
                 "location": n.loc.to_dict() if n.loc else None,
                 "detail": n.detail}
                for n in self.nodes
            ],
            "edges": [
                {"src": e.src, "dst": e.dst, "kind": e.kind,
                 "detail": e.detail}
                for e in self.edges
            ],
        }


def build_cfg(function):
    return CFGBuilder(function).build()
