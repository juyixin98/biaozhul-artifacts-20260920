"""Path-sensitive resource-release analysis.

The analyzer enumerates *concrete control-flow paths* through a function's CFG
with deterministic ordering. Both normal and exception edges are traversed, so
exceptional exits participate in every calculation.

Resource states tracked per variable:

* ``ABSENT``    - never acquired on this path
* ``HELD``      - acquired and not released
* ``RELEASED``  - released since acquisition

Loops are unfolded a bounded number of times (``loop_bound`` back edges);
conditions are opaque, so both branch outcomes are explored. This makes the
result a complete description of behavior within the documented bound and
fully reproducible.
"""
from __future__ import annotations

from dataclasses import dataclass, field
from typing import Dict, List, Optional, Tuple

from . import ast_nodes as ast
from .cfg import EXCEPTION, NORMAL, CfgEdge, CfgNode, FunctionCfg, build_cfg
from .errors import AnalysisError
from .locations import SourceFile, Span
from .parser import parse_source
from .semantics import check_program
from .summaries import compute_summaries

ABSENT = "ABSENT"
HELD = "HELD"
RELEASED = "RELEASED"

# Finding codes.
DOUBLE_RELEASE = "DOUBLE_RELEASE"
USE_AFTER_RELEASE = "USE_AFTER_RELEASE"
LEAK = "RESOURCE_LEAK"
RELEASE_NOT_HELD = "RELEASE_NOT_HELD"
USE_NOT_HELD = "USE_NOT_HELD"
OVERWRITE_HELD = "OVERWRITE_HELD"


@dataclass
class Finding:
    code: str
    message: str
    span: Span
    function: str
    variable: Optional[str] = None
    resource_kind: Optional[str] = None
    node_id: str = ""


@dataclass
class FunctionResult:
    name: str
    paths: List[dict] = field(default_factory=list)
    findings: List[Finding] = field(default_factory=list)


@dataclass
class _Shared:
    findings: List[Finding] = field(default_factory=list)
    index: Dict[Tuple[str, str, str, str, int], int] = field(default_factory=dict)
    path_counter: int = 0
    truncated: int = 0

    def intern(self, finding: Finding) -> int:
        """Return the stable 1-based id of a finding, creating it on first use."""
        key = (
            finding.function,
            finding.code,
            finding.variable or "",
            finding.node_id,
            finding.span.start.offset,
        )
        existing = self.index.get(key)
        if existing is not None:
            return existing
        self.findings.append(finding)
        fid = len(self.findings)
        self.index[key] = fid
        return fid


