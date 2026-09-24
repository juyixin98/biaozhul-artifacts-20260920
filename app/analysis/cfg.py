"""Per-function control-flow graph construction.

Node kinds:

    SIMPLE   one statement (var / assign / return / expression statement)
    BRANCH   if-statement (edges tagged "then" / "else")
    LOOP     while-statement (edges tagged "body" / "exit")
    JOIN     environment merge point
    EXIT     unique function exit (target of every `return` and fall-through)

Edges carry unique ids because two successors may point at the same node
(empty if-branches); edge ids are also the keys of dataflow edge states.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from itertools import count

from ..language import (
    Assign,
    FuncDecl,
    IfStmt,
    ReturnStmt,
    Stmt,
    VarDecl,
    WhileStmt,
    walk_stmt,
)


@dataclass(frozen=True)
class Edge:
    eid: int
    target: int
    tag: str  # "normal" | "then" | "else" | "body" | "loop-exit"
    back_edge: bool = False


@dataclass
class CfgNode:
    nid: int
    kind: str  # "simple" | "branch" | "loop" | "join" | "exit"
    stmt: Stmt | None = None
    edges: list[Edge] = field(default_factory=list)
    label: str = ""


@dataclass
class Cfg:
    function: str
    nodes: dict[int, CfgNode]
    entry: int
    exit: int
    locals: frozenset[str]


def build_cfg(fn: FuncDecl) -> Cfg:
    b = _Builder(fn)
    body_entry = b.stmts(fn.body, b.fexit)
    entry = body_entry if body_entry is not None else b.fexit
    local_names = {p for p in fn.params}
    for stmt in fn.body:
        for s in walk_stmt(stmt):
            if isinstance(s, (VarDecl, Assign)):
                local_names.add(s.name)
    return Cfg(
        function=fn.name,
        nodes=b.nodes,
        entry=entry,
        exit=b.fexit,
        locals=frozenset(local_names),
    )


class _Builder:
    def __init__(self, fn: FuncDecl):
        self.fn = fn
        self.nodes: dict[int, CfgNode] = {}
        self.ids = count()
        self.eids = count()
        self.fexit = self.node("exit", label="exit")

    def node(self, kind: str, stmt: Stmt | None = None, label: str = "") -> int:
        nid = next(self.ids)
        self.nodes[nid] = CfgNode(nid=nid, kind=kind, stmt=stmt, label=label)
        return nid

    def link(self, src: int, dst: int, tag: str = "normal",
             back_edge: bool = False) -> None:
        self.nodes[src].edges.append(
            Edge(next(self.eids), dst, tag, back_edge=back_edge)
        )

    def stmts(self, stmts: list[Stmt], cont: int,
              cont_back: bool = False) -> int | None:
        """Build a statement sequence.

        The *final* statement links to ``cont``; if ``cont_back`` is true that
        final edge is tagged a back edge (used for loop bodies, where the
        continuation is the loop header).
        """
        if not stmts:
            return None
        heads: list[int] = []
        n = len(stmts)
        # We still build tail-first (a statement's continuation must exist),
        # but cont_back is applied ONLY at the final statement's out-edge.
        current_cont = cont
        current_back = cont_back
        for i in range(n - 1, -1, -1):
            stmt = stmts[i]
            head = self._one(stmt, current_cont, current_back)
            heads.append(head)
            current_cont = head
            current_back = False
        return heads[-1]

    def _one(self, stmt: Stmt, cont: int, cont_back: bool = False) -> int:
        if isinstance(stmt, IfStmt):
            return self._if(stmt, cont)
        if isinstance(stmt, WhileStmt):
            return self._while(stmt, cont)
        nid = self.node("simple", stmt)
        if isinstance(stmt, ReturnStmt):
            # Returns bypass the structural continuation entirely.
            self.link(nid, self.fexit)
        else:
            self.link(nid, cont, back_edge=cont_back)
        return nid

    def _if(self, stmt: IfStmt, cont: int) -> int:
        join = self.node("join", label="endif")
        self.link(join, cont)
        then_entry = self.stmts(stmt.then_body, join) or join
        else_entry = self.stmts(stmt.else_body, join) or join
        branch = self.node("branch", stmt, label="if")
        self.link(branch, then_entry, "then")
        self.link(branch, else_entry, "else")
        return branch

    def _while(self, stmt: WhileStmt, cont: int) -> int:
        join = self.node("join", label="endwhile")
        self.link(join, cont)
        loop = self.node("loop", stmt, label="while")
        # Edges from the body's tail back to the loop header are back edges.
        body_entry = self.stmts(stmt.body, loop, cont_back=True) or loop
        self.link(loop, body_entry, "body")
        self.link(loop, join, "loop-exit")
        return loop
