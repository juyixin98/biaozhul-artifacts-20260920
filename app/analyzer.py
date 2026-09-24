"""Static taint analysis for TaintLang.

Analysis design (see README "Analysis model" for the prose version):

* May-taint dataflow, abstract domain ``variable -> {taint-origin: witness}``.
* Intraprocedural control-flow graph solved with a chaotic worklist to a
  least fixpoint (join = union, so loops and merged branches are handled
  without unrolling).
* Interprocedural: context-insensitive *token-parametric* function
  summaries. A callee summary speaks of symbolic origins for its own
  parameters and concrete origins for ``source()`` calls inside it; at a
  call site the caller substitutes its own origins for the callee's
  parameter tokens. This gives precise cross-function / recursive
  propagation without per-call-string explosion.
* Recursive SCCs are iterated to a monotone fixpoint. The token universe
  is finite (one token per source-call AST node and per parameter) and
  witnesses are replaced only by strictly shorter ones, so iteration
  terminates.
* Witnesses record one shortest evidence path source -> ... -> sink.

Deliberate imprecision / false-positive boundary:
both arms of every ``if`` are analyzed and merged (path-insensitive), so
sanitizing on only one arm still leaves the other arm's taint alive at
the join and is reported. Implicit flows (taint via the *value* of a
condition) are not tracked.
"""
from __future__ import annotations

from dataclasses import dataclass, field

from . import ast_nodes as ast

# Origin token prefixes
SRC_PREFIX = "src:"
PARAM_PREFIX = "param:"


class AnalysisError(Exception):
    def __init__(self, message: str, loc: ast.Loc | None = None):
        if loc is not None:
            message = f"{message} at line {loc.line}, col {loc.col}"
        super().__init__(message)
        self.loc = loc


# --------------------------------------------------------------------------- #
# Witness steps
# --------------------------------------------------------------------------- #


def _step(kind: str, func: str, loc: ast.Loc, detail: str) -> dict:
    return {"kind": kind, "func": func, "loc": f"{loc.line}:{loc.col}", "detail": detail}


def _merge_taint(dst: dict[str, list[dict]], src: dict[str, list[dict]]) -> bool:
    """Union origins into dst; for a shared origin keep the shorter witness."""
    changed = False
    for origin, wit in src.items():
        old = dst.get(origin)
        if old is None or len(wit) < len(old):
            dst[origin] = list(wit)
            changed = True
    return changed


# --------------------------------------------------------------------------- #
# CFG
# --------------------------------------------------------------------------- #


@dataclass
class CfgNode:
    node_id: int
    kind: str  # entry | skip | branch | while_header | stmt
    loc: ast.Loc
    stmt: ast.Stmt | None = None
    succs: list[int] = field(default_factory=list)


@dataclass
class Cfg:
    func: str
    nodes: dict[int, CfgNode] = field(default_factory=dict)
    entry_id: int = 0
    exit_id: int = 0


