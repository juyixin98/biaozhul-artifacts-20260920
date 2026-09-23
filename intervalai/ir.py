"""Integer IR: the resolved program is lowered to a control-flow graph of
basic blocks over temporaries.

Instructions
------------
Const(dst, value)          dst = integer constant
Copy(dst, src)             dst = src scalar/temp interval
LoadElem(dst, arr, src)    dst = arr[src]   (src is a temp holding the index)
StoreElem(arr, idx, src)   arr[idx] = src   (weak/smashed update in analysis)
Unary(dst, op, src)        dst = -src
Bin(dst, op, l, r)         dst = l op r     (+ - * / %)
SetVar(name, src)          user scalar name = src

Terminators
-----------
Jump(target)
Branch(cond_ast, yes, no)  cond is kept as the original (typed) AST node so
                           the abstract transfer can evaluate/filter it;
                           expression alarms (OOB inside the condition) are
                           collected there as well
Halt

Every instruction and block keeps its source location.
"""

from dataclasses import dataclass, field
from typing import Dict, List, Optional

from . import ast_nodes as ast


@dataclass
class Const:
    dst: str
    value: int
    loc: object


@dataclass
class Copy:
    dst: str
    src: str
    loc: object


@dataclass
class LoadElem:
    dst: str
    arr: str
    src: str
    loc: object


@dataclass
class StoreElem:
    arr: str
    idx: str
    src: str
    loc: object


@dataclass
class Unary:
    dst: str
    op: str
    src: str
    loc: object


@dataclass
class Bin:
    dst: str
    op: str
    lhs: str
    rhs: str
    loc: object


@dataclass
class SetVar:
    name: str
    src: str
    loc: object


@dataclass
class Jump:
    target: int
    loc: object = None


@dataclass
class Branch:
    cond: object
    yes: int
    no: int
    loc: object


@dataclass
class Halt:
    loc: object = None


@dataclass
class Block:
    id: int
    instrs: List[object] = field(default_factory=list)
    term: object = None
    loc: object = None
    is_header: bool = False


