"""Path-sensitive resource-release analysis over the CFG.

The analyzer performs a bounded, depth-first symbolic execution of the
control-flow graph:

* Every path maintains a map ``resource -> state`` where state is one of
  ``unacquired`` / ``held`` / ``released``.
* Branch and loop decisions fork the path.  Exception edges emitted by
  ``throw`` nodes are first-class CFG edges, so exceptional exits take
  part in the state computation exactly like normal edges.
* Loops are unrolled up to ``loop_bound`` entries.  A continuation that
  would enter the loop once more is reported with
  ``status = "loop_truncated"``; the exit side is still explored.
* At every terminal (``exit`` and ``uncaught``) the residual state is
  checked for leaked resources.

The walk order is fixed (successors sorted by edge kind), which makes the
reported path numbering reproducible across runs and machines.
"""

from dataclasses import dataclass, field

from . import ast_nodes as ast
from .cfg import build_cfg

# Resource lifecycle states.
UNACQUIRED = "unacquired"
HELD = "held"
RELEASED = "released"

# Diagnostic codes.
DOUBLE_ACQUIRE = "double_acquire"
DOUBLE_RELEASE = "double_release"
USE_AFTER_RELEASE = "use_after_release"
USE_UNACQUIRED = "use_unacquired"
RELEASE_UNACQUIRED = "release_unacquired"
RESOURCE_LEAK = "resource_leak"
UNKNOWN_RESOURCE = "unknown_resource"

TERMINAL_KINDS = ("exit", "uncaught")

# Deterministic successor order: true before false, normal/back after.
_EDGE_ORDER = {"true": 0, "false": 1, "normal": 2,
               "exception": 3, "back": 4}


@dataclass
class Diagnostic:
    code: str
    message: str
    resource: str
    location: object
    path_id: int = -1
    terminal: str = ""

    def key(self):
        return (self.code, self.resource, self.location.line,
                self.location.column, self.terminal)


@dataclass
class StateChange:
    node_id: int
    resource: str
    op: str
    before: str
    after: str


@dataclass
class Path:
    id: int
    status: str
    terminal: str
    trace: list
    decisions: list
    final_states: dict
    changes: list
    diagnostics: list = field(default_factory=list)


class Options:
    def __init__(self, loop_bound=2, max_paths=512):
        self.loop_bound = loop_bound
        self.max_paths = max_paths


def analyze_program(functions, options=None):
    options = options or Options()
    return [analyze_function(fn, options) for fn in functions]


def analyze_function(function, options=None):
    options = options or Options()
    graph = build_cfg(function)
    universe = _resource_universe(function)
    walker = _Walker(graph, universe, set(function.local_names()), options)
    initial = {r: (HELD if r in function.params else UNACQUIRED)
               for r in universe}
    walker.run(initial)
    return FunctionAnalysis(function, graph, walker)


def _resource_universe(function):
    """All names that can ever be a resource, in first-appearance order.

    A name belongs to the resource universe iff it is either a function
    parameter (ownership passed in, initially ``held``) or it appears in
    an ``acquire``/``release``/``use`` statement.  Plain condition
    variables referenced only in expressions are intentionally excluded:
    they behave as unconstrained symbolic inputs.
    """
    names = []

    def add(n):
        if n not in names:
            names.append(n)

    for p in function.params:
        add(p)

    def walk(stmts):
        for s in stmts:
            t = type(s)
            if t in (ast.Acquire, ast.Release, ast.Use):
                add(s.resource)
            for block in ast._child_blocks(s):
                walk(block)

    walk(function.body)
    return names