class CfgBuilder:
    def __init__(self, func_name: str):
        self.func = func_name
        self.nodes: dict[int, CfgNode] = {}
        self._counter = 0

    def _new(self, kind: str, loc: ast.Loc, stmt: ast.Stmt | None = None) -> CfgNode:
        nid = self._counter
        self._counter += 1
        node = CfgNode(node_id=nid, kind=kind, loc=loc, stmt=stmt)
        self.nodes[nid] = node
        return node

    def build(self, body: ast.Block | None, loc: ast.Loc) -> Cfg:
        entry = self._new("entry", loc)
        exitn = self._new("skip", loc)
        head, tails = self._compile_block(body, exitn.node_id)
        self.nodes[entry.node_id].succs = [head]
        for t in tails:
            self.nodes[t].succs.append(exitn.node_id)
        return Cfg(func=self.func, nodes=self.nodes,
                   entry_id=entry.node_id, exit_id=exitn.node_id)

    def _compile_block(self, block: ast.Block | None, exit_id: int) -> tuple[int, list[int]]:
        """Return (head node id, ids of nodes that leave the block normally)."""
        if block is None or not block.body:
            skip = self._new("skip", block.loc if block else ast.Loc(1, 1))
            return skip.node_id, [skip.node_id]
        head: int | None = None
        tails: list[int] = []
        for stmt in block.body:
            h, tails = self._compile_stmt(stmt, exit_id)
            if head is None:
                head = h
            else:
                for t in tails_prev:
                    self.nodes[t].succs.append(h)
            tails_prev = tails
        return head, tails  # type: ignore[return-value]

    def _compile_stmt(self, stmt: ast.Stmt, exit_id: int) -> tuple[int, list[int]]:
        if isinstance(stmt, ast.Block):
            return self._compile_block(stmt, exit_id)

        if isinstance(stmt, ast.If):
            branch = self._new("branch", stmt.loc, stmt)
            then_h, then_t = self._compile_stmt(stmt.then, exit_id)
            join = self._new("skip", stmt.loc)
            branch.succs = [then_h]
            for t in then_t:
                self.nodes[t].succs.append(join.node_id)
            if stmt.otherwise is not None:
                else_h, else_t = self._compile_stmt(stmt.otherwise, exit_id)
                branch.succs.append(else_h)
                for t in else_t:
                    self.nodes[t].succs.append(join.node_id)
            else:
                branch.succs.append(join.node_id)
            return branch.node_id, [join.node_id]

        if isinstance(stmt, ast.While):
            header = self._new("while_header", stmt.loc, stmt)
            join = self._new("skip", stmt.loc)
            body_h, body_t = self._compile_stmt(stmt.body, exit_id)
            header.succs = [body_h, join.node_id]
            for t in body_t:
                self.nodes[t].succs.append(header.node_id)
            return header.node_id, [join.node_id]

        if isinstance(stmt, ast.Return):
            node = self._new("stmt", stmt.loc, stmt)
            node.succs = [exit_id]
            return node.node_id, []  # control does not fall through

        # Assign / ExprStmt
        node = self._new("stmt", stmt.loc, stmt)
        return node.node_id, [node.node_id]


# --------------------------------------------------------------------------- #
# Function summaries
# --------------------------------------------------------------------------- #


@dataclass
class Summary:
    func: str
    # origin token -> witness (witness ends with a 'return' step for ret)
    ret: dict[str, list[dict]] = field(default_factory=dict)
    # sink-site key -> (sink loc, origin -> witness ending with 'sink' step)
    sinks: dict[str, tuple[ast.Loc, dict[str, list[dict]]]] = field(default_factory=dict)

    def signature(self) -> tuple:
        return (
            tuple(sorted((o, len(w)) for o, w in self.ret.items())),
            tuple(sorted(
                (site, tuple(sorted((o, len(w)) for o, w in origins.items())))
                for site, (_, origins) in self.sinks.items()
            )),
        )


# --------------------------------------------------------------------------- #
# Analyzer
# --------------------------------------------------------------------------- #


@dataclass
class SourceInfo:
    token: str
    func: str
    loc: ast.Loc


@dataclass
class Finding:
    sink_func: str
    sink_loc: ast.Loc
    source_func: str
    source_loc: ast.Loc
    origin: str
    path: list[dict]