class Lower:
    def __init__(self, prog: ast.Program):
        self.prog = prog
        self.blocks: List[Block] = []
        self.temps: List[str] = []

    def fresh(self):
        name = f"%t{len(self.temps)}"
        self.temps.append(name)
        return name

    def new_block(self, loc=None):
        bid = len(self.blocks)
        b = Block(bid, loc=loc)
        self.blocks.append(b)
        return bid

    # ------------------------------------------------------------ lowering
    def lower(self):
        exit_b = self.new_block(self.prog.loc)
        self.blocks[exit_b].term = Halt(self.prog.loc)
        entry = self.lower_stmts(self.prog.body, exit_b)
        self.entry = entry
        self.exit = exit_b
        self._compute_graph()
        return self

    def lower_stmts(self, stmts, cont):
        """Chain statements in source order; return entry block id."""
        target = cont
        for s in reversed(stmts):
            target = self.lower_stmt(s, target)
        return target

    def lower_stmt(self, s, cont):
        if isinstance(s, ast.Assign):
            b = self.new_block(s.loc)
            src = self.lower_expr(s.value, b)
            if isinstance(s.target, ast.Var):
                self.blocks[b].instrs.append(SetVar(s.target.name, src, s.loc))
            else:
                idx = self.lower_expr(s.target.index, b)
                # Alarm location is the array-access expression, not the
                # enclosing assignment statement.
                self.blocks[b].instrs.append(
                    StoreElem(s.target.name, idx, src, s.target.loc))
            self.blocks[b].term = Jump(cont, s.loc)
            return b

        if isinstance(s, ast.If):
            then_b = self.lower_stmts(s.then_body, cont)
            else_b = cont
            if s.else_body is not None:
                else_b = self.lower_stmts(s.else_body, cont)
            b = self.new_block(s.loc)
            self.blocks[b].term = Branch(s.cond, then_b, else_b, s.loc)
            return b

        if isinstance(s, ast.While):
            header = self.new_block(s.loc)
            body_b = self.lower_stmts(s.body, header)
            self.blocks[header].term = Branch(s.cond, body_b, cont, s.loc)
            return header

        raise AssertionError(f"cannot lower {type(s).__name__}")

    def lower_expr(self, e, b) -> str:
        """Append instructions to block ``b``; return temp holding the value."""
        if isinstance(e, ast.IntLit):
            t = self.fresh()
            self.blocks[b].instrs.append(Const(t, e.value, e.loc))
            return t
        if isinstance(e, ast.Var):
            t = self.fresh()
            self.blocks[b].instrs.append(Copy(t, e.name, e.loc))
            return t
        if isinstance(e, ast.ArrayRef):
            idx = self.lower_expr(e.index, b)
            t = self.fresh()
            self.blocks[b].instrs.append(LoadElem(t, e.name, idx, e.loc))
            return t
        if isinstance(e, ast.Unary):
            src = self.lower_expr(e.expr, b)
            t = self.fresh()
            self.blocks[b].instrs.append(Unary(t, e.op, src, e.loc))
            return t
        if isinstance(e, ast.Binary):
            l = self.lower_expr(e.lhs, b)
            r = self.lower_expr(e.rhs, b)
            t = self.fresh()
            self.blocks[b].instrs.append(Bin(t, e.op, l, r, e.loc))
            return t
        raise AssertionError(f"cannot lower expression {type(e).__name__}")

    # ----------------------------------------------------------- graph info
    def _compute_graph(self):
        n = len(self.blocks)
        succs: Dict[int, List[int]] = {i: [] for i in range(n)}
        for blk in self.blocks:
            t = blk.term
            if isinstance(t, Jump):
                succs[blk.id].append(t.target)
            elif isinstance(t, Branch):
                succs[blk.id] = [t.yes, t.no]

        # DFS from entry: reverse post-order + back-edge targets (headers).
        WHITE, GRAY, BLACK = 0, 1, 2
        color = [WHITE] * n
        headers = set()
        order = []

        def dfs(u):
            color[u] = GRAY
            for v in succs[u]:
                if color[v] == WHITE:
                    dfs(v)
                elif color[v] == GRAY:
                    headers.add(v)
            color[u] = BLACK
            order.append(u)

        dfs(self.entry)
        rpo = list(reversed(order))

        preds: Dict[int, List[int]] = {i: [] for i in range(n)}
        for u, vs in succs.items():
            for v in vs:
                preds[v].append(u)

        self.succs = succs
        self.preds = preds
        self.rpo = rpo
        for h in headers:
            self.blocks[h].is_header = True

        # Dominators (iterative fixed point over RPO).
        dom = self._dominators(headers, rpo)

        # Back edges u -> h where h dominates u, and the natural loop body
        # (blocks dominated by h that can reach the back edge).
        self.loop_modified: Dict[int, set] = {}
        for h in headers:
            body = {h}
            # seed with every back-edge source u (h dominates u)
            seeds = [u for u in range(n)
                     if h in succs[u] and u in dom[h]]
            stack = list(seeds)
            while stack:
                u = stack.pop()
                if u in body:
                    continue
                body.add(u)
                stack.extend(p for p in preds[u] if p in dom[h])
            modified = set()
            for u in body:
                for ins in self.blocks[u].instrs:
                    if isinstance(ins, SetVar):
                        modified.add(ins.name)
            self.loop_modified[h] = modified

    def _dominators(self, headers, rpo):
        """Return dom[h] = set of blocks dominated by h."""
        n = len(self.blocks)
        order_index = {b: i for i, b in enumerate(rpo)}
        # dom_set[u]: set of nodes dominating u
        full = set(range(n))
        dom_set = {b: set(full) for b in range(n)}
        dom_set[self.entry] = {self.entry}
        changed = True
        while changed:
            changed = False
            for u in rpo:
                if u == self.entry:
                    continue
                preds_dom = [dom_set[p] for p in self.preds[u]]
                new = set.intersection(*preds_dom) if preds_dom else set()
                new = new | {u}
                if new != dom_set[u]:
                    dom_set[u] = new
                    changed = True
        result = {h: set() for h in headers}
        for node in range(n):
            for h in headers:
                if h in dom_set[node]:
                    result[h].add(node)
        return result


def lower(prog: ast.Program) -> Lower:
    return Lower(prog).lower()