class _Walker:
    def __init__(self, graph, universe, declared, options):
        self.g = graph
        self.universe = universe
        self.declared = declared
        self.opt = options
        self.paths = []
        # Reserved for path-independent checks; currently none are needed
        # because acquire itself introduces a resource into the universe.
        self.static_diagnostics = []
        self._path_counter = 0
        self._path_cap_reported = False

    # -- DFS ---------------------------------------------------------------

    def run(self, initial_state):
        self._visit(
            node_id=self.g.entry,
            state=dict(initial_state),
            trace=[],
            decisions=[],
            changes=[],
            loop_counts={},
            diags=[],
        )

    def _finish(self, status, terminal, state, trace, decisions, changes,
                diags):
        pid = self._path_counter
        self._path_counter += 1
        resolved = []
        seen_keys = set()
        for d in list(self.static_diagnostics) + diags:
            d2 = Diagnostic(d.code, d.message, d.resource, d.location,
                            pid, terminal)
            if d2.key() in seen_keys:
                continue
            seen_keys.add(d2.key())
            resolved.append(d2)
        self.paths.append(Path(
            id=pid, status=status, terminal=terminal,
            trace=list(trace), decisions=list(decisions),
            final_states=dict(state), changes=list(changes),
            diagnostics=resolved))

    def _visit(self, node_id, state, trace, decisions, changes,
               loop_counts, diags):
        node = self.g.nodes[node_id]

        # Global path budget: one synthetic truncation record, then stop.
        if len(self.paths) >= self.opt.max_paths:
            if not self._path_cap_reported:
                self._path_cap_reported = True
                self._finish("path_cap", f"node:{node_id}", state, trace,
                             decisions, changes, diags)
            return

        self._transfer(node, state, changes, diags)
        trace.append(node_id)

        if node.kind in TERMINAL_KINDS:
            self._leak_checks(state, node, diags)
            self._finish("completed", node.kind, state, trace, decisions,
                         changes, diags)
            return

        edges, truncated = self._select_edges(node, decisions, loop_counts)

        if truncated:
            # Record the truncation as its own path so the result is
            # reproducible and explicit; normal continuations still fork.
            # ``iteration`` names the iteration that could not be entered.
            next_iteration = loop_counts.get(node.id, 0) + 1
            trunc_decisions = decisions + [{
                "node": node.id, "edge": "true", "value": True,
                "iteration": next_iteration,
                "truncated": True,
            }]
            self._finish("loop_truncated", f"loop:{node.id}", dict(state),
                         trace, trunc_decisions, changes, diags)
            if not edges:
                return

        if not edges:
            self._finish("completed", f"node:{node_id}", state, trace,
                         decisions, changes, diags)
            return

        for edge in edges:
            self._continue(edge, node, state, trace, decisions, changes,
                           loop_counts, diags)

    def _continue(self, edge, parent_node, state, trace, decisions,
                  changes, loop_counts, diags):
        """Build the per-edge continuation state and recurse.

        Both single- and multi-edge cases funnel through here so loop
        counters are incremented exactly once per taken ``true`` edge,
        regardless of how many siblings the edge has.
        """
        new_counts = dict(loop_counts)
        new_decisions = decisions
        cv = parent_node.detail.get("cond_value", None)
        if parent_node.kind == "loop":
            count = new_counts.get(parent_node.id, 0)
            if edge.kind == "true":
                new_counts[parent_node.id] = count + 1
                new_decisions = decisions + [{
                    "node": parent_node.id, "edge": "true",
                    "value": True, "iteration": count + 1,
                    "constant": cv is True}]
            elif edge.kind == "false":
                new_decisions = decisions + [{
                    "node": parent_node.id, "edge": "false",
                    "value": False, "iteration": count,
                    "constant": cv is False}]
        elif parent_node.kind == "branch":
            new_decisions = decisions + [{
                "node": parent_node.id, "edge": edge.kind,
                "value": edge.kind == "true",
                "constant": cv is not None}]
        self._visit(
            edge.dst,
            dict(state),
            list(trace),
            new_decisions,
            [StateChange(c.node_id, c.resource, c.op, c.before, c.after)
             for c in changes],
            new_counts,
            list(diags))

    # -- transfer functions ------------------------------------------------

    def _transfer(self, node, state, changes, diags):
        d = node.detail
        op = d.get("op")
        res = d.get("resource")
        if op is None or res is None:
            return
        before = state.get(res, UNACQUIRED)
        after = before
        if op == "acquire":
            if before == HELD:
                diags.append(Diagnostic(
                    DOUBLE_ACQUIRE,
                    f"resource {res!r} acquired while still held "
                    f"(previous instance leaks)", res, node.loc))
            after = HELD
        elif op == "release":
            if before == RELEASED:
                diags.append(Diagnostic(
                    DOUBLE_RELEASE,
                    f"resource {res!r} released twice", res, node.loc))
            elif before == UNACQUIRED:
                diags.append(Diagnostic(
                    RELEASE_UNACQUIRED,
                    f"resource {res!r} released but never acquired",
                    res, node.loc))
            after = RELEASED
        elif op == "use":
            if before == RELEASED:
                diags.append(Diagnostic(
                    USE_AFTER_RELEASE,
                    f"resource {res!r} used after release",
                    res, node.loc))
            elif before == UNACQUIRED:
                diags.append(Diagnostic(
                    USE_UNACQUIRED,
                    f"resource {res!r} used but never acquired",
                    res, node.loc))
        changes.append(StateChange(node.id, res, op, before, after))
        state[res] = after

    def _leak_checks(self, state, terminal_node, diags):
        for res in self.universe:
            if state.get(res) == HELD:
                diags.append(Diagnostic(
                    RESOURCE_LEAK,
                    f"resource {res!r} still held at {terminal_node.kind} "
                    f"(missing release on this path)",
                    res, terminal_node.loc))

    # -- edge selection / branch & loop semantics --------------------------

    def _select_edges(self, node, decisions, loop_counts):
        """Return ``(edges, truncated)`` for a branch/loop/other node.

        Pure: it never mutates ``decisions``/``loop_counts``.  Decision
        records and counter increments are produced per taken edge in
        :meth:`_continue`.
        """
        out = sorted(self.g.out_edges(node.id),
                     key=lambda e: _EDGE_ORDER.get(e.kind, 9))
        cv = node.detail.get("cond_value", None)

        if node.kind == "branch":
            if cv is True:
                return [e for e in out if e.kind == "true"], False
            if cv is False:
                return [e for e in out if e.kind == "false"], False
            return out, False

        if node.kind == "loop":
            count = loop_counts.get(node.id, 0)
            true_edges = [e for e in out if e.kind == "true"]
            false_edges = [e for e in out if e.kind == "false"]
            if cv is False:
                return false_edges, False
            if cv is True:
                if count >= self.opt.loop_bound:
                    return [], True
                return true_edges, False
            # Unknown condition: explore the exit side and, while within
            # the bound, the re-entry side.
            if count >= self.opt.loop_bound:
                return false_edges, True
            return true_edges + false_edges, False

        return out, False


