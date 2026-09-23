"""Interval abstract interpretation over the integer CFG.

Fixpoint strategy
-----------------
The CFG is analyzed by a worklist algorithm.

1. Ascending iteration with widening.  Loop headers are identified
   structurally (a block is a widening point if some successor path reaches
   it again).  When a header's joined input grows, widening is applied,
   pushing unstable finite bounds to +/-infinity so the chain terminates.
2. Descending iteration with narrowing.  Starting from the post-fixed point,
   one classical narrowing pass re-propagates states; infinite bounds that
   the body actually stabilizes (e.g. ``x <= 9`` from ``x < 10``) are
   recovered.  Classical narrowing never re-widens and terminates in one
   pass; we also bound the pass count defensively.

Soundness rules
---------------
* An unknown interval (Top) is never treated as safe: a divisor interval
  containing 0 raises a division-by-zero alarm; an index interval not fully
  inside ``[0, length-1]`` raises an index-out-of-bounds alarm.
* Array *contents* are not tracked cell-by-cell; every load yields Top.
  Stores are still bounds-checked.
* ``input`` / ``havoc`` variables start at Top.
"""

from __future__ import annotations
from dataclasses import dataclass, field

from .errors import Span
from .intervals import Interval
from . import ir


# ---- abstract state --------------------------------------------------------

@dataclass(frozen=True)
class Alarm:
    kind: str               # "div_by_zero" | "index_out_of_bounds"
    message: str
    span: Span
    block: int
    index_interval: Interval | None = None
    length: int | None = None
    certainty: str = "possible"   # "possible" | "certain"

    def key(self) -> tuple:
        return (self.kind, self.span.start_offset, self.block)

    def to_dict(self) -> dict:
        d = {
            "kind": self.kind,
            "certainty": self.certainty,
            "message": self.message,
            "block": self.block,
            "location": self.span.to_dict(),
        }
        if self.index_interval is not None:
            d["index_interval"] = self.index_interval.to_dict()
        if self.length is not None:
            d["array_length"] = self.length
        return d


State = dict[str, Interval]      # scalar variable -> interval
BOTTOM_MARK = None               # State | None: None == unreachable (bottom)


def top_state(vars_: list[str]) -> State:
    return {v: Interval.top() for v in vars_}


def join_state(a: State | None, b: State | None) -> State | None:
    if a is None:
        return b
    if b is None:
        return a
    return {k: a[k].join(b[k]) for k in a}


def state_subset(a: State | None, b: State | None) -> bool:
    if a is None:
        return True
    if b is None:
        return False
    return all(a[k].subset_eq(b[k]) for k in a)


# ---- expression evaluation -------------------------------------------------

class _EvalError(Exception):
    """Division by zero during abstract evaluation of a pure expression."""

    def __init__(self, span: Span, op: str, divisor: Interval,
                 certain: bool):
        self.span = span
        self.op = op
        self.divisor = divisor
        self.certain = certain


