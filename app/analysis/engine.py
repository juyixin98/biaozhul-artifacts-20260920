"""Interproducible may-taint analysis with a bounded call-string fixpoint.

Context sensitivity
-------------------
* Calls are distinguished by a *call string* of function names
  ("main > parse > render", length up to ``CALL_STRING_K``).  Different
  callers of the same callee therefore do not taint each other; recursive
  cycles share one context once the string repeats.
* Longer call strings are truncated (oldest frame dropped).  Truncation only
  *merges* contexts, and merges join abstract states, so precision is lost but
  no taint is dropped (sound for a may-analysis).
* The analysis is FLOW-SENSITIVE within a function (per-edge environments on
  an explicit CFG) and CONTEXT-SENSITIVE across functions; it is NOT
  object/field-sensitive (the language has no aggregates) and NOT
  path-sensitive beyond validator-guard refinement.

Fixpoint
--------
Monotone framework: environments live on CFG edges, parameter environments
and return values live per context, everything moves only upward on the
finite lattice CLEAN < MAYBE < TAINT with bounded provenance chains.  A
worklist re-runs nodes whose inputs grew; termination is guaranteed by the
finite lattice (number of contexts and chains is bounded).
"""

from __future__ import annotations

from dataclasses import dataclass, field

from ..language import (
    Assign,
    BinaryOp,
    BoolLit,
    Call,
    Expr,
    ExprStmt,
    FuncDecl,
    IfStmt,
    IntLit,
    NilLit,
    Program,
    ReturnStmt,
    StrLit,
    UnaryOp,
    VarDecl,
    Variable,
    WhileStmt,
    walk_expr,
)
from .cfg import Cfg, CfgNode, build_cfg
from .constant_fold import const_truth
from .constraints import sanitized_by_condition
from .evidence import Chain, HOP_CAP, Hop, Value, join_envs
from .policy import BuiltinSpec, classify
from .taint import Taint

CALL_STRING_K = 3
ENTRY_LABEL = "<entry>"


# -- per-context state -------------------------------------------------------

@dataclass
class ContextState:
    key: tuple[str, ...]
    function: str
    cfg: Cfg
    params: tuple[str, ...]
    in_env: dict[str, Value]                 # environment at function entry
    edge_envs: dict[int, dict[str, Value]]   # eid -> env carried on the edge
    return_value: Value = field(default_factory=Value.bottom)
    return_lines: set[int] = field(default_factory=set)
    #: call-site token -> (argument values bound at this call site, location)
    callsite_bindings: dict[object, tuple[tuple[Value, ...], tuple]] = (
        field(default_factory=dict)
    )


@dataclass(frozen=True)
class SinkRecord:
    sink: str
    function: str
    context: str
    line: int
    col: int
    arg_index: int
    value: Value


@dataclass
class AnalysisResult:
    findings: list[SinkRecord]
    contexts: list[str]
    warnings: list[str]
    iterations: int
    reachable_functions: list[str]


# -- engine ------------------------------------------------------------------

