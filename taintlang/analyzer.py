"""Interprocedural taint analysis on the custom IR.

Design summary (see README "Approximations" for the complete list):

* **May dataflow, flow-sensitive within a context.**  Environments map
  registers to sets of taint *origins*.  Joins are set unions; there is no
  strong update, so an assignment that ever held taint keeps it.
* **Finite call-string contexts (k-CFA style).**  A context is the last
  ``k`` call-site identifiers on the call stack.  ``k=None`` is bounded by
  ``max_call_sites`` instead.  Once the bound is reached older frames are
  dropped, which merges callers — sound, but a source of false alarms.
* **No precomputed summaries.**  Every (function, context) pair is solved by
  one global monotone worklist fixpoint over an interprocedural supergraph;
  the per-context entry->exit taint relation *is* the bounded-context
  summary.
* **Branches are not interpreted.**  Both successors of ``Br`` are explored,
  so taint sanitized on only one branch is still tainted at the merge — the
  intentional "conditional sanitization" false alarm.
* **Opaque calls.**  A call to a name without a function body taints the
  result iff any argument is tainted.
* **Sanitizers are strong cleaners** modelled exactly: the result register
  of ``sanitize(x)`` never carries taint from ``x``.

The fixpoint also grows a monotone *flow graph* (nodes are instruction
occurrences, block entries, parameter bindings, call/return joins; edges are
observed taint propagations).  Findings and source->sink paths are
reconstructed after the fixpoint by bounded reverse DFS on that graph.
"""

from __future__ import annotations

from collections import defaultdict, deque
from dataclasses import dataclass
from typing import Optional

from .config import Config
from .errors import BuildError
from .ir import (
    BinOp, Block, Br, CallTerm, Const, Copy, FunctionIR, Jmp, ProgramIR, Ret,
    SinkInstr, SanitizeInstr, SourceInstr, UnOp, UnknownCall,
)

ROOT_CTX: tuple = ()


@dataclass(frozen=True)
class Origin:
    oid: str
    kind: str           # marker name, or "entry-parameter"
    func: str
    instr_uid: Optional[int]
    span: dict
    detail: str