class Analyzer:
    def __init__(self, cfg: ir.CFG):
        self.cfg = cfg
        self.alarm_map: dict[tuple, Alarm] = {}
        # Inputs are unknown; declared scalars are refined as the CFG runs,
        # but their initial value at entry is Bottom for non-inputs until the
        # declaration assignments establish them.

    # ---- scalar expression semantics ------------------------------------

    def eval_int(self, e: ir.RExpr, st: State, block_id: int) -> Interval:
        if isinstance(e, ir.RConst):
            return Interval.point(e.value)
        if isinstance(e, ir.RVar):
            return st.get(e.name, Interval.point(0))
        if isinstance(e, ir.RArrayLoad):
            idx = self.eval_int(e.index, st, block_id)
            self.check_index(idx, e.length, e.span, block_id)
            # Cell contents are not tracked: a load may return any integer.
            return Interval.top()
        if isinstance(e, ir.RNeg):
            return self.eval_int(e.operand, st, block_id).neg()
        if isinstance(e, ir.RBin):
            a = self.eval_int(e.left, st, block_id)
            b = self.eval_int(e.right, st, block_id)
            if e.op in ("/", "%"):
                if b.contains(0):
                    certain = (b.is_point and b.lo == 0)
                    self.raise_div(e, b, block_id, certain)
                if e.op == "/":
                    return a.div(b)
                return a.rem(b)
            return self.apply_arith(e.op, a, b)
        raise AssertionError(f"unexpected rvalue {e!r}")  # pragma: no cover

    @staticmethod
    def apply_arith(op: str, a: Interval, b: Interval) -> Interval:
        if op == "+":
            return a.add(b)
        if op == "-":
            return a.sub(b)
        if op == "*":
            return a.mult(b)
        raise AssertionError(f"bad arith op {op!r}")  # pragma: no cover

    def raise_div(self, e: ir.RBin, divisor: Interval, block_id: int,
                  certain: bool) -> None:
        op = e.op
        name = "modulo by zero" if op == "%" else "division by zero"
        span = e.op_span
        key = ("div_by_zero", span.start_offset, block_id)
        # Do not downgrade a "certain" finding when later seen as possible.
        if key in self.alarm_map and self.alarm_map[key].certainty == "certain":
            return
        self.alarm_map[key] = Alarm(
            "div_by_zero",
            f"possible {name}: divisor interval {divisor} contains 0",
            span, block_id,
            certainty="certain" if certain else "possible")

    def check_index(self, idx: Interval, length: int, span: Span,
                    block_id: int) -> None:
        key = ("index_out_of_bounds", span.start_offset, block_id)
        in_bounds = idx.lo is not None and idx.hi is not None \
            and idx.lo >= 0 and idx.hi < length
        if in_bounds:
            return
        certain = False
        if idx.is_bottom:
            return  # unreachable code contributes no alarm
        if idx.lo is not None and idx.hi is not None:
            certain = idx.hi < 0 or idx.lo >= length
        elif idx.hi is not None and idx.hi < 0:
            certain = True
        elif idx.lo is not None and idx.lo >= length:
            certain = True
        msg = (f"possible index out of bounds: index interval {idx} not "
               f"contained in [0, {length - 1}]")
        if key in self.alarm_map and self.alarm_map[key].certainty == "certain":
            return
        self.alarm_map[key] = Alarm(
            "index_out_of_bounds", msg, span, block_id,
            index_interval=idx, length=length,
            certainty="certain" if certain else "possible")

    # ---- condition filtering --------------------------------------------

    def assume(self, cond: ir.CondExpr, st: State, block_id: int,
               truth: bool) -> State | None:
        """Restrict st to states where cond has the given truth value.

        Returns None when the filter proves the branch unreachable.
        """
        return self._assume(cond, st, block_id, truth)

    def _assume(self, cond, st, block_id, truth):
        if isinstance(cond, ir.RBool):
            if cond.value == truth:
                return st
            return None
        if isinstance(cond, ir.RNot):
            return self._assume(cond.operand, st, block_id, not truth)
        if isinstance(cond, ir.RAnd):
            if truth:
                st = self._assume(cond.left, st, block_id, True)
                if st is None:
                    return None
                return self._assume(cond.right, st, block_id, True)
            # !(a && b) = !a || !b : interval domain can only apply a side
            # when the other side is known true; otherwise no refinement.
            va = self.eval_truth(cond.left, st, block_id)
            vb = self.eval_truth(cond.right, st, block_id)
            if vb is True:
                return self._assume(cond.left, st, block_id, False)
            if va is True:
                return self._assume(cond.right, st, block_id, False)
            return st
        if isinstance(cond, ir.ROr):
            if not truth:
                st = self._assume(cond.left, st, block_id, False)
                if st is None:
                    return None
                return self._assume(cond.right, st, block_id, False)
            va = self.eval_truth(cond.left, st, block_id)
            vb = self.eval_truth(cond.right, st, block_id)
            if vb is False:
                return self._assume(cond.left, st, block_id, True)
            if va is False:
                return self._assume(cond.right, st, block_id, True)
            return st
        if isinstance(cond, ir.RRel):
            return self.assume_rel(cond, st, block_id, truth)
        raise AssertionError(f"bad condition {cond!r}")  # pragma: no cover

    def eval_truth(self, cond, st, block_id):
        """Return True/False if the condition is definite, else None."""
        t = self._assume(cond, st, block_id, True)
        f = self._assume(cond, st, block_id, False)
        if t is None and f is not None:
            return False
        if f is None and t is not None:
            return True
        return None

    def assume_rel(self, rel: ir.RRel, st: State, block_id: int,
                   truth: bool) -> State | None:
        op = rel.op
        if not truth:
            op = {"<": ">=", "<=": ">", ">": "<=", ">=": "<",
                  "==": "!=", "!=": "=="}[op]
        # var-vs-constant and constant-vs-var filtering; also var-vs-var for ==
        if isinstance(rel.left, ir.RVar):
            x = rel.left.name
            rhs = self.eval_int(rel.right, st, block_id)
            new = self.filter_interval(st[x], op, rhs)
            if new is None:
                return None
            st = dict(st)
            st[x] = new
            # Equality: propagate the constant the other way too.
            if op in ("==", "!=") and isinstance(rel.right, ir.RConst) \
                    and op == "==":
                pass
            return st
        if isinstance(rel.right, ir.RVar):
            x = rel.right.name
            lhs = self.eval_int(rel.left, st, block_id)
            # Reverse the operator.
            rev = {"<": ">", "<=": ">=", ">": "<", ">=": "<=",
                   "==": "==", "!=": "!="}[op]
            new = self.filter_interval(st[x], rev, lhs)
            if new is None:
                return None
            st = dict(st)
            st[x] = new
            return st
        return st

    @staticmethod
    def filter_interval(x: Interval, op: str,
                        y: Interval) -> Interval | None:
        # Only constant right sides permit precise refinement.
        if y.is_bottom:
            return None
        if not y.is_point:
            # Fall back: only decide unreachability for clearly disjoint
            # comparisons; otherwise leave unchanged (sound).
            return x
        c = y.lo
        if op == "<":
            r = x.filter_less(c)
        elif op == "<=":
            r = x.filter_leq(c)
        elif op == ">":
            r = x.filter_greater(c)
        elif op == ">=":
            r = x.filter_geq(c)
        elif op == "==":
            r = x.meet(Interval.point(c))
        else:  # "!="
            r = x
            if x.is_point and x.lo == c:
                r = Interval.bottom()
        return None if r.is_bottom else r

    # ---- block transfer --------------------------------------------------

    def exec_block(self, blk: ir.Block, st: State | None) -> State | None:
        if st is None:
            return None
        for ins in blk.instrs:
            st = self.exec_instr(ins, st, blk.id)
            if st is None:
                return None
        return st

    def exec_instr(self, ins, st: State, block_id: int) -> State | None:
        if isinstance(ins, ir.IAssign):
            st = dict(st)
            st[ins.name] = self.eval_int(ins.value, st, block_id)
            return st
        if isinstance(ins, ir.IArrayStore):
            idx = self.eval_int(ins.index, st, block_id)
            _ = self.eval_int(ins.value, st, block_id)
            self.check_index(idx, ins.length, ins.op_span, block_id)
            # Contents not tracked; state unchanged.
            return st
        if isinstance(ins, ir.IInput):
            st = dict(st)
            st[ins.name] = Interval.top()
            return st
        if isinstance(ins, ir.IHavoc):
            st = dict(st)
            st[ins.name] = Interval.top()
            return st
        if isinstance(ins, ir.ISkip):
            return st
        if isinstance(ins, ir.IAssume):
            return self.assume(ins.expr, st, block_id, True)
        raise AssertionError(f"bad instruction {ins!r}")  # pragma: no cover

    # ---- CFG structure ---------------------------------------------------

    def predecessors(self) -> list[list[int]]:
        preds: list[list[int]] = [[] for _ in self.cfg.blocks]
        for b in self.cfg.blocks:
            t = b.terminator
            if isinstance(t, ir.Jump):
                preds[t.target].append(b.id)
            elif isinstance(t, ir.Branch):
                preds[t.then].append(b.id)
                preds[t.else_].append(b.id)
        return preds

    def successors(self, b_id: int) -> list[int]:
        t = self.cfg.blocks[b_id].terminator
        if isinstance(t, ir.Jump):
            return [t.target]
        if isinstance(t, ir.Branch):
            return [t.then, t.else_]
        return []

    def entry_state(self) -> State:
        st: State = {}
        for v in self.cfg.vars:
            if v in self.cfg.input_vars:
                st[v] = Interval.top()
            else:
                st[v] = Interval.bottom()
        return st

    # ---- fixpoint --------------------------------------------------------

    def analyze(self) -> "AnalysisResult":
        preds = self.predecessors()
        widening_points = self.find_widening_points()
        loop_vars = {h: self.loop_modified_vars(h) for h in widening_points}

        # in[b] is the state on entry to block b (None == unreachable).
        inputs: list[State | None] = [None] * len(self.cfg.blocks)
        inputs[self.cfg.entry] = self.entry_state()

        # ---------- ascending iteration with widening ----------
        # Chaotic iteration driven by a worklist, in reverse postorder so
        # acyclic blocks are processed before their successors.  Widening is
        # applied at loop headers against their current iterate, which
        # guarantees termination (each header bound stabilizes in finitely
        # many iterations: finite -> +/-infinity).
        rpo = self.reverse_postorder()
        priority = {b: i for i, b in enumerate(rpo)}
        worklist = [self.cfg.entry]
        in_wl = {self.cfg.entry}

        def schedule(blocks) -> None:
            for x in blocks:
                if x not in in_wl:
                    in_wl.add(x)
                    worklist.append(x)
            worklist.sort(key=lambda b: priority.get(b, 1 << 30))

        while worklist:
            b_id = worklist.pop(0)
            in_wl.discard(b_id)

            out = self.exec_block(self.cfg.blocks[b_id], inputs[b_id])
            for s in self.successors(b_id):
                edge = self.edge_to(b_id, s, out)
                old = inputs[s]
                if edge is None:
                    continue
                if old is None:
                    new = edge
                elif s in widening_points and not state_subset(edge, old):
                    # Widen only the variables this loop actually modifies;
                    # carried-through variables (e.g. outer counters) keep
                    # their bounds.
                    new = self.widen_state_vars(old, edge, loop_vars[s])
                else:
                    new = join_state(old, edge)
                if not state_subset(new, old):
                    inputs[s] = new
                    schedule([s])

        # ---------- descending iteration with narrowing ----------
        inputs = self.narrow(inputs, preds, widening_points)

        # Re-execute every reachable block once more from the narrowed
        # invariants so alarms reflect the final, sharpened states.
        for b_id in self.reverse_postorder():
            self.exec_block(self.cfg.blocks[b_id], inputs[b_id])

        invariants = [None if s is None else dict(s) for s in inputs]
        return AnalysisResult(self.cfg, invariants,
                              sorted(self.alarm_map.values(),
                                     key=lambda a: (a.span.start_offset,
                                                    a.block, a.kind)))

    def edge_to(self, src: int, dst: int, st: State | None) -> State | None:
        if st is None:
            return None
        t = self.cfg.blocks[src].terminator
        if isinstance(t, ir.Branch):
            if dst == t.then:
                return self.assume(t.cond, st, src, True)
            if dst == t.else_:
                return self.assume(t.cond, st, src, False)
        return st

    def widen_state(self, old: State, new: State) -> State:
        return {k: old[k].widen(new[k]) for k in old}

    def widen_state_vars(self, old: State, new: State,
                         vars_: set[str]) -> State:
        """Widen only the given loop-modified variables; others join."""
        out = {}
        for k in old:
            if k in vars_:
                out[k] = old[k].widen(new[k])
            else:
                out[k] = old[k].join(new[k])
        return out

    def narrow_state(self, old: State, new: State) -> State:
        return {k: old[k].narrow(new[k]) for k in old}

    def find_widening_points(self) -> set[int]:
        """Targets of back edges -- the loop headers (v dominates u for an
        edge u -> v)."""
        dom = self.dominators()
        wps: set[int] = set()
        for u in range(len(self.cfg.blocks)):
            for v in self.successors(u):
                if v in dom[u]:
                    wps.add(v)
        return wps

    def loop_modified_vars(self, header: int) -> set[str]:
        """Scalars assigned in the *natural loop* headed by ``header``.

        For every back edge n -> header, its natural-loop body is the set of
        blocks that can reach n without passing through the header.  This
        precisely excludes outer-loop blocks (an outer tail reaches the inner
        header only by going through it again), so an inner header never
        widens an outer counter and vice versa.
        """
        dom = self.dominators()
        back_sources = [u for u in range(len(self.cfg.blocks))
                        for v in self.successors(u)
                        if v == header and header in dom[u]]
        body: set[int] = set()
        for n in back_sources:
            # Reverse DFS from n over predecessors, stopping at the header.
            stack = [n]
            while stack:
                x = stack.pop()
                if x in body or x == header:
                    continue
                body.add(x)
                for p in self.predecessors()[x]:
                    if p != header and p not in body:
                        stack.append(p)
        modified: set[str] = set()
        for b_id in body:
            for ins in self.cfg.blocks[b_id].instrs:
                if isinstance(ins, (ir.IAssign, ir.IInput, ir.IHavoc)):
                    modified.add(ins.name)
        return modified

    def dominators(self) -> list[set[int]]:
        n = len(self.cfg.blocks)
        preds = self.predecessors()
        entry = self.cfg.entry
        all_blocks = set(range(n))
        dom: list[set[int]] = [set(all_blocks) for _ in range(n)]
        dom[entry] = {entry}
        changed = True
        while changed:
            changed = False
            for b in range(n):
                if b == entry:
                    continue
                incoming = [dom[p] for p in preds[b]]
                new = ({b} | set.intersection(*incoming)) if incoming else {b}
                if new != dom[b]:
                    dom[b] = new
                    changed = True
        return dom

    def narrow(self, inputs: list[State | None],
               preds: list[list[int]], wps: set[int]) -> list[State | None]:
        """Descending iteration (classical narrowing).

        Each block's entry state is recomputed by joining all *filtered*
        predecessor edges from the current invariants.  At loop headers the
        classical narrowing operator ``old ▽ new`` is applied (only
        +/--infinite bounds may be refined), which guarantees termination
        without re-widening; other blocks take the exact recomputation.

        Classical narrowing is only valid when the recomputed input is a
        post-fixed point (``incoming ⊆ old``), which can transiently fail for
        an *outer* header while a nested inner loop has not yet stabilized.
        In that case we keep the (still sound) widened value rather than
        loosen a finite bound.  A few bounded rounds propagate the
        refinements; the process is monotone, so it always terminates.
        """
        inputs = [s for s in inputs]
        loop_vars = {h: self.loop_modified_vars(h) for h in wps}
        for _ in range(8):
            changed = False
            for b_id in self.reverse_postorder():
                if b_id == self.cfg.entry:
                    continue
                incoming = None
                for p in preds[b_id]:
                    edge = self.exec_block(self.cfg.blocks[p], inputs[p])
                    edge = self.edge_to(p, b_id, edge)
                    incoming = join_state(incoming, edge)
                old = inputs[b_id]
                if b_id in wps and old is not None:
                    if incoming is None:
                        new = old
                    elif state_subset(incoming, old):
                        # Narrow only loop-modified variables; carried
                        # variables take their exact recomputed value.
                        new = {}
                        lv = loop_vars[b_id]
                        for k in old:
                            new[k] = (old[k].narrow(incoming[k])
                                      if k in lv else incoming[k])
                    else:
                        # Not yet a post-fixed point (nested inner loop may be
                        # stabilizing): keep the sound widened invariant.
                        new = old
                else:
                    new = incoming
                if not states_equal(new, old):
                    inputs[b_id] = new
                    changed = True
            if not changed:
                break
        return inputs

    def reverse_postorder(self) -> list[int]:
        visited: set[int] = set()
        order: list[int] = []

        def dfs(u: int) -> None:
            if u in visited:
                return
            visited.add(u)
            for s in self.successors(u):
                dfs(s)
            order.append(u)

        dfs(self.cfg.entry)
        order.reverse()
        return order


def states_equal(a: State | None, b: State | None) -> bool:
    if a is None or b is None:
        return a is None and b is None
    return all(a[k] == b[k] for k in a)


@dataclass
class AnalysisResult:
    cfg: ir.CFG
    invariants: list[State | None]
    alarms: list[Alarm]