class TaintEngine:
    def __init__(self, program: Program, policy: dict[str, BuiltinSpec],
                 call_string_k: int = CALL_STRING_K):
        self.program = program
        self.policy = policy
        self.k = call_string_k
        self.cfgs: dict[str, Cfg] = {
            name: build_cfg(fn) for name, fn in program.functions.items()
        }
        self.contexts: dict[tuple[str, ...], ContextState] = {}
        # callee context -> call sites waiting on its return value
        self.return_waiters: dict[tuple[str, ...], set[tuple]] = {}
        self.sink_records: dict[tuple, SinkRecord] = {}
        self.warnings: set[str] = set()
        self.worklist: list[tuple[tuple[str, ...], int]] = []
        self.queued: set[tuple[tuple[str, ...], int]] = set()
        self.started_contexts: set[tuple[str, ...]] = set()
        self.iterations = 0

    # -- public entry ------------------------------------------------------
    def analyze(self) -> AnalysisResult:
        entry_fn = self.program.functions[self.program.entry]
        entry_key = (ENTRY_LABEL, self.program.entry)
        ctx = self._make_context(entry_key, entry_fn)
        # The entry function receives no external arguments: its formal
        # parameters are modeled as clean nil (the language's loose default).
        ctx.in_env = {p: Value.clean() for p in ctx.params}
        self.started_contexts.add(entry_key)
        self._enqueue(entry_key, ctx.cfg.entry)
        self._run()
        return self._build_result()

    # -- contexts ----------------------------------------------------------
    def _make_context(self, key: tuple[str, ...], fn: FuncDecl) -> ContextState:
        cfg = self.cfgs[fn.name]
        ctx = ContextState(
            key=key,
            function=fn.name,
            cfg=cfg,
            params=tuple(fn.params),
            # Parameters start at BOTTOM (unbound); the first concrete call
            # binds them.  Initializing them CLEAN would wrongly join a
            # "clean path" against a tainted argument and yield MAYBE.
            in_env={p: Value.bottom() for p in fn.params},
            edge_envs={},
        )
        self.contexts[key] = ctx
        self.return_waiters[key] = set()
        return ctx

    def _child_key(self, parent: tuple[str, ...], callee: str) -> tuple[str, ...]:
        key = parent + (callee,)
        if len(key) > self.k + 1:  # +1 for the ENTRY_LABEL frame
            key = (ENTRY_LABEL,) + key[-(self.k):]
            self.warnings.add(
                f"call string truncated at depth {self.k}; contexts merged "
                f"(may cause false positives, never false negatives)"
            )
        return key

    # -- worklist ----------------------------------------------------------
    def _enqueue(self, key: tuple[str, ...], nid: int) -> None:
        item = (key, nid)
        if item not in self.queued:
            self.queued.add(item)
            self.worklist.append(item)

    def _node_input(self, ctx: ContextState, node: CfgNode) -> dict[str, Value]:
        """Join the environments on all incoming edges of a node."""
        env: dict[str, Value] | None = None
        for n in ctx.cfg.nodes.values():
            for edge in n.edges:
                if edge.target == node.nid:
                    edge_env = ctx.edge_envs.get(edge.eid)
                    # missing edge = not processed yet; empty {} = bottom
                    # (provably unreachable); neither contributes flow.
                    if edge_env:
                        env = dict(edge_env) if env is None else join_envs(env, edge_env)
        if env is None:
            env = dict(ctx.in_env) if not _has_incoming(ctx, node) else {}
        return env

    def _put_edge(self, ctx: ContextState, eid: int,
                  env: dict[str, Value]) -> bool:
        old = ctx.edge_envs.get(eid)
        if old is None:
            ctx.edge_envs[eid] = dict(env)
            return True
        merged = join_envs(old, env)
        if _env_grew(old, merged):
            ctx.edge_envs[eid] = merged
            return True
        return False

    def _run(self) -> None:
        while self.worklist:
            key, nid = self.worklist.pop(0)
            self.queued.discard((key, nid))
            self.iterations += 1
            ctx = self.contexts.get(key)
            if ctx is None:
                continue
            node = ctx.cfg.nodes.get(nid)
            if node is None:
                continue
            # transfer returns the edges whose environment actually grew;
            # only those edges re-activate their successor.  Bottom (empty
            # env, i.e. unreachable) never propagates flow.
            for edge in self._transfer(ctx, node):
                env = ctx.edge_envs.get(edge.eid)
                if env:
                    self._enqueue(key, edge.target)

    # -- transfer ----------------------------------------------------------
    def _transfer(self, ctx: ContextState, node: CfgNode) -> list:
        """Return the edges whose output environment changed."""
        inp = self._node_input(ctx, node)
        if node.kind == "simple":
            return self._transfer_simple(ctx, node, inp)
        if node.kind == "join" or node.kind == "exit":
            return self._transfer_passthrough(ctx, node, inp)
        if node.kind == "branch":
            return self._exec_branch(ctx, node, inp)
        if node.kind == "loop":
            return self._exec_loop(ctx, node, inp)
        return []  # pragma: no cover

    def _transfer_simple(self, ctx: ContextState, node: CfgNode,
                         inp: dict[str, Value]) -> list:
        out = dict(inp)
        out, ret = self._exec_simple(ctx, node, out)
        if ret is not None:
            self._update_return(ctx, ret, node)
        return self._write_edges(ctx, node, {e.eid: out for e in node.edges})

    def _transfer_passthrough(self, ctx: ContextState, node: CfgNode,
                              inp: dict[str, Value]) -> list:
        return self._write_edges(
            ctx, node, {e.eid: inp for e in node.edges}
        )

    def _write_edges(self, ctx: ContextState, node: CfgNode,
                     outputs: dict[int, dict[str, Value]]) -> list:
        changed_edges = []
        for edge in node.edges:
            if edge.eid in outputs and self._put_edge(ctx, edge.eid,
                                                      outputs[edge.eid]):
                changed_edges.append(edge)
        return changed_edges
    def _exec_simple(self, ctx: ContextState, node: CfgNode,
                     env: dict[str, Value]):
        stmt = node.stmt
        ret: Value | None = None
        if isinstance(stmt, VarDecl):
            if stmt.init is not None:
                val = self.eval(stmt.init, env, ctx, node.nid)
                val = _defined_or_bottom(stmt.init, val)
                if val.level is not Taint.BOTTOM:
                    val = val.add_hop(_hop(
                        "assign", f"var {stmt.name}", ctx, stmt.loc.line
                    ))
                env[stmt.name] = val
            else:
                env[stmt.name] = Value.clean()
        elif isinstance(stmt, Assign):
            val = self.eval(stmt.value, env, ctx, node.nid)
            val = _defined_or_bottom(stmt.value, val)
            if val.level is not Taint.BOTTOM:
                val = val.add_hop(_hop(
                    "assign", f"{stmt.name} = ...", ctx, stmt.loc.line
                ))
            env[stmt.name] = val
        elif isinstance(stmt, ExprStmt):
            self.eval(stmt.expr, env, ctx, node.nid)
        elif isinstance(stmt, ReturnStmt):
            if stmt.value is not None:
                val = self.eval(stmt.value, env, ctx, node.nid)
                if val.level is Taint.BOTTOM:
                    ret = _defined_or_bottom(stmt.value, val)
                else:
                    ret = val.add_hop(
                        Hop("return", f"return from {ctx.function}",
                            ctx.function, stmt.loc.line, stmt.loc.col)
                    )
            else:
                ret = Value.clean()
        return env, ret

    def _exec_branch(self, ctx: ContextState, node: CfgNode,
                     inp: dict[str, Value]) -> list:
        stmt: IfStmt = node.stmt  # type: ignore[assignment]
        # Fully evaluate the condition: sources/propagators and sinks in the
        # condition are analyzed just like ordinary expressions.
        self.eval(stmt.cond, inp, ctx, node.nid)
        truth = const_truth(stmt.cond, self.policy)
        then_clean = sanitized_by_condition(stmt.cond, True, self.policy)
        else_clean = sanitized_by_condition(stmt.cond, False, self.policy)
        outputs: dict[int, dict[str, Value]] = {}
        for edge in node.edges:
            unreachable = (
                (edge.tag == "then" and truth is False)
                or (edge.tag == "else" and truth is True)
            )
            if unreachable:
                outputs[edge.eid] = {}  # bottom -- never propagates
            elif edge.tag == "then":
                outputs[edge.eid] = _refine(inp, then_clean)
            else:
                outputs[edge.eid] = _refine(inp, else_clean)
        return self._write_edges(ctx, node, outputs)

    def _exec_loop(self, ctx: ContextState, node: CfgNode,
                   inp: dict[str, Value]) -> list:
        """While-loop transfer returning changed output edges.

        Two distinct inputs matter:

        * ``first`` -- environment reaching the loop header from OUTSIDE
          (forward edges only); governs the case where the body never runs.
        * ``iter_env`` -- join of ``first`` with every back-edge environment;
          governs iterations after the body ran at least once.

        The body edge carries ``iter_env`` (bottom when no back flow exists
        and the header was entered only from outside).  The exit edge carries
        the join of "exited before first iteration" and "exited after
        iterations", both refined on the false guard.
        """
        stmt: WhileStmt = node.stmt  # type: ignore[assignment]
        first = self._forward_input(ctx, node)
        back = self._backedge_input(ctx, node)
        iter_env = join_envs(first, back)
        self.eval(stmt.cond, iter_env, ctx, node.nid)
        truth = const_truth(stmt.cond, self.policy)
        body_clean = sanitized_by_condition(stmt.cond, True, self.policy)
        exit_clean = sanitized_by_condition(stmt.cond, False, self.policy)
        body_env = _refine(iter_env, body_clean)
        exit_env = join_envs(
            _refine(first, exit_clean),
            _refine(iter_env, exit_clean),
        )
        outputs: dict[int, dict[str, Value]] = {}
        for edge in node.edges:
            unreachable = (
                (edge.tag == "body" and truth is False)
                or (edge.tag == "loop-exit" and truth is True)
            )
            if unreachable:
                outputs[edge.eid] = {}
            elif edge.tag == "body":
                outputs[edge.eid] = body_env
            else:
                outputs[edge.eid] = exit_env
        return self._write_edges(ctx, node, outputs)

    def _forward_input(self, ctx: ContextState, node: CfgNode) -> dict[str, Value]:
        env = self._collect_incoming(ctx, node, back_edges=False)
        if env is None:
            return dict(ctx.in_env) if node.nid == ctx.cfg.entry else {}
        return env

    def _backedge_input(self, ctx: ContextState, node: CfgNode) -> dict[str, Value]:
        env = self._collect_incoming(ctx, node, back_edges=True)
        return env if env is not None else {}

    def _collect_incoming(
        self, ctx: ContextState, node: CfgNode, back_edges: bool
    ) -> dict[str, Value] | None:
        env: dict[str, Value] | None = None
        for n in ctx.cfg.nodes.values():
            for edge in n.edges:
                if edge.target != node.nid:
                    continue
                if edge.back_edge != back_edges:
                    continue
                edge_env = ctx.edge_envs.get(edge.eid)
                if edge_env:
                    env = dict(edge_env) if env is None else join_envs(env, edge_env)
        return env

    def _update_return(self, ctx: ContextState, val: Value,
                       node: CfgNode) -> bool:
        # Normalize before aggregating: a function's return summary must not
        # contain hops that identify a particular caller ("call-ret"),
        # otherwise recursive call strings can grow without bound and the
        # interprocedural fixpoint would not terminate.
        val = _normalize_value(val)
        before = ctx.return_value
        ctx.return_value = Value.join(before, val)
        if node.stmt is not None:
            ctx.return_lines.add(node.stmt.loc.line)
        grew = ctx.return_value != before
        if grew:
            # re-run every caller node waiting on this context's return
            for caller_key, caller_nid, _token in self.return_waiters[ctx.key]:
                self._enqueue(caller_key, caller_nid)
        return grew

    # -- expression evaluation --------------------------------------------
    def eval(self, expr: Expr, env: dict[str, Value],
             ctx: ContextState, node_id: int, call_idx: int = 0) -> Value:
        """Abstract-evaluate; call_idx identifies calls within one statement."""
        if isinstance(expr, (IntLit, StrLit, BoolLit, NilLit)):
            # A literal is a defined-clean constant.  In value combination
            # (combine) it behaves neutrally toward taint, but as a variable's
            # initializer it is a genuine clean path.
            return Value.clean()
        if isinstance(expr, Variable):
            # Variables present in env keep their exact level -- including
            # BOTTOM for a not-yet-bound parameter.  Truly undeclared names
            # follow the language's loose semantics (clean nil) and warn.
            if expr.name in env:
                return env[expr.name]
            if expr.name not in ctx.cfg.locals \
                    and expr.name not in self.program.functions \
                    and classify(self.policy, expr.name) is None:
                self.warnings.add(
                    f"{ctx.function}:{expr.loc.line}: use of undeclared "
                    f"variable {expr.name!r} (treated as clean nil)"
                )
            return Value.clean()
        if isinstance(expr, UnaryOp):
            return self.eval(expr.operand, env, ctx, node_id, call_idx)
        if isinstance(expr, BinaryOp):
            return self._eval_binop(expr, env, ctx, node_id)
        if isinstance(expr, Call):
            return self._eval_call(expr, env, ctx, node_id)
        raise TypeError(f"unknown expr {type(expr).__name__}")  # pragma: no cover

    def _eval_binop(self, expr: BinaryOp, env: dict[str, Value],
                    ctx: ContextState, node_id: int) -> Value:
        left = self.eval(expr.left, env, ctx, node_id)
        right = self.eval(expr.right, env, ctx, node_id)
        cmp_ops = {"==", "!=", "<", "<=", ">", ">="}
        if expr.op in cmp_ops:
            # Comparisons produce clean booleans; taint still flows through
            # validator-guard refinement, not through the boolean value.
            return Value.clean()
        merged = Value.combine(left, right)
        # BOTTOM survives only when the expression reads a not-yet-bound
        # variable; otherwise constants combine to a defined-clean value.
        if merged.level is Taint.BOTTOM and not (
            _expr_depends_on_unbound(expr.left, env)
            or _expr_depends_on_unbound(expr.right, env)
        ):
            return Value.clean()
        if merged.level in (Taint.BOTTOM, Taint.CLEAN):
            return merged
        return merged.add_hop(
            Hop("binop", expr.op, ctx.function, expr.loc.line, expr.loc.col)
        )

    def _eval_call(self, call: Call, env: dict[str, Value],
                   ctx: ContextState, node_id: int) -> Value:
        # Depth-first: sinks nested in arguments are checked while evaluating.
        arg_vals = [
            self.eval(a, env, ctx, node_id, i) for i, a in enumerate(call.args)
        ]
        kind = classify(self.policy, call.callee)
        loc = call.loc
        if kind == "source":
            chain = Chain(hops=(
                Hop("source", call.callee, ctx.function, loc.line, loc.col),
            ))
            return Value.tainted(frozenset({chain}))
        if kind == "sanitizer":
            return Value.clean()
        if kind == "validator":
            # A validator standing alone (e.g. assigned to a variable) yields
            # a clean boolean; its arguments are NOT cleaned by the call value.
            return Value.clean()
        if kind == "pure":
            return Value.clean()
        if kind == "sink":
            self._record_sink(call, arg_vals, ctx)
            return Value.clean()
        if kind == "propagator":
            spec = self.policy[call.callee]
            if spec.propagate_args:
                picked_idx = [i for i in spec.propagate_args]
            else:
                picked_idx = list(range(len(call.args)))
            # combine is single-path: literal/clean operands never downgrade a
            # tainted operand to MAYBE, and a BOTTOM (not-ready) operand keeps
            # the result BOTTOM.
            result: Value | None = None
            for i in picked_idx:
                if i < len(arg_vals):
                    result = arg_vals[i] if result is None else Value.combine(
                        result, arg_vals[i]
                    )
            if result is None:
                result = Value.clean()
            if result.level in (Taint.TAINT, Taint.MAYBE):
                result = result.add_hop(
                    Hop("propagator", call.callee,
                        ctx.function, loc.line, loc.col)
                )
            return result
        return self._eval_user_call(call, arg_vals, ctx, node_id)

    def _eval_user_call(
        self, call: Call, arg_vals: list[Value],
        ctx: ContextState, node_id: int,
    ) -> Value:
        callee = call.callee
        if callee not in self.program.functions:
            self.warnings.add(
                f"{ctx.function}:{call.loc.line}: call to undefined function "
                f"{callee!r} -- result assumed TAINTED (conservative)"
            )
            chain = Chain(hops=(
                Hop("unknown-fn", callee, ctx.function,
                    call.loc.line, call.loc.col),
            ))
            return Value.tainted(frozenset({chain}))
        callee_fn = self.program.functions[callee]
        target_key = self._child_key(ctx.key, callee)
        target = self.contexts.get(target_key)
        if target is None:
            target = self._make_context(target_key, callee_fn)

        # 1) bind arguments into the (monotone) parameter environment
        bound_env: dict[str, Value] = {}
        for i, param in enumerate(callee_fn.params):
            if i < len(arg_vals):
                v = arg_vals[i].add_hop(
                    Hop("param", f"{ctx.function} -> {callee}.{param}",
                        callee, callee_fn.param_locs[i].line
                        if i < len(callee_fn.param_locs) else call.loc.line)
                )
            else:
                v = Value.clean()
            bound_env[param] = v
        old_in = target.in_env
        new_in = join_envs(old_in, bound_env) if target.in_env else bound_env
        first_time = target_key not in self.started_contexts
        if first_time:
            self.started_contexts.add(target_key)
        if first_time or _env_grew(old_in, new_in):
            target.in_env = new_in
            # Parameters are read at entry; re-run from the top.
            self._enqueue(target_key, target.cfg.entry)

        # 2) depend on the callee's (monotone) aggregated return value.
        # The returned chains are filtered by THIS call site's arguments:
        # taint from another caller's arguments must not flow to us.
        token = (node_id, call.loc.line, call.loc.col, len(call.args))
        ctx.callsite_bindings[token] = (tuple(arg_vals), ctx.key)
        self.return_waiters[target_key].add((ctx.key, node_id, token))
        result = self._callsite_result(ctx, target, token, call)
        return result

    def _callsite_result(
        self,
        caller: ContextState,
        callee: ContextState,
        token: object,
        call: Call,
    ) -> Value:
        """Map the callee's return value onto one call site's provenance."""
        ret = callee.return_value
        if ret.level is Taint.BOTTOM:
            # Callee not analyzed to a return yet: the result is unknown
            # (bottom), NOT clean.  Returning clean here would plant a
            # spurious clean path into the caller's arguments on the first
            # chaotic pass; the worklist re-runs us once ret grows.
            return Value.bottom()
        if ret.level is Taint.CLEAN or not ret.chains:
            return Value.clean()
        bound, _key = caller.callsite_bindings[token]
        # Which parameters are tainted AT THIS call site.  Taint from a
        # different caller's arguments must not flow to us.
        tainted_args = {
            callee.params[i]
            for i, v in enumerate(bound)
            if i < len(callee.params) and v.level in (Taint.TAINT, Taint.MAYBE)
        }
        kept: set[Chain] = set()
        for chain in ret.chains:
            param_hop = next(
                (h for h in chain.hops if h.kind == "param"), None
            )
            if param_hop is None:
                # Source reached directly inside the callee: propagates to
                # every caller.  Internal "return"/"call-ret" hops are
                # caller-independent and stay attached.
                kept.add(chain)
                continue
            pname = param_hop.detail.rsplit(".", 1)[-1]
            if pname not in tainted_args:
                continue
            # Rebase: prefix with THIS argument's chains, suffix with the
            # callee-internal hops that followed the parameter binding.
            idx = chain.hops.index(param_hop)
            tail = tuple(h for h in chain.hops[idx + 1 :]
                         if h.kind != "call-ret")
            arg_index = callee.params.index(pname)
            for source_chain in bound[arg_index].chains:
                merged = Chain(
                    hops=source_chain.hops + tail,
                    truncated=chain.truncated or source_chain.truncated,
                )
                kept.add(_normalize_chain(merged))
        if not kept:
            return Value.clean()
        # Preserve whether the callee's return is definite-tainted or only
        # conditionally tainted (MAYBE) at this call site.
        result = Value(level=ret.level, chains=frozenset(kept))
        # Exactly one caller-specific hop per use -- never fed back into the
        # callee's return value, so it cannot participate in a growing cycle.
        return result.add_hop(
            Hop("call-ret", f"{caller.function} calls {callee.function}",
                caller.function, call.loc.line, call.loc.col)
        )

    def _record_sink(self, call: Call, arg_vals: list[Value],
                     ctx: ContextState) -> None:
        for i, v in enumerate(arg_vals):
            if v.level not in (Taint.TAINT, Taint.MAYBE):
                continue
            key = (ctx.key, call.callee, call.loc.line, call.loc.col, i)
            existing = self.sink_records.get(key)
            if existing is not None:
                v = Value.join(existing.value, v)
            self.sink_records[key] = SinkRecord(
                sink=call.callee,
                function=ctx.function,
                context=_context_label(ctx.key),
                line=call.loc.line,
                col=call.loc.col,
                arg_index=i,
                value=v,
            )

    # -- result ------------------------------------------------------------
    def _build_result(self) -> AnalysisResult:
        findings = sorted(
            self.sink_records.values(),
            key=lambda r: (r.line, r.col, r.sink, r.context),
        )
        reachable = {c.function for c in self.contexts.values()}
        return AnalysisResult(
            findings=findings,
            contexts=[_context_label(c.key) for c in self.contexts.values()],
            warnings=sorted(self.warnings),
            iterations=self.iterations,
            reachable_functions=sorted(reachable),
        )


