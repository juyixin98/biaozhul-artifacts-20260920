"""Interval abstract interpreter over the integer-IR CFG.

Fixpoint strategy
-----------------
1. *Ascending iteration with widening.*  A worklist propagates abstract states
   along edges.  At a loop header (a DFS back-edge target), incoming states are
   joined with widening; plain joins are used elsewhere.  Widening sends any
   unstable bound straight to +/- infinity, so iteration terminates.
2. *Descending iteration with narrowing.*  Once the post-fixpoint is reached,
   bounded RPO rounds recompute states and narrow at headers, recovering
   precision (e.g. tightening an unbounded induction variable to its real
   upper bound).
3. *Alarm pass.*  A final RPO pass records per-block entry environments and
   collects division-by-zero / array-OOB alarms with locations.  Alarms are
   reported for everything that *may* go wrong; an interval of "unknown"
   (``[-oo,+oo]``) is never treated as safety.

Arithmetic is over mathematical integers; see :mod:`intervalai.intervals`.
"""

from typing import Dict, List

from . import abstract_env as ae
from . import intervals as iv
from . import ir as ir_mod
from .abstract_env import Env, BOT_ENV, assume, condition_alarms


NARROW_ROUNDS = 4


class AnalysisResult:
    def __init__(self):
        self.entry_states: Dict[int, Env] = {}
        self.exit_states: Dict[int, Env] = {}
        self.points: List[dict] = []
        self.alarms: List[dict] = []
        self.headers: List[int] = []
        self.iterations = 0
        self.narrow_rounds = 0