class Analyzer:
    def __init__(
        self,
        source: SourceFile,
        funcs: Dict[str, ast.Function],
        cfgs: Dict[str, FunctionCfg],
        loop_bound: int,
        max_steps: int,
        may_return: Dict[str, bool],
        may_throw: Dict[str, bool],
    ):
        self.source = source
        self.funcs = funcs
        self.cfgs = cfgs
        self.loop_bound = loop_bound
        self.max_steps = max_steps
        self.may_return = may_return
        self.may_throw = may_throw
        self.results: Dict[str, FunctionResult] = {}
        self.shared = _Shared()

    def analyze(self) -> Dict[str, FunctionResult]:
        for fn in self.funcs.values():
            self.results[fn.name] = self.analyze_function(self.cfgs[fn.name])
        return self.results

    # ---------- function / path driver ----------

    def analyze_function(self, cfg: FunctionCfg) -> FunctionResult:
        result = FunctionResult(name=cfg.name)
        adjacency: Dict[str, List[CfgEdge]] = {node_id: [] for node_id in cfg.nodes}
        for edge in cfg.edges:
            adjacency.setdefault(edge.src, []).append(edge)
        for edges in adjacency.values():
            edges.sort(key=edge_order)

        initial_state = {var: ABSENT for var in cfg.resource_vars}

        def visit(
            node_id: str,
            state: Dict[str, str],
            steps: List[dict],
            incoming: Optional[CfgEdge],
            loop_visits: Dict[str, int],
        ) -> None:
            node = cfg.nodes[node_id]
            if len(steps) > self.max_steps:
                self.shared.truncated += 1
                return

            local_state = dict(state)
            events: List[dict] = []
            terminal: Optional[str] = None

            if node.kind == "acquire":
                self.do_acquire(cfg, node, local_state, events, result)
            elif node.kind == "release":
                self.do_release(cfg, node, local_state, events, result)
            elif node.kind == "use":
                self.do_use(cfg, node, local_state, events, result)
            elif node.kind == "return":
                # Its normal edge reaches the synthetic exit, where the leak
                # check is performed once for all normal exits.
                pass
            elif node.kind == "exit":
                self.do_leak_check(cfg, node, local_state, events, result, "normal")
                terminal = "normal"
            elif node.kind == "except_exit":
                self.do_leak_check(cfg, node, local_state, events, result, "exception")
                terminal = "exception"
            # Other kinds (entry/skip/block/condition/while/try/catch/call/throw)
            # have no direct resource effect; their edges encode the transfer.

            step = {
                "node_id": node_id,
                "kind": node.kind,
                "label": node.label,
                "span": node.span.to_dict(),
                "via_edge": edge_dict(incoming) if incoming is not None else None,
                "state_after": snapshot(local_state),
                "events": events,
            }
            next_steps = steps + [step]

            if terminal is not None:
                self.record_path(result, next_steps, terminal)
                return

            edges = adjacency.get(node_id, [])
            usable_count = 0
            for edge in edges:
                # Interprocedural summary: drop outcomes the callee cannot have.
                if node.kind == "call":
                    callee = node.call.name
                    if edge.kind == NORMAL and not self.may_return[callee]:
                        continue
                    if edge.kind == EXCEPTION and not self.may_throw[callee]:
                        continue
                next_visits = loop_visits
                if edge.label == "back":
                    visits = loop_visits.get(edge.dst, 0) + 1
                    if visits > self.loop_bound:
                        continue
                    next_visits = dict(loop_visits)
                    next_visits[edge.dst] = visits
                usable_count += 1
                visit(edge.dst, local_state, next_steps, edge, next_visits)

            if usable_count == 0:
                # No possible successor: the function diverges here (infinite
                # loop or a call that neither returns nor throws).
                self.record_path(result, next_steps, "divergent")

        visit(cfg.entry, initial_state, [], None, {})
        return result

    def record_path(self, result: FunctionResult, steps: List[dict], terminal: str) -> None:
        self.shared.path_counter += 1
        result.paths.append(
            {
                "path_id": self.shared.path_counter,
                "kind": terminal,
                "node_sequence": [s["node_id"] for s in steps],
                "steps": steps,
                "finding_ids": self.finding_ids_on_path(steps),
            }
        )

    @staticmethod
    def finding_ids_on_path(steps: List[dict]) -> List[int]:
        ids: List[int] = []
        for step in steps:
            for ev in step["events"]:
                if ev.get("finding_id") is not None and ev["finding_id"] not in ids:
                    ids.append(ev["finding_id"])
        return ids

    # ---------- resource state transitions ----------

    def add_finding(
        self,
        cfg: FunctionCfg,
        code: str,
        message: str,
        span: Span,
        events: List[dict],
        result: FunctionResult,
        variable: Optional[str] = None,
        resource_kind: Optional[str] = None,
        node_id: str = "",
    ) -> int:
        finding = Finding(
            code=code,
            message=message,
            span=span,
            function=cfg.name,
            variable=variable,
            resource_kind=resource_kind,
            node_id=node_id,
        )
        fid = self.shared.intern(finding)
        result.findings.append(fid)
        events.append(
            {
                "type": "finding",
                "finding_id": fid,
                "code": code,
                "message": message,
                "variable": variable,
                "span": span.to_dict(),
            }
        )
        return fid

    def do_acquire(
        self,
        cfg: FunctionCfg,
        node: CfgNode,
        state: Dict[str, str],
        events: List[dict],
        result: FunctionResult,
    ) -> None:
        var = node.target
        kind = node.resource_kind
        prior = state.get(var, ABSENT)
        if prior == HELD:
            # Overwriting a held handle leaks the old resource, but the
            # variable is held again from this point on.
            self.add_finding(
                cfg, OVERWRITE_HELD,
                f"variable {var!r} is re-acquired while a resource is still held; "
                f"the previous {kind!r} resource is leaked",
                node.span, events, result, variable=var, resource_kind=kind, node_id=node.id,
            )
            self.add_finding(
                cfg, LEAK,
                f"resource {var!r} ({kind}) is still held when overwritten",
                node.span, events, result, variable=var, resource_kind=kind, node_id=node.id,
            )
        elif prior == RELEASED:
            # Reusing a released slot is normal reacquisition.
            events.append({"type": "reacquire", "variable": var})
        state[var] = HELD
        events.append({"type": "acquire", "variable": var, "resource_kind": kind})

    def do_release(
        self,
        cfg: FunctionCfg,
        node: CfgNode,
        state: Dict[str, str],
        events: List[dict],
        result: FunctionResult,
    ) -> None:
        var = node.target
        prior = state.get(var, ABSENT)
        if prior == HELD:
            state[var] = RELEASED
            events.append({"type": "release", "variable": var})
            return
        if prior == RELEASED:
            self.add_finding(
                cfg, DOUBLE_RELEASE,
                f"resource {var!r} is released a second time",
                node.span, events, result, variable=var, node_id=node.id,
            )
        else:
            self.add_finding(
                cfg, RELEASE_NOT_HELD,
                f"release of {var!r} but no resource is held on this path",
                node.span, events, result, variable=var, node_id=node.id,
            )
        # State stays unchanged on an invalid release.

    def do_use(
        self,
        cfg: FunctionCfg,
        node: CfgNode,
        state: Dict[str, str],
        events: List[dict],
        result: FunctionResult,
    ) -> None:
        var = node.target
        prior = state.get(var, ABSENT)
        if prior == HELD:
            events.append({"type": "use", "variable": var})
            return
        if prior == RELEASED:
            self.add_finding(
                cfg, USE_AFTER_RELEASE,
                f"use of {var!r} after it was released",
                node.span, events, result, variable=var, node_id=node.id,
            )
        else:
            self.add_finding(
                cfg, USE_NOT_HELD,
                f"use of {var!r} but no resource is held on this path",
                node.span, events, result, variable=var, node_id=node.id,
            )

    def do_leak_check(
        self,
        cfg: FunctionCfg,
        node: CfgNode,
        state: Dict[str, str],
        events: List[dict],
        result: FunctionResult,
        exit_kind: str,
    ) -> None:
        for var in cfg.resource_vars:
            if state.get(var, ABSENT) == HELD:
                kind = self.kind_of(cfg, var)
                if exit_kind == "exception":
                    message = f"resource {var!r} ({kind}) is still held on exceptional exit"
                else:
                    message = f"resource {var!r} ({kind}) is still held on function exit"
                self.add_finding(
                    cfg, LEAK, message, node.span, events, result,
                    variable=var, resource_kind=kind, node_id=node.id,
                )

    @staticmethod
    def kind_of(cfg: FunctionCfg, var: str) -> str:
        for node_id in cfg.order:
            node = cfg.nodes[node_id]
            if node.kind == "acquire" and node.target == var:
                return node.resource_kind or "unknown"
        return "unknown"