class FunctionAnalyzer:
    """Analyzes one function against the current global summaries."""

    def __init__(self, fn: ast.FuncDef, funcs: dict[str, ast.FuncDef],
                 summaries: dict[str, Summary], sources: dict[str, SourceInfo],
                 is_entry: bool = False):
        self.fn = fn
        self.func = fn.name
        self.funcs = funcs
        self.summaries = summaries
        self.sources = sources
        self.is_entry = is_entry
        self.cfg = CfgBuilder(self.func).build(fn.body, fn.loc)
        self.param_tokens = [f"{PARAM_PREFIX}{self.func}:{i}" for i in range(len(fn.params))]
        # per-run accumulators
        self._ret: dict[str, list[dict]] = {}
        self._sinks: dict[str, tuple[ast.Loc, dict[str, list[dict]]]] = {}

    # ---- expression evaluation (returns taint map) ----

    def _eval(self, expr: ast.Expr, env: dict[str, dict[str, list[dict]]]) -> dict[str, list[dict]]:
        if isinstance(expr, (ast.IntLit, ast.StrLit, ast.BoolLit, ast.NilLit)):
            return {}
        if isinstance(expr, ast.Var):
            if expr.name not in env:
                raise AnalysisError(f"read of undeclared variable {expr.name!r}", expr.loc)
            return {o: list(w) for o, w in env[expr.name].items()}
        if isinstance(expr, ast.Unary):
            return self._with_step(self._eval(expr.operand, env), expr,
                                   f"operator '{expr.op}' propagates operand taint")
        if isinstance(expr, ast.Binary):
            taint: dict[str, list[dict]] = {}
            _merge_taint(taint, self._eval(expr.left, env))
            _merge_taint(taint, self._eval(expr.right, env))
            return self._with_step(taint, expr, f"operator '{expr.op}' propagates operand taint")
        if isinstance(expr, ast.Call):
            return self._eval_call(expr, env)
        raise AnalysisError(f"unknown expression {type(expr).__name__}", expr.loc)

    def _with_step(self, taint: dict[str, list[dict]] , expr: ast.Expr, detail: str) -> dict[str, list[dict]]:
        step = _step("op", self.func, expr.loc, detail)
        return {o: w + [step] for o, w in taint.items()}

    def _eval_call(self, call: ast.Call, env: dict[str, dict[str, list[dict]]]) -> dict[str, list[dict]]:
        arg_taints = [self._eval(a, env) for a in call.args]  # side effects included

        if call.name == "source":
            token = f"{SRC_PREFIX}{id(call)}"
            if token not in self.sources:
                self.sources[token] = SourceInfo(token=token, func=self.func, loc=call.loc)
            return {token: [_step("source", self.func, call.loc,
                                  "source() produces attacker-controlled data")]}

        if call.name == "sanitize":
            # Sanitizer kills taint regardless of its input.
            return {}

        if call.name == "sink":
            taint = arg_taints[0]
            wit_with_sink = {
                o: w + [_step("sink", self.func, call.loc,
                              f"tainted value reaches sink({_arg_repr(call.args[0])})")]
                for o, w in taint.items()
            }
            if wit_with_sink:
                key = f"{id(call)}"
                _, existing = self._sinks.get(key, (call.loc, {}))
                _merge_taint(existing, wit_with_sink)
                self._sinks[key] = (call.loc, existing)
            return {}

        # user-defined function: instantiate its summary
        callee = self.funcs[call.name]
        summary = self.summaries.get(call.name)
        if summary is None:
            # Callee summary not computed yet (first pass over a recursive SCC):
            # assume it returns nothing and has no reachable sinks.
            return {}
        return self._instantiate(call, callee, summary, arg_taints)

    def _instantiate(self, call: ast.Call, callee: ast.FuncDef, summary: Summary,
                     arg_taints: list[dict[str, list[dict]]]) -> dict[str, list[dict]]:
        call_step = _step(
            "call", self.func, call.loc,
            f"calls {callee.name}({', '.join(_arg_repr(a) for a in call.args)})")

        def expand(origin: str, wit: list[dict]) -> dict[str, list[dict]]:
            """Bind one callee-side origin to the caller-side origins.

            A callee parameter expands to every taint origin of the
            matching argument (the caller witness runs up to the call;
            the callee's own param/source step continues the chain).
            An internal source of the callee is a concrete origin on
            its own, reached only after the call executes.
            """
            if origin.startswith(PARAM_PREFIX):
                idx = int(origin.rsplit(":", 1)[1])
                out: dict[str, list[dict]] = {}
                for caller_origin, arg_wit in arg_taints[idx].items():
                    _merge_taint(out, {caller_origin: arg_wit + [call_step] + wit})
                return out
            return {origin: [call_step] + wit}

        # instantiate sink findings of the callee at this call edge
        for site, (sink_loc, origins) in summary.sinks.items():
            mapped: dict[str, list[dict]] = {}
            for origin, wit in origins.items():
                _merge_taint(mapped, expand(origin, wit))
            if mapped:
                _, existing = self._sinks.get(site, (sink_loc, {}))
                _merge_taint(existing, mapped)
                self._sinks[site] = (sink_loc, existing)

        # instantiate return taint
        result: dict[str, list[dict]] = {}
        for origin, wit in summary.ret.items():
            _merge_taint(result, expand(origin, wit))
        return result

    # ---- transfer ----

    def _initial_env(self) -> dict[str, dict[str, list[dict]]]:
        env: dict[str, dict[str, list[dict]]] = {}
        for name, token in zip(self.fn.params, self.param_tokens):
            step = _step("param", self.func, self.fn.loc,
                         f"parameter {name!r} enters function {self.func}")
            step["_token"] = token
            env[name] = {token: [step]}
        return env

    def _transfer(self, node: CfgNode,
                  in_env: dict[str, dict[str, list[dict]]]) -> list[dict[str, dict[str, list[dict]]]]:
        """Return one outgoing env per successor."""
        stmt = node.stmt
        if node.kind in ("skip", "entry"):
            return [dict(in_env)] * len(node.succs)

        if node.kind in ("branch", "while_header"):
            # Conditions may contain calls with sinks; evaluate for effects.
            cond = stmt.cond
            self._eval(cond, in_env)
            return [dict(in_env)] * len(node.succs)

        if isinstance(stmt, ast.Assign):
            new_env = dict(in_env)
            taint = self._eval(stmt.value, in_env)
            taint = {o: w + [_step("assign", self.func, stmt.loc,
                                   f"assigned to variable {stmt.name!r}")]
                     for o, w in taint.items()}
            new_env[stmt.name] = taint  # strong update
            return [new_env] * len(node.succs)

        if isinstance(stmt, ast.ExprStmt):
            self._eval(stmt.expr, in_env)
            return [dict(in_env)] * len(node.succs)

        if isinstance(stmt, ast.Return):
            taint = self._eval(stmt.value, in_env)
            taint = {o: w + [_step("return", self.func, stmt.loc,
                                   f"returned from function {self.func}")]
                     for o, w in taint.items()}
            _merge_taint(self._ret, taint)
            return [dict(in_env)] * len(node.succs)

        raise AnalysisError(f"unexpected CFG node kind {node.kind}", node.loc)

    def run(self) -> Summary:
        ins: dict[int, dict[str, dict[str, list[dict]]]] = {}
        worklist = [self.cfg.entry_id]
        ins[self.cfg.entry_id] = self._initial_env()
        while worklist:
            nid = worklist.pop()
            node = self.cfg.nodes[nid]
            outs = self._transfer(node, ins[nid])
            for succ, out_env in zip(node.succs, outs):
                old = ins.get(succ)
                merged: dict[str, dict[str, list[dict]]] = {}
                if old is not None:
                    for var in set(old) | set(out_env):
                        merged[var] = {}
                        _merge_taint(merged[var], old.get(var, {}))
                        _merge_taint(merged[var], out_env.get(var, {}))
                else:
                    merged = {var: {o: list(w) for o, w in taint.items()}
                              for var, taint in out_env.items()}
                if old != merged:
                    ins[succ] = merged
                    if succ not in worklist:
                        worklist.append(succ)
        # strip internal annotations from witnesses before publishing
        clean_ret = {o: _clean(w) for o, w in self._ret.items()}
        clean_sinks = {
            site: (loc, {o: _clean(w) for o, w in origins.items()})
            for site, (loc, origins) in self._sinks.items()
        }
        return Summary(func=self.func, ret=clean_ret, sinks=clean_sinks)