class FunctionAnalysis:
    def __init__(self, function, graph, walker):
        self.function = function
        self.graph = graph
        self.paths = walker.paths
        self.static_diagnostics = walker.static_diagnostics
        self.path_cap_hit = walker._path_cap_reported
        self.universe = walker.universe

    def to_dict(self):
        initial = {r: (HELD if r in self.function.params else UNACQUIRED)
                   for r in self.universe}
        diagnostics = self._aggregate_diagnostics()
        return {
            "name": self.function.name,
            "params": list(self.function.params),
            "location": self.function.loc.to_dict(),
            "resource_universe": list(self.universe),
            "initial_states": initial,
            "cfg": self.graph.to_dict(),
            "paths": [self._path_dict(p) for p in self.paths],
            "diagnostics": diagnostics,
            "summary": {
                "path_count": len(self.paths),
                "completed": sum(1 for p in self.paths
                                 if p.status == "completed"),
                "loop_truncated": sum(1 for p in self.paths
                                      if p.status == "loop_truncated"),
                "path_cap": sum(1 for p in self.paths
                                if p.status == "path_cap"),
                "diagnostic_count": len(diagnostics),
            },
        }

    def _path_dict(self, p):
        return {
            "id": p.id,
            "status": p.status,
            "terminal": p.terminal,
            "node_trace": p.trace,
            "decisions": p.decisions,
            "state_changes": [
                {"node": c.node_id, "resource": c.resource, "op": c.op,
                 "before": c.before, "after": c.after}
                for c in p.changes
            ],
            "final_states": {r: p.final_states.get(r, UNACQUIRED)
                             for r in self.universe},
            "diagnostics": [self._diag_dict(d) for d in p.diagnostics],
        }

    def _aggregate_diagnostics(self):
        merged = {}
        order = []
        for p in self.paths:
            for d in p.diagnostics:
                key = (d.code, d.resource, d.location.line,
                       d.location.column)
                if key not in merged:
                    order.append(key)
                    merged[key] = {
                        "code": d.code,
                        "message": d.message,
                        "resource": d.resource,
                        "location": d.location.to_dict(),
                        "path_ids": [],
                        "terminals": [],
                    }
                entry = merged[key]
                if d.path_id not in entry["path_ids"]:
                    entry["path_ids"].append(d.path_id)
                if d.terminal and d.terminal not in entry["terminals"]:
                    entry["terminals"].append(d.terminal)
        return [merged[k] for k in order]

    @staticmethod
    def _diag_dict(d):
        return {
            "code": d.code,
            "message": d.message,
            "resource": d.resource,
            "location": d.location.to_dict(),
        }