# -- helpers -----------------------------------------------------------------

def _hop(kind: str, detail: str, ctx: ContextState, line: int = 0) -> Hop:
    return Hop(kind, detail, ctx.function, line)


def _expr_depends_on_unbound(expr: Expr, env: dict[str, Value]) -> bool:
    """True if any variable read by ``expr`` is currently BOTTOM."""
    for sub in walk_expr(expr):
        if isinstance(sub, Variable) and sub.name in env:
            if env[sub.name].level is Taint.BOTTOM:
                return True
    return False


def _is_constant_expr(expr: Expr) -> bool:
    """True for an expression built only from literals (no calls/variables)."""
    for sub in walk_expr(expr):
        if not isinstance(sub, (IntLit, StrLit, BoolLit, NilLit, BinaryOp,
                                UnaryOp)):
            return False
    return True


def _defined_or_bottom(expr: Expr, val: Value) -> Value:
    """Materialize a BOTTOM expression result.

    A pure literal constant is a *defined-clean* value; a BOTTOM that came
    from an unbound variable or a not-yet-ready user call must stay BOTTOM so
    it cannot seed a spurious clean arm in the fixpoint.
    """
    if val.level is Taint.BOTTOM and _is_constant_expr(expr):
        return Value.clean()
    return val


def _refine(env: dict[str, Value], clean_names: set[str]) -> dict[str, Value]:
    if not clean_names:
        return dict(env)
    out = dict(env)
    for name in clean_names:
        if name in out and out[name].level in (Taint.TAINT, Taint.MAYBE):
            # On the validator-true branch the governed value is trusted and
            # becomes defined-clean.
            out[name] = Value.clean()
    return out