def _arg_repr(expr: ast.Expr) -> str:
    if isinstance(expr, ast.Var):
        return expr.name
    if isinstance(expr, ast.Call):
        return f"{expr.name}(...)"
    if isinstance(expr, ast.IntLit):
        return str(expr.value)
    if isinstance(expr, ast.StrLit):
        return repr(expr.value)
    return type(expr).__name__.lower()


def _clean(wit: list[dict]) -> list[dict]:
    return [{k: v for k, v in s.items() if not k.startswith("_")} for s in wit]


# --------------------------------------------------------------------------- #
# Call graph, SCCs, driver
# --------------------------------------------------------------------------- #


class Analyzer:
    MAX_SCC_ITERATIONS = 10000

    def __init__(self, program: ast.Program):
        self.program = program
        self.summaries: dict[str, Summary] = {}
        self.sources: dict[str, SourceInfo] = {}
        self.iterations = 0
        self._validate()

    def _all_calls(self) -> list[ast.Call]:
        calls: list[ast.Call] = []

        def visit_expr(e: ast.Expr) -> None:
            if isinstance(e, ast.Call):
                for a in e.args:
                    visit_expr(a)
                calls.append(e)
            elif isinstance(e, ast.Binary):
                visit_expr(e.left)
                visit_expr(e.right)
            elif isinstance(e, ast.Unary):
                visit_expr(e.operand)

        def visit_stmt(s: ast.Stmt) -> None:
            if isinstance(s, ast.Block):
                for st in s.body:
                    visit_stmt(st)
            elif isinstance(s, ast.If):
                visit_expr(s.cond)
                visit_stmt(s.then)
                if s.otherwise is not None:
                    visit_stmt(s.otherwise)
            elif isinstance(s, ast.While):
                visit_expr(s.cond)
                visit_stmt(s.body)
            elif isinstance(s, ast.Return):
                visit_expr(s.value)
            elif isinstance(s, ast.Assign):
                visit_expr(s.value)
            elif isinstance(s, ast.ExprStmt):
                visit_expr(s.expr)

        for fn in self.program.funcs.values():
            visit_stmt(fn.body)
        for s in self.program.top_level:
            visit_stmt(s)
        return calls

    def _validate(self) -> None:
        arity = {"source": 0, "sink": 1, "sanitize": 1}
        for call in self._all_calls():
            if call.name in ast.BUILTINS:
                want = arity[call.name]
                if len(call.args) != want:
                    raise AnalysisError(
                        f"builtin {call.name}() expects {want} argument(s), got {len(call.args)}",
                        call.loc)
            elif call.name in self.program.funcs:
                want = len(self.program.funcs[call.name].params)
                if len(call.args) != want:
                    raise AnalysisError(
                        f"function {call.name}() expects {want} argument(s), got {len(call.args)}",
                        call.loc)
            else:
                raise AnalysisError(f"call to undefined function {call.name!r}", call.loc)

    def _call_graph(self) -> dict[str, set[str]]:
        graph: dict[str, set[str]] = {name: set() for name in self.program.func_order}
        for name, fn in self.program.funcs.items():
            for call in self._calls_in(fn.body):
                if call.name in self.program.funcs:
                    graph[name].add(call.name)
        return graph

    def _calls_in(self, node: ast.Node) -> list[ast.Call]:
        out: list[ast.Call] = []

        def expr(e: ast.Expr) -> None:
            if isinstance(e, ast.Call):
                for a in e.args:
                    expr(a)
                out.append(e)
            elif isinstance(e, ast.Binary):
                expr(e.left)
                expr(e.right)
            elif isinstance(e, ast.Unary):
                expr(e.operand)

        def stmt(s: ast.Stmt) -> None:
            if isinstance(s, ast.Block):
                for st in s.body:
                    stmt(st)
            elif isinstance(s, ast.If):
                expr(s.cond)
                stmt(s.then)
                if s.otherwise is not None:
                    stmt(s.otherwise)
            elif isinstance(s, ast.While):
                expr(s.cond)
                stmt(s.body)
            elif isinstance(s, ast.Return):
                expr(s.value)
            elif isinstance(s, ast.Assign):
                expr(s.value)
            elif isinstance(s, ast.ExprStmt):
                expr(s.expr)

        stmt(node)
        return out

    @staticmethod
    def _tarjan(graph: dict[str, set[str]]) -> list[list[str]]:
        index = 0
        indices: dict[str, int] = {}
        low: dict[str, int] = {}
        stack: list[str] = []
        on_stack: set[str] = set()
        result: list[list[str]] = []

        def strongconnect(v: str) -> None:
            nonlocal index
            indices[v] = low[v] = index
            index += 1
            stack.append(v)
            on_stack.add(v)
            for w in sorted(graph[v]):
                if w not in indices:
                    strongconnect(w)
                    low[v] = min(low[v], low[w])
                elif w in on_stack:
                    low[v] = min(low[v], indices[w])
            if low[v] == indices[v]:
                comp: list[str] = []
                while True:
                    w = stack.pop()
                    on_stack.discard(w)
                    comp.append(w)
                    if w == v:
                        break
                result.append(comp)

        for v in sorted(graph):
            if v not in indices:
                strongconnect(v)
        return result  # popped in topological (callees-first) order

    def _analyze_function(self, fn: ast.FuncDef, is_entry: bool = False) -> Summary:
        return FunctionAnalyzer(fn, self.program.funcs, self.summaries,
                                self.sources, is_entry).run()

    def analyze(self) -> dict:
        graph = self._call_graph()
        for scc in self._tarjan(graph):
            recursive = len(scc) > 1 or scc[0] in graph[scc[0]]
            if not recursive:
                name = scc[0]
                self.summaries[name] = self._analyze_function(self.program.funcs[name])
                continue
            # Fixpoint over the recursive SCC: start with empty summaries
            # for its members and iterate until no summary changes.
            for name in scc:
                self.summaries.setdefault(name, Summary(name))
            for _ in range(self.MAX_SCC_ITERATIONS):
                self.iterations += 1
                old_sigs = {n: self.summaries[n].signature() for n in scc}
                for name in scc:
                    self.summaries[name] = self._analyze_function(self.program.funcs[name])
                if all(self.summaries[n].signature() == old_sigs[n] for n in scc):
                    break
            else:
                raise AnalysisError("fixpoint did not converge (internal error)")

        # Synthetic entry function wrapping the top-level statements.
        entry_fn = ast.FuncDef(loc=ast.Loc(1, 1), name="<main>", params=[],
                               body=ast.Block(loc=ast.Loc(1, 1), body=self.program.top_level))
        entry_summary = self._analyze_function(entry_fn, is_entry=True)

        findings = self._collect_findings(entry_summary)
        return {
            "findings": findings,
            "vulnerable": bool(findings),
            "stats": {
                "functions": len(self.program.funcs) + 1,
                "user_functions": len(self.program.funcs),
                "scc_fixpoint_rounds": self.iterations,
                "sources": len({s.loc for s in self.sources.values()}),
                "sinks_hit": len(entry_summary.sinks),
                "findings": len(findings),
            },
        }

    def _collect_findings(self, entry_summary: Summary) -> list[dict]:
        findings: list[Finding] = []
        # Deduplicate identical (sink site, origin, witness length/content) records.
        seen: set[tuple] = set()
        for site, (sink_loc, origins) in entry_summary.sinks.items():
            for origin, wit in origins.items():
                if not origin.startswith(SRC_PREFIX):
                    continue  # residual symbolic token: not reachable from a real source
                src = self.sources[origin]
                key = (site, origin, tuple((s["kind"], s["loc"], s["detail"]) for s in wit))
                if key in seen:
                    continue
                seen.add(key)
                findings.append(Finding(
                    sink_func=_sink_func(wit),
                    sink_loc=sink_loc,
                    source_func=src.func,
                    source_loc=src.loc,
                    origin=origin,
                    path=wit,
                ))
        findings.sort(key=lambda f: (f.sink_loc.line, f.source_loc.line, len(f.path)))
        return [self._finding_dict(f) for f in findings]

    def _finding_dict(self, f: Finding) -> dict:
        return {
            "sink": {"func": f.sink_func, "loc": f"{f.sink_loc.line}:{f.sink_loc.col}"},
            "source": {"func": f.source_func, "loc": f"{f.source_loc.line}:{f.source_loc.col}"},
            "path_length": len(f.path),
            "path": f.path,
            "call_chain": _call_chain(f.path),
        }


def _sink_func(wit: list[dict]) -> str:
    for step in reversed(wit):
        if step["kind"] == "sink":
            return step["func"]
    return "<main>"


def _call_chain(wit: list[dict]) -> list[str]:
    chain: list[str] = []
    for step in wit:
        if step["kind"] == "call":
            detail = step["detail"]
            # detail format: "calls name(...)"
            name = detail[len("calls "):].split("(", 1)[0]
            chain.append(name)
        elif step["kind"] == "sink":
            chain.append(f"{step['func']}::sink")
    return chain