class Analyzer:
    def __init__(self, program: ProgramIR, config: Config,
                 tainted_entry_params: bool = True):
        self.program = program
        self.config = config
        self.tainted_entry_params = tainted_entry_params

        # (func, block, ctx) -> {reg: frozenset[oid]}
        self.env: dict[tuple, dict[str, frozenset[str]]] = {}
        # monotone flow graph
        self.preds: dict[tuple, set[tuple]] = defaultdict(set)
        # origin tags per edge: which taint origins that edge propagated.
        # Control-only edges carry an empty tag and can therefore never
        # appear in a reconstructed taint path.
        self.edge_tags: dict[tuple, frozenset[str]] = {}
        self.node_meta: dict[tuple, dict] = {}
        # origin registry
        self.origins: dict[str, Origin] = {}
        # sinks
        self.sink_hits: dict[int, set[str]] = defaultdict(set)
        self.sink_all: dict[int, dict] = {}
        # opaque calls
        self.unknown_calls: list[dict] = []
        self._unknown_seen: set[int] = set()
        # pending returns: (site_uid, callee_ctx) -> info
        self.pending: dict[tuple, dict] = {}
        # contexts: ctx -> {frames, truncated}
        self.contexts: dict[tuple, dict] = {}
        # per (func, ctx) set of origins the function may return
        self.exit_taint: dict[tuple, frozenset[str]] = {}
        # worklist
        self.queue: deque[tuple] = deque()
        self.in_queue: set[tuple] = set()
        self.block_steps = 0

    # ==================================================================
    # graph / context helpers
    # ==================================================================
    def node(self, key: tuple, **meta) -> None:
        if key not in self.node_meta:
            self.node_meta[key] = meta

    def edge(self, src: tuple, dst: tuple,
             origins: frozenset[str] = frozenset()) -> None:
        """Add a flow edge tagged with the origins it propagates.

        An edge created with no origins is control-only and is never used
        when reconstructing a source->sink taint path.  Tags accumulate
        monotonically when the same edge is observed again later.
        """
        if src not in self.node_meta:
            self.node(src)
        if dst not in self.node_meta:
            self.node(dst)
        key = (src, dst)
        self.preds[dst].add(src)
        prev = self.edge_tags.get(key, frozenset())
        self.edge_tags[key] = prev | origins

    def _register_context(self, ctx: tuple, frames: list[tuple]) -> None:
        if ctx not in self.contexts:
            self.contexts[ctx] = {
                "frames": [
                    {"site_uid": s, "caller": a, "callee": b}
                    for s, a, b in frames
                ],
                "truncated": len(frames) > len(ctx),
            }

    def _push_context(self, ctx: tuple, site_uid: int,
                      caller: str, callee: str) -> tuple:
        frames = [tuple(f) for f in self.contexts[ctx]["frames"]]
        frames.append((site_uid, caller, callee))
        if self.config.k is None:
            new_ctx = ctx + (site_uid,)
            if len(self.contexts) >= self.config.max_call_sites:
                new_ctx = ctx  # hard cap: merge with current context
        else:
            k = self.config.k
            new_ctx = (ctx + (site_uid,))[-k:] if k else ROOT_CTX
        self._register_context(new_ctx, frames)
        return new_ctx

    # ==================================================================
    # fixpoint driver
    # ==================================================================
    def analyze(self) -> dict:
        entries = self.config.entry_points or (self.program.functions[0].name,)
        for name in entries:
            if name not in self.program._by_name:
                raise BuildError(f"unknown entry point '{name}'")
        self._register_context(ROOT_CTX, [])

        for name in entries:
            func = self.program.function(name)
            entry_node = ("entry", name, func.entry, ROOT_CTX)
            self.node(entry_node, kind="block-entry", func=name,
                      label=func.entry, ctx=ROOT_CTX, span=func.span.to_dict())
            seed: dict[str, frozenset[str]] = {}
            if self.tainted_entry_params:
                for i, pname in enumerate(func.params):
                    oid = f"param@{name}#{i}"
                    self.origins[oid] = Origin(
                        oid=oid, kind="entry-parameter", func=name,
                        instr_uid=None, span=func.span.to_dict(),
                        detail=f"parameter '{pname}' of entry '{name}'")
                    seed[pname] = frozenset({oid})
                    pn = ("rootparam", name, i)
                    self.node(pn, kind="entry-param", func=name, index=i,
                              name=pname, origin=oid, span=func.span.to_dict())
                    self.edge(pn, entry_node, frozenset({oid}))
            self._enqueue((name, func.entry, ROOT_CTX), seed)

        while self.queue:
            key = self.queue.popleft()
            self.in_queue.discard(key)
            self._process_block(key)

        return self._build_result(entries)

    def _enqueue(self, key: tuple, added: dict[str, frozenset[str]]) -> None:
        cur = self.env.get(key)
        if cur is None:
            merged = {r: frozenset(o) for r, o in added.items() if o}
            self.env[key] = merged
            # every newly reachable block is visited once, even with a
            # completely clean incoming environment (its body might itself
            # introduce sources)
            changed = True
        else:
            merged = dict(cur)
            changed = False
            for reg, origins in added.items():
                if not origins:
                    continue
                old = merged.get(reg, frozenset())
                new = old | origins
                if new != old:
                    merged[reg] = new
                    changed = True
            self.env[key] = merged
        if changed and key not in self.in_queue:
            self.queue.append(key)
            self.in_queue.add(key)

    # ==================================================================
    # block processing
    # ==================================================================
    def _process_block(self, key: tuple) -> None:
        func_name, block_label, ctx = key
        func = self.program.function(func_name)
        block = func.block(block_label)
        env = dict(self.env.get(key, {}))
        self.block_steps += 1

        entry_node = ("entry", func_name, block_label, ctx)
        self.node(entry_node, kind="block-entry", func=func_name,
                  label=block_label, ctx=ctx, span=block.span.to_dict())

        # last_def[reg] = graph node that produced the current value
        last_def: dict[str, tuple] = {reg: entry_node for reg in env}
        last_node: tuple = entry_node

        for instr in block.instructions:
            node = ("instr", instr.uid)
            env, last_def, last_node = self._transfer(
                func_name, ctx, instr, env, last_def, node, last_node)

        term = block.terminator
        if term is None:
            return
        if isinstance(term, Jmp):
            self._wire_jump(func_name, ctx, env, term, last_def, last_node)
        elif isinstance(term, Br):
            self._wire_branch(func_name, ctx, env, term, last_def, last_node)
        elif isinstance(term, Ret):
            self._wire_return(func_name, ctx, env, term, last_def, last_node)
        elif isinstance(term, CallTerm):
            self._wire_call(func_name, ctx, block, env, term, last_def, last_node)
        else:  # pragma: no cover
            raise AssertionError(f"unknown terminator {type(term).__name__}")

    def _wire_value_edges(self, env, last_def, target_entry) -> None:
        """Connect the producer of every tainted outgoing register to the
        entry node of the successor block, so taint arriving through a CFG
        join (including loop back-edges) retains its provenance chain."""
        for reg, origins in env.items():
            if origins and reg in last_def:
                self.edge(last_def[reg], target_entry, origins)

    def _wire_jump(self, func_name, ctx, env, term: Jmp,
                   last_def, last_node) -> None:
        to = ("entry", func_name, term.target, ctx)
        self.node(to, kind="block-entry", func=func_name,
                  label=term.target, ctx=ctx,
                  span=self.program.function(func_name).block(term.target).span.to_dict())
        # control edge (no origins): marks reachability, never a taint step
        self.edge(last_node, to)
        self._wire_value_edges(env, last_def, to)
        self._enqueue((func_name, term.target, ctx), env)

    def _wire_branch(self, func_name, ctx, env, term: Br,
                     last_def, last_node) -> None:
        # Both edges are always considered possible (conditions are not
        # interpreted): the same tainted environment flows to both sides.
        for target, taken in ((term.then_target, True), (term.else_target, False)):
            bn = ("br", func_name, target, ctx)
            self.node(bn, kind="branch", func=func_name, label=target, ctx=ctx,
                      taken=taken, span=term.span.to_dict())
            to = ("entry", func_name, target, ctx)
            self.node(to, kind="block-entry", func=func_name,
                      label=target, ctx=ctx,
                      span=self.program.function(func_name).block(target).span.to_dict())
            # taint values flow *through* the branch node: tag both hops
            for reg, origins in env.items():
                if origins and reg in last_def:
                    self.edge(last_def[reg], bn, origins)
                    self.edge(bn, to, origins)
            self._enqueue((func_name, target, ctx), env)

    def _wire_return(self, func_name, ctx, env, term: Ret,
                     last_def, last_node) -> None:
        exit_node = ("exit", func_name, ctx)
        self.node(exit_node, kind="exit", func=func_name, ctx=ctx,
                  span=term.span.to_dict())
        origins = env.get(term.value, frozenset()) if term.value else frozenset()
        if term.value and origins:
            self.edge(last_def.get(term.value, last_node), exit_node, origins)
        old = self.exit_taint.get((func_name, ctx), frozenset())
        new = old | origins
        if new != old:
            self.exit_taint[(func_name, ctx)] = new
            self._fire_pending(func_name, ctx, new - old, exit_node)

    def _wire_call(self, func_name, ctx, block, env, term: CallTerm,
                   last_def, last_node) -> None:
        arg_nodes = []
        for i, areg in enumerate(term.args):
            an = ("callarg", term.site_uid, i, ctx)
            self.node(an, kind="call-arg", site_uid=term.site_uid, index=i,
                      ctx=ctx, func=func_name, name=areg,
                      span=term.span.to_dict())
            if env.get(areg):
                self.edge(last_def.get(areg, last_node), an, env[areg])
            arg_nodes.append(an)

        if term.callee is None:  # defensive; builder emits UnknownCall instead
            self._continue_call(func_name, ctx, term, env, last_def,
                                frozenset(), None)
            return

        callee = self.program.function(term.callee)
        callee_ctx = self._push_context(ctx, term.site_uid,
                                        func_name, term.callee)

        callee_entry = ("entry", term.callee, callee.entry, callee_ctx)
        self.node(callee_entry, kind="block-entry", func=term.callee,
                  label=callee.entry, ctx=callee_ctx,
                  span=callee.span.to_dict())
        seed: dict[str, frozenset[str]] = {}
        for i, pname in enumerate(callee.params):
            pn = ("param", term.callee, i, callee_ctx)
            self.node(pn, kind="param", func=term.callee, index=i,
                      name=pname, ctx=callee_ctx, span=callee.span.to_dict())
            if env.get(term.args[i]):
                self.edge(arg_nodes[i], pn, env[term.args[i]])
                self.edge(pn, callee_entry, env[term.args[i]])
            seed[pname] = env.get(term.args[i], frozenset())

        pend_key = (term.site_uid, callee_ctx)
        if pend_key not in self.pending:
            self.pending[pend_key] = {
                "snapshot": {r: frozenset(o) for r, o in env.items()},
                "caller_ctx": ctx,
                "caller_func": func_name,
                "callee": term.callee,
                "return_reg": term.return_reg,
                "cont": term.cont,
                "propagated": set(),
                "last_def": dict(last_def),
                "span": term.span,
            }
        else:
            # same call site+context revisited at a later fixpoint round
            # with a richer environment: remember the union of caller locals
            snap = self.pending[pend_key]["snapshot"]
            for reg, origins in env.items():
                if origins:
                    snap[reg] = snap.get(reg, frozenset()) | origins
                    self.pending[pend_key]["last_def"].setdefault(reg, last_def[reg])

        self._enqueue((term.callee, callee.entry, callee_ctx), seed)

        # continuation must proceed even when the callee returns clean, so
        # enqueue the snapshot now; tainted returns re-enqueue later
        self._continue_call(func_name, ctx, term, env, last_def,
                            frozenset(), None)

        prior = self.exit_taint.get((term.callee, callee_ctx))
        if prior:
            exit_node = ("exit", term.callee, callee_ctx)
            self._fire_one(pend_key, prior, exit_node)

    def _continue_call(self, func_name, ctx, term: CallTerm, env,
                       last_def, return_origins, callret_node) -> None:
        cont_entry = ("entry", func_name, term.cont, ctx)
        self.node(cont_entry, kind="block-entry", func=func_name,
                  label=term.cont, ctx=ctx,
                  span=self.program.function(func_name).block(term.cont).span.to_dict())
        # caller locals survive the call unchanged
        for reg, origins in env.items():
            if origins and reg != term.return_reg:
                self.edge(last_def.get(reg, cont_entry), cont_entry, origins)
        if callret_node is not None and return_origins:
            self.edge(callret_node, cont_entry, return_origins)
        merged_env = {r: frozenset(o) for r, o in env.items()}
        if term.return_reg is not None and return_origins:
            merged_env[term.return_reg] = return_origins
        self._enqueue((func_name, term.cont, ctx), merged_env)

    def _fire_pending(self, callee_name, callee_ctx, new_origins,
                      exit_node) -> None:
        for (site_uid, cctx), info in list(self.pending.items()):
            if cctx == callee_ctx and info["callee"] == callee_name:
                self._fire_one((site_uid, cctx), new_origins, exit_node)

    def _fire_one(self, pend_key, origins, exit_node) -> None:
        info = self.pending[pend_key]
        site_uid, callee_ctx = pend_key
        callret = ("callret", site_uid, info["caller_ctx"])
        self.node(callret, kind="call-return", site_uid=site_uid,
                  callee=info["callee"], ctx=info["caller_ctx"],
                  span=info["span"].to_dict())
        new_to_propagate = frozenset(origins) - frozenset(info["propagated"])
        if new_to_propagate:
            self.edge(exit_node, callret, new_to_propagate)
            info["propagated"].update(new_to_propagate)
        term_proxy = _CallCont(
            cont=info["cont"], return_reg=info["return_reg"],
            span=info["span"])
        self._continue_call(
            info["caller_func"], info["caller_ctx"], term_proxy,
            info["snapshot"], info["last_def"], frozenset(origins), callret)

    # ==================================================================
    # instruction transfer
    # ==================================================================
    def _transfer(self, func_name, ctx, instr, env, last_def, node, last_node):
        span = instr.span.to_dict()

        if isinstance(instr, Const):
            self.node(node, kind="const", dst=instr.dst, value=instr.value,
                      func=func_name, span=span)
            env[instr.dst] = frozenset()
            last_def[instr.dst] = node

        elif isinstance(instr, Copy):
            self.node(node, kind="copy", dst=instr.dst, src=instr.src,
                      func=func_name, span=span)
            origins = env.get(instr.src, frozenset())
            env[instr.dst] = origins
            if origins:
                self.edge(last_def.get(instr.src, last_node), node, origins)
            last_def[instr.dst] = node

        elif isinstance(instr, (BinOp, UnOp)):
            if isinstance(instr, BinOp):
                operands = [instr.left, instr.right]
                self.node(node, kind="binop", dst=instr.dst, op=instr.op,
                          func=func_name, span=span)
            else:
                operands = [instr.operand]
                self.node(node, kind="unop", dst=instr.dst, op=instr.op,
                          func=func_name, span=span)
            tainted_ops = [(o, env.get(o, frozenset())) for o in operands]
            origins = frozenset().union(
                *[o for _, o in tainted_ops]) if tainted_ops else frozenset()
            env[instr.dst] = origins
            for reg, reg_origins in tainted_ops:
                if reg_origins:
                    self.edge(last_def.get(reg, last_node), node, reg_origins)
            last_def[instr.dst] = node

        elif isinstance(instr, SourceInstr):
            self.node(node, kind="source", dst=instr.dst, marker=instr.kind,
                      func=func_name, span=span)
            oid = f"src@{instr.uid}"
            self.origins[oid] = Origin(
                oid=oid, kind=instr.kind, func=func_name,
                instr_uid=instr.uid, span=span,
                detail=f"{instr.kind}() in {func_name}")
            env[instr.dst] = frozenset({oid})
            last_def[instr.dst] = node

        elif isinstance(instr, SanitizeInstr):
            self.node(node, kind="sanitizer", dst=instr.dst, src=instr.src,
                      marker=instr.kind, func=func_name, span=span)
            if env.get(instr.src):
                # record that taint reached the cleaner (then stopped here)
                self.edge(last_def.get(instr.src, last_node), node,
                          env[instr.src])
            env[instr.dst] = frozenset()
            last_def[instr.dst] = node

        elif isinstance(instr, UnknownCall):
            self.node(node, kind="opaque-call", dst=instr.dst,
                      name=instr.name, func=func_name, span=span)
            origins = frozenset().union(
                *[env.get(a, frozenset()) for a in instr.args]
            ) if instr.args else frozenset()
            env[instr.dst] = origins
            for a in instr.args:
                if env.get(a):
                    self.edge(last_def.get(a, last_node), node, env[a])
            if instr.uid not in self._unknown_seen:
                self._unknown_seen.add(instr.uid)
                self.unknown_calls.append({
                    "name": instr.name,
                    "span": span,
                    "result_assumed_tainted": bool(origins),
                })
            last_def[instr.dst] = node

        elif isinstance(instr, SinkInstr):
            self.node(node, kind="sink", marker=instr.kind, arg=instr.arg,
                      func=func_name, span=span)
            origins = env.get(instr.arg, frozenset())
            if origins:
                self.edge(last_def.get(instr.arg, last_node), node, origins)
                self.sink_hits[instr.uid] |= origins
            self.sink_all.setdefault(instr.uid, {
                "marker": instr.kind, "span": span, "func": func_name})
        else:  # pragma: no cover
            raise AssertionError(f"unknown instruction {type(instr).__name__}")

        return env, last_def, node

    # ==================================================================
    # path reconstruction and result assembly
    # ==================================================================
    def _origin_node(self, oid: str) -> Optional[tuple]:
        origin = self.origins[oid]
        if origin.instr_uid is not None:
            return ("instr", origin.instr_uid)
        # entry-parameter origin id has the form  param@<func>#<index>
        index = int(oid.rsplit("#", 1)[1])
        return ("rootparam", origin.func, index)

    def _find_paths(self, sink_uid: int, oid: str) -> list[list[tuple]]:
        start = ("instr", sink_uid)
        goal = self._origin_node(oid)
        if goal is None or start not in self.node_meta:
            return []
        results: list[list[tuple]] = []
        seen_paths: set[tuple] = set()
        max_depth = self.config.max_path_len
        max_paths = self.config.max_paths

        def dfs(node, path, visited):
            if len(results) >= max_paths or len(path) > max_depth:
                return
            if node == goal:
                forward = list(reversed(path))
                key = tuple(forward)
                if key not in seen_paths:
                    seen_paths.add(key)
                    results.append(forward)
                return
            for pred in sorted(self.preds.get(node, ()), key=str):
                if pred in visited:  # break recursion cycles per path
                    continue
                # only traverse edges that actually propagated this origin;
                # this excludes control-only and clean-register edges
                if oid not in self.edge_tags.get((pred, node), frozenset()):
                    continue
                dfs(pred, path + [pred], visited | {pred})
                if len(results) >= max_paths:
                    return

        dfs(start, [start], {start})
        return results

    def _render_step(self, key: tuple) -> dict:
        meta = self.node_meta.get(key, {})
        kind = key[0]
        out: dict = {"node": self._node_id(key), "kind": meta.get("kind", kind)}
        ctx = meta.get("ctx")
        if ctx is not None:
            out["context"] = list(ctx)
        if "span" in meta:
            out["span"] = meta["span"]
        if kind == "instr":
            uid = key[1]
            out["instr_uid"] = uid
            span = meta.get("span") or {}
            out["text"] = self._span_text(span)
            label_map = {
                "source": f"{meta.get('marker')}()",
                "sink": f"{meta.get('marker')}({meta.get('arg')})",
                "sanitizer": f"{meta.get('marker')}({meta.get('src')})",
                "copy": f"{meta.get('dst')} = {meta.get('src')}",
                "binop": f"{meta.get('dst')} = _ {meta.get('op')} _",
                "unop": f"{meta.get('dst')} = {meta.get('op')} _",
                "const": f"{meta.get('dst')} = {meta.get('value')}",
                "opaque-call": f"{meta.get('dst')} = {meta.get('name')}(...)",
            }
            out["label"] = label_map.get(meta.get("kind"), f"instr#{uid}")
        else:
            out.update(self._aux_label(key, meta))
        return out

    def _aux_label(self, key, meta) -> dict:
        kind = key[0]
        if kind == "entry":
            return {"label": f"block {meta.get('label')}"}
        if kind == "br":
            return {"label": f"branch -> {meta.get('label')}"
                              f" ({'true' if meta.get('taken') else 'false'})"}
        if kind == "param":
            return {"label": f"bind param {meta.get('name')} of {meta.get('func')}"}
        if kind == "rootparam":
            return {"label": f"tainted entry parameter {meta.get('name')}"}
        if kind == "callarg":
            return {"label": f"argument #{meta.get('index')} at call #{meta.get('site_uid')}"}
        if kind == "callret":
            return {"label": f"return from {meta.get('callee')} at call #{meta.get('site_uid')}"}
        if kind == "exit":
            return {"label": f"return from {meta.get('func')}"}
        return {"label": kind}

    def _span_text(self, span_dict: Optional[dict]) -> str:
        if not span_dict:
            return ""
        return span_dict.get("text", "")

    def _node_id(self, key: tuple) -> str:
        kind = key[0]
        if kind == "instr":
            return f"n{key[1]}"
        if kind == "entry":
            return f"entry:{key[1]}:{key[2]}:{'.'.join(map(str, key[3])) or 'root'}"
        if kind == "br":
            return f"br:{key[1]}:{key[2]}:{'.'.join(map(str, key[3])) or 'root'}"
        if kind == "param":
            return f"param:{key[1]}:{key[2]}:{'.'.join(map(str, key[3])) or 'root'}"
        if kind == "rootparam":
            return f"rootparam:{key[1]}:{key[2]}"
        if kind == "callarg":
            return f"arg:{key[1]}:{key[2]}:{'.'.join(map(str, key[3])) or 'root'}"
        if kind == "callret":
            return f"ret:{key[1]}:{'.'.join(map(str, key[2])) or 'root'}"
        if kind == "exit":
            return f"exit:{key[1]}:{'.'.join(map(str, key[2])) or 'root'}"
        return str(key)

    def _build_result(self, entries) -> dict:
        findings = []
        for uid in sorted(self.sink_all):
            origins = self.sink_hits.get(uid, frozenset())
            info = self.sink_all[uid]
            for oid in sorted(origins):
                origin = self.origins[oid]
                paths = self._find_paths(uid, oid)
                findings.append({
                    "sink": {
                        "marker": info["marker"],
                        "func": info["func"],
                        "span": info["span"],
                        "instr_uid": uid,
                    },
                    "source": {
                        "kind": origin.kind,
                        "func": origin.func,
                        "instr_uid": origin.instr_uid,
                        "span": origin.span,
                        "detail": origin.detail,
                    },
                    "paths": [
                        {"length": len(p), "steps": [self._render_step(n) for n in p]}
                        for p in paths
                    ],
                    "path_count": len(paths),
                    "paths_truncated": len(paths) >= self.config.max_paths,
                })

        sinks_safe = []
        for uid, info in sorted(self.sink_all.items()):
            if uid not in self.sink_hits:
                sinks_safe.append({
                    "marker": info["marker"], "func": info["func"],
                    "span": info["span"], "instr_uid": uid,
                })

        truncated_ctx = [
            {"context": list(ctx), "frames": info["frames"]}
            for ctx, info in self.contexts.items() if info["truncated"]
        ]

        n_instr = sum(1 for f in self.program.functions for b in f.blocks
                      for _ in b.instructions)
        n_blocks = sum(len(f.blocks) for f in self.program.functions)

        return {
            "findings": findings,
            "sinks_without_taint": sinks_safe,
            "opaque_calls": self.unknown_calls,
            "contexts": [
                {"context": list(ctx), **info}
                for ctx, info in sorted(self.contexts.items(), key=lambda kv: str(kv[0]))
            ],
            "truncated_contexts": truncated_ctx,
            "entry_points": list(entries),
            "config_echo": {
                "k": self.config.k,
                "sources": list(self.config.sources),
                "sinks": list(self.config.sinks),
                "sanitizers": list(self.config.sanitizers),
                "tainted_entry_params": self.tainted_entry_params,
            },
            "stats": {
                "functions": len(self.program.functions),
                "blocks": n_blocks,
                "instructions": n_instr,
                "call_sites": len(self.program.call_sites),
                "contexts": len(self.contexts),
                "block_steps": self.block_steps,
                "flow_nodes": len(self.node_meta),
                "flow_edges": sum(len(v) for v in self.preds.values()),
                "findings": len(findings),
            },
        }


class _CallCont:
    """Minimal stand-in for CallTerm when re-firing a pending return."""

    def __init__(self, cont: str, return_reg, span):
        self.cont = cont
        self.return_reg = return_reg
        self.span = span