def _has_incoming(ctx: ContextState, node: CfgNode) -> bool:
    return any(
        edge.target == node.nid
        for n in ctx.cfg.nodes.values()
        for edge in n.edges
    ) and node.nid != ctx.cfg.entry


def _env_grew(old: dict[str, Value], new: dict[str, Value]) -> bool:
    if old.keys() != new.keys():
        return True
    for k, v in new.items():
        if old[k] != v:
            return True
    return False


def _normalize_chain(chain: Chain) -> Chain:
    """Drop caller-specific / redundant hops from a return summary chain."""
    out: list[Hop] = []
    for hop in chain.hops:
        if hop.kind == "call-ret":
            continue
        # collapse adjacent identical hops (param/return repeated by
        # recursive fixpoint iterations)
        if out and out[-1] == hop:
            continue
        out.append(hop)
    normalized = tuple(out)
    if len(normalized) > HOP_CAP:
        normalized = normalized[:HOP_CAP]
        return Chain(hops=normalized, truncated=True)
    return Chain(hops=normalized, truncated=chain.truncated)


def _normalize_value(v: Value) -> Value:
    if v.level is Taint.CLEAN:
        return v
    chains = frozenset(_normalize_chain(c) for c in v.chains)
    return Value(level=v.level, chains=chains)


def _context_label(key: tuple[str, ...]) -> str:
    return " > ".join(key)