# ---------- serialization helpers ----------

def edge_order(edge: CfgEdge) -> Tuple[int, str, str]:
    # Normal edges before exception edges; false before true for deterministic
    # branch exploration; back edges last among normal edges.
    kind_rank = 0 if edge.kind == NORMAL else 1
    label_rank = {"false": 0, "true": 1, "": 2, "back": 3}.get(edge.label, 4)
    return (kind_rank, str(label_rank), edge.dst)


def edge_dict(edge: CfgEdge) -> dict:
    return {"src": edge.src, "dst": edge.dst, "kind": edge.kind, "label": edge.label}


def snapshot(state: Dict[str, str]) -> Dict[str, str]:
    return {var: state.get(var, ABSENT) for var in sorted(state)}


# ---------- public entry point ----------

def analyze_source(
    source_text: str,
    *,
    filename: str = "<input>",
    loop_bound: int = 2,
    max_steps: int = 10000,
) -> dict:
    """Parse, check, build CFGs and analyze ``source_text``.

    Returns a JSON-serializable dictionary. Raises a :class:`ResflowError`
    subclass on invalid input.
    """
    if loop_bound < 0:
        raise AnalysisError("loop_bound must be >= 0", None)
    source = SourceFile(source_text, filename)
    program = parse_source(source)  # lexing happens inside the parser
    funcs = check_program(program)
    cfgs = build_cfg(source, funcs)
    may_return, may_throw = compute_summaries(cfgs, funcs)
    analyzer = Analyzer(
        source, funcs, cfgs,
        loop_bound=loop_bound,
        max_steps=max_steps,
        may_return=may_return,
        may_throw=may_throw,
    )
    results = analyzer.analyze()

    functions_out = []
    for fn in funcs.values():
        cfg = cfgs[fn.name]
        res = results[fn.name]
        functions_out.append(
            {
                "name": fn.name,
                "params": list(fn.params),
                "throws": fn.throws,
                "span": fn.span.to_dict(),
                "resource_variables": cfg.resource_vars,
                "cfg": serialize_cfg(cfg),
                "paths": res.paths,
                "finding_ids": _unique_ordered(res.findings),
            }
        )

    return {
        "language": "resflow/1",
        "filename": filename,
        "options": {"loop_bound": loop_bound, "max_steps": max_steps},
        "summaries": [
            {
                "name": name,
                "throws": funcs[name].throws,
                "may_return": may_return[name],
                "may_throw": may_throw[name],
            }
            for name in funcs
        ],
        "functions": functions_out,
        "findings": [
            {**serialize_finding(f), "id": idx + 1}
            for idx, f in enumerate(analyzer.shared.findings)
        ],
        "counts": {
            "functions": len(functions_out),
            "paths": sum(len(f["paths"]) for f in functions_out),
            "findings": len(analyzer.shared.findings),
            "truncated_paths": analyzer.shared.truncated,
        },
    }


def _unique_ordered(values: List[int]) -> List[int]:
    seen: List[int] = []
    for v in values:
        if v not in seen:
            seen.append(v)
    return seen


def serialize_cfg(cfg: FunctionCfg) -> dict:
    return {
        "entry": cfg.entry,
        "exit": cfg.exit,
        "except_exit": cfg.except_exit,
        "nodes": [
            {
                "id": node_id,
                "kind": cfg.nodes[node_id].kind,
                "label": cfg.nodes[node_id].label,
                "span": cfg.nodes[node_id].span.to_dict(),
            }
            for node_id in cfg.order
        ],
        "edges": [
            {"src": e.src, "dst": e.dst, "kind": e.kind, "label": e.label}
            for e in sorted(cfg.edges, key=lambda e: (e.src, edge_order(e)))
        ],
    }


def serialize_finding(finding: Finding) -> dict:
    return {
        "id": 0,  # filled by caller below, ids are assigned in intern()
        "code": finding.code,
        "message": finding.message,
        "function": finding.function,
        "variable": finding.variable,
        "resource_kind": finding.resource_kind,
        "node_id": finding.node_id,
        "span": finding.span.to_dict(),
    }