class Analyzer:
    def __init__(self, cfg: ir_mod.Lower, entry_env: Env):
        self.cfg = cfg
        self.blocks = cfg.blocks
        self.preds = cfg.preds
        self.succs = cfg.succs
        self.rpo = cfg.rpo
        self.headers = {b.id for b in self.blocks if b.is_header}
        self.entry_env = entry_env
        # user-scalar names (temps are filtered out of reported states)
        self.user_names = {n for n in entry_env.vars}

    # ------------------------------------------------------------- transfer
    def transfer_block(self, blk, env_in, collect=False):
        """Return (env_out, edge_outs, alarms).

        edge_outs maps successor id -> outgoing env (after the terminator
        filter).  When ``collect`` is true, program-point records and alarms
        are gathered.
        """
        alarms: List[dict] = []
        points: List[dict] = []
        env = env_in
        for ins in blk.instrs:
            if env.is_bottom():
                break
            env, new_alarms = self.transfer_ins(ins, env)
            if collect:
                alarms.extend(new_alarms)
                points.append(self._point(blk, ins, env, "after"))
        # terminator
        edge_outs: Dict[int, Env] = {}
        term = blk.term
        if not env.is_bottom():
            if isinstance(term, ir_mod.Jump):
                edge_outs[term.target] = env
            elif isinstance(term, ir_mod.Halt):
                pass
            elif isinstance(term, ir_mod.Branch):
                (yes, no), term_alarms = self.branch_envs(env, term)
                edge_outs[term.yes] = yes
                edge_outs[term.no] = no
                if collect:
                    alarms.extend(term_alarms)
            else:
                raise AssertionError("bad terminator")
        return env, edge_outs, alarms, points

    def branch_envs(self, env, term):
        cond = term.cond
        yes = assume(env, cond, negate=False)
        no = assume(env, cond, negate=True)
        # OOB/div alarms occurring inside the condition expression
        alarms = condition_alarms(env, cond)
        return (yes, no), alarms

    def transfer_ins(self, ins, env):
        alarms = []
        if isinstance(ins, ir_mod.Const):
            env = env.set_var(ins.dst, iv.const(ins.value))
        elif isinstance(ins, ir_mod.Copy):
            env = env.set_var(ins.dst, env.vars.get(ins.src, iv.TOP))
        elif isinstance(ins, ir_mod.Unary):
            v = env.vars.get(ins.src, iv.TOP)
            env = env.set_var(ins.dst, iv.neg(v))
        elif isinstance(ins, ir_mod.Bin):
            env, alarms = self.do_bin(ins, env)
        elif isinstance(ins, ir_mod.LoadElem):
            idx = env.vars.get(ins.src, iv.TOP)
            size, summary = env.arrays[ins.arr]
            oob = self._oob_alarms(ins, idx, size)
            alarms.extend(oob)
            # If the index may be out of bounds, the loaded value is unknown
            # (an in-bounds element would be covered by the summary, but an
            # OOB read yields an arbitrary/undefined value). Using TOP here is
            # essential for soundness; otherwise faults downstream of an
            # OOB-loaded value could be missed.
            value = iv.TOP if oob else summary
            env = env.set_var(ins.dst, value)
        elif isinstance(ins, ir_mod.StoreElem):
            idx = env.vars.get(ins.idx, iv.TOP)
            size, _ = env.arrays[ins.arr]
            for al in self._oob_alarms(ins, idx, size):
                alarms.append(al)
            val = env.vars.get(ins.src, iv.TOP)
            _, cur = env.arrays[ins.arr]
            # Smashed / weak update: all elements abstracted by one interval.
            env = env.copy()
            env.arrays[ins.arr] = (size, iv.join(cur, val))
        elif isinstance(ins, ir_mod.SetVar):
            v = env.vars.get(ins.src, iv.TOP)
            env = env.set_var(ins.name, v)
        else:
            raise AssertionError(f"bad instruction {type(ins).__name__}")
        return env, alarms

    def do_bin(self, ins, env):
        a = env.vars.get(ins.lhs, iv.TOP)
        b = env.vars.get(ins.rhs, iv.TOP)
        alarms = []
        op = ins.op
        if op == "+":
            r = iv.add(a, b)
        elif op == "-":
            r = iv.sub(a, b)
        elif op == "*":
            r = iv.mul(a, b)
        elif op == "/":
            r, may_zero = iv.div(a, b)
            if may_zero:
                alarms.append(self._div_alarm(ins, b))
                # A division by zero is a fault: on that path the result is
                # undefined, so the continuation must see TOP rather than the
                # (possibly empty) quotient over valid divisors.  Returning BOT
                # here would wrongly mark the whole continuation unreachable
                # and mask later alarms.
                r = iv.join(r, iv.TOP)
        elif op == "%":
            r, may_zero = iv.mod(a, b)
            if may_zero:
                alarms.append(self._div_alarm(ins, b))
                r = iv.join(r, iv.TOP)
        else:
            raise AssertionError(f"bad binop {op}")
        return env.set_var(ins.dst, r), alarms

    # ------------------------------------------------------------- driver
    def analyze(self, trace=False):
        # states[bid]         = latest ENTRY environment
        # self._edge_states[(u,v)] = latest environment on edge u -> v
        states: Dict[int, Env] = {b.id: BOT_ENV for b in self.blocks}
        self._edge_states: Dict[tuple, Env] = {}

        # ---------------- ascending: worklist with widening at headers -----
        worklist = [self.cfg.entry]
        iters = 0
        while worklist:
            bid = worklist.pop(0)
            iters += 1
            env_in = self._join_incoming(bid, states, widening=True)
            states[bid] = env_in
            _, edges, _, _ = self.transfer_block(self.blocks[bid], env_in)
            for succ, env in edges.items():
                key = (bid, succ)
                if self._edge_states.get(key) != env:
                    self._edge_states[key] = env
                    if succ not in worklist:
                        worklist.append(succ)
        self._asc_iterations = iters

        # ---------------- descending: plain-join refinement rounds ---------
        narrow_rounds = self._descending(states)

        # ---------------- final alarm-collection pass ----------------------
        result = AnalysisResult()
        result.iterations = iters
        result.narrow_rounds = narrow_rounds
        result.headers = sorted(self.headers)
        self._collect(states, result)
        return result

    def _join_incoming(self, bid, states, widening):
        """Join all incoming edges (plus the external entry env for the CFG
        entry block), widening at loop headers only over variables that the
        header's loop actually modifies."""
        acc = self.entry_env if bid == self.cfg.entry else BOT_ENV
        for p in self.preds[bid]:
            acc = acc.join(self._edge_states.get((p, bid), BOT_ENV))
        if widening and bid in self.headers and not states[bid].is_bottom():
            loop_vars = self.cfg.loop_modified.get(bid, set())
            acc = states[bid].widen_selective(acc, loop_vars)
        return acc

    def _descending(self, states):
        """Classical descending (narrowing) iterations.

        Ascending iteration ends at a widened post-fixpoint with the edge map
        fully populated. Each descending reverse-post-order sweep recomputes
        every block entry from its incoming edges and, at loop headers,
        intersects via interval *narrowing* (only infinite bounds may be
        pulled in). ``Env.narrow`` additionally guarantees the result stays a
        sub-state of the current sound post-fixpoint, so each sweep is a
        smaller post-fixpoint — nested headers are handled by sweeping in RPO
        over a few rounds until nothing changes.
        """
        rounds = 0
        for _ in range(NARROW_ROUNDS):
            rounds += 1
            changed = False
            for bid in self.cfg.rpo:
                joined = self._join_incoming_from(self._edge_states, bid)
                if bid in self.headers:
                    candidate = states[bid].narrow(joined)
                else:
                    candidate = joined
                if candidate == states[bid]:
                    continue
                states[bid] = candidate
                changed = True
                _, edges, _, _ = self.transfer_block(self.blocks[bid], candidate)
                for succ, env in edges.items():
                    self._edge_states[(bid, succ)] = env
            if not changed:
                break
        return rounds

    def _join_incoming_from(self, edge_states, bid):
        acc = self.entry_env if bid == self.cfg.entry else BOT_ENV
        for p in self.preds[bid]:
            acc = acc.join(edge_states.get((p, bid), BOT_ENV))
        return acc

    # ------------------------------------------------------------- collect
    def _collect(self, states, result):
        alarm_keys = set()
        for bid in self.rpo:
            env_in = states[bid]
            result.entry_states[bid] = env_in
            if env_in.is_bottom():
                result.exit_states[bid] = BOT_ENV
                continue
            env_out, edges, alarms, points = self.transfer_block(
                self.blocks[bid], env_in, collect=True)
            result.exit_states[bid] = env_out
            for al in alarms:
                key = (al["kind"], al["subkind"],
                       al["loc"]["line"], al["loc"]["col"])
                if key not in alarm_keys:
                    alarm_keys.add(key)
                    result.alarms.append(al)
            for p in points:
                result.points.append(p)
            # per-block point
            blk = self.blocks[bid]
            result.points.append({
                "kind": "block_entry",
                "block": bid,
                "loc": blk.loc.to_dict() if blk.loc else None,
                "env": self._public_env(env_in).to_dict(),
            })
            for succ, env in edges.items():
                self._edge_states[(bid, succ)] = env

    def _public_env(self, env):
        if env.is_bottom():
            return env
        keep = {k: v for k, v in env.vars.items() if k in self.user_names}
        return Env(keep, dict(env.arrays))

    def _point(self, blk, ins, env, phase):
        return {
            "kind": type(ins).__name__,
            "block": blk.id,
            "loc": ins.loc.to_dict() if getattr(ins, "loc", None) else None,
            "phase": phase,
            "env": self._public_env(env).to_dict(),
        }

    def _div_alarm(self, ins, divisor):
        lo, hi = divisor if not iv.is_bot(divisor) else (0, 0)
        definite = divisor == iv.const(0)
        return {
            "kind": "div_by_zero",
            "subkind": "definite_zero_divisor" if definite else "zero_divisor_possible",
            "severity": "definite" if definite else "possible",
            "loc": ins.loc.to_dict(),
            "divisor": ae._iv_json(divisor),
            "message": ("right operand of division is always zero"
                        if definite else
                        "right operand of division may be zero"),
        }

    def _oob_alarms(self, ins, idx, size):
        if iv.is_bot(idx):
            return []
        lo, hi = idx
        out = []
        if lo is None or lo < 0:
            definite_neg = hi is not None and hi < 0
            out.append({
                "kind": "index_out_of_bounds",
                "subkind": "negative_index",
                "severity": "definite" if definite_neg else "possible",
                "loc": ins.loc.to_dict(),
                "size": size,
                "index": ae._iv_json(idx),
                "message": ("array index is always negative"
                            if definite_neg else "array index may be negative"),
            })
        if hi is None or hi >= size:
            definite_hi = lo is not None and lo >= size
            if definite_hi:
                out.append({
                    "kind": "index_out_of_bounds",
                    "subkind": "index_too_large",
                    "severity": "definite",
                    "loc": ins.loc.to_dict(),
                    "size": size,
                    "index": ae._iv_json(idx),
                    "message": f"array index is always >= array size {size}",
                })
            else:
                out.append({
                    "kind": "index_out_of_bounds",
                    "subkind": "index_too_large_possible",
                    "severity": "possible",
                    "loc": ins.loc.to_dict(),
                    "size": size,
                    "index": ae._iv_json(idx),
                    "message": f"array index may be >= array size {size}",
                })
        return out


def analyze_cfg(cfg, entry_env):
    return Analyzer(cfg, entry_env).analyze()
