"""Finite HTN planning kernel.

Responsibilities (real mechanism, no hard-coded demo tables):

* Method selection: every method whose guard holds in the *current* state is a
  candidate; alternatives are tried in declaration order with full backtracking.
* Recursive decomposition bounded by an explicit depth limit and an expansion
  budget; repeating an identical task frame on the active stack is a cycle.
* Primitive feasibility: leaf actions are checked against the state (facts and
  finite resources) and their effects are simulated to thread the resulting
  state through subsequent tasks.
* Partial order: the sibling DAG admits topological orders, but a topological
  order is **not** treated as executable.  Ready siblings are interleaved by
  search and each prefix is simulated; an ordering whose preconditions fail is
  rejected and another ready sibling is attempted.
* Evidence: every rejected guard and failed branch is recorded with a typed
  failure category; when no method works, a NO_VIABLE_METHOD summary lists the
  alternatives and why each lost.
"""
from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass, field

from .models import (
    Domain,
    FailureEvidence,
    FailureKind,
    Node,
    PlanResult,
    Primitive,
    Problem,
)
from .rule_language import (
    RuleLanguageError,
    bind_method,
    bind_primitive,
    substitute_condition,
    substitute_token,
)
from .state import ConditionError, State


@dataclass
class _GuardResult:
    ok: bool
    reason: str = ""
    method: object = None
    derived: dict[str, str] = field(default_factory=dict)


@dataclass
class _Budget:
    max_depth: int
    max_expansions: int
    expansions: int = 0
    max_depth_seen: int = 0


@dataclass
class _SolveOutcome:
    ok: bool
    node_id: str
    order: list[str] = field(default_factory=list)
    state: State | None = None
    failure: FailureEvidence | None = None
    # sibling id -> child node id, populated for compound nodes
    child_by_sibling: dict[str, str] = field(default_factory=dict)
    # Fatal outcomes (resource bounds, structural cycle) propagate to the top
    # instead of being treated as one more rejected method alternative.
    fatal: bool = False


class Planner:
    def __init__(
        self,
        domain: Domain,
        audit_sink: Callable[[str, str], None] | None = None,
    ) -> None:
        self.domain = domain
        self._nodes: dict[str, Node] = {}
        self._failures: list[FailureEvidence] = []
        self._counter = 0
        self._budget: _Budget | None = None
        self._warnings: list[str] = []
        self._audit_sink = audit_sink

    def _audit(self, location: str, message: str) -> None:
        if self._audit_sink is not None:
            self._audit_sink(location, message)

    # -- public entry point ----------------------------------------------
    def plan(self, problem: Problem, request_id: str | None = None) -> PlanResult:
        if problem.domain != self.domain.name:
            self._warnings.append(
                f"problem declares domain {problem.domain!r} but planner holds "
                f"{self.domain.name!r}; proceeding with loaded domain"
            )
        self._budget = _Budget(problem.max_depth, problem.max_expansions)
        state = State.from_problem(problem, self.domain)
        self._audit(
            "kernel:plan",
            f"START goal={problem.goal_task}({', '.join(problem.goal_args)}) "
            f"domain={self.domain.name}@{self.domain.version} "
            f"depth<={problem.max_depth} expansions<={problem.max_expansions}",
        )
        for rname, (capacity, held) in state.resources.items():
            if held > capacity:
                self._warnings.append(
                    f"resource {rname!r} initially over-subscribed: "
                    f"held={held} > capacity={capacity}"
                )

        outcome = self._solve_task(
            problem.goal_task,
            problem.goal_args,
            state,
            depth=0,
            active=set(),
            path=[f"{problem.goal_task}({', '.join(problem.goal_args)})"],
            parent=None,
        )
        roots = [outcome.node_id]
        self._prune_to_reachable(roots)
        result = PlanResult(
            feasible=outcome.ok,
            request_id=request_id,
            domain=self.domain.name,
            domain_version=self.domain.version,
            problem=problem.name,
            goal_task=problem.goal_task,
            goal_args=problem.goal_args,
            execution_order=outcome.order if outcome.ok else [],
            nodes=self._nodes,
            roots=roots,
            failures=[] if outcome.ok else list(self._failures),
            abandoned_branches=list(self._failures) if outcome.ok else [],
            depth_used=self._budget.max_depth_seen,
            expansions_used=self._budget.expansions,
            warnings=self._warnings,
        )
        if outcome.ok:
            abandoned_count = len(self._failures)
            self._independently_validate(result, state)
            # If the independent pass overturned feasibility, surface the newly
            # recorded validator evidence as terminal failures.
            if not result.feasible:
                result.failures = list(self._failures[abandoned_count:])
                result.abandoned_branches = list(self._failures[:abandoned_count])
        elif outcome.failure is not None and not self._failures:
            self._failures.append(outcome.failure)
        return result

    def _prune_to_reachable(self, roots: list[str]) -> None:
        """Drop nodes created while exploring losing backtracking branches.

        Only nodes reachable from a retained root through committed children
        belong to the reported expansion tree.
        """
        reachable: set[str] = set()
        stack = list(roots)
        while stack:
            nid = stack.pop()
            if nid in reachable or nid not in self._nodes:
                continue
            reachable.add(nid)
            stack.extend(self._nodes[nid].children)
        for stray in [n for n in self._nodes if n not in reachable]:
            del self._nodes[stray]

    # -- tree helpers -----------------------------------------------------
    def _new_node(
        self, kind: str, task: str, args: list[str], depth: int
    ) -> Node:
        self._counter += 1
        node_id = f"n{self._counter}"
        node = Node(
            node_id=node_id,
            kind=kind,  # type: ignore[arg-type]
            task=task,
            args=list(args),
            depth=depth,
        )
        self._nodes[node_id] = node
        return node

    def _evidence(
        self,
        kind: FailureKind,
        detail: str,
        depth: int,
        path: list[str],
        task: str | None = None,
        method: str | None = None,
        **extra,
    ) -> FailureEvidence:
        ev = FailureEvidence(
            kind=kind,
            task=task,
            method=method,
            detail=detail,
            depth=depth,
            node_path=list(path),
            **extra,
        )
        self._failures.append(ev)
        return ev

    # -- guard evaluation (fact/not/avail/bound + deterministic bind) -----
    def _evaluate_guards(self, method_t, call_args, state: State) -> _GuardResult:
        if len(call_args) != len(method_t.parameters):
            return _GuardResult(
                ok=False,
                reason=(
                    f"arity mismatch: method {method_t.name!r} declares "
                    f"{len(method_t.parameters)} params, called with {len(call_args)}"
                ),
            )
        binding = dict(zip(method_t.parameters, call_args))
        derived: dict[str, str] = {}
        for cond in method_t.guard:
            scope = {**binding, **derived}
            if cond.kind == "bind":
                try:
                    pattern = tuple(substitute_token(a, scope) for a in cond.args)
                except RuleLanguageError as exc:
                    return _GuardResult(ok=False, reason=str(exc))
                values = state.lookup_values(cond.name, pattern)
                if len(values) != 1:
                    return _GuardResult(
                        ok=False,
                        reason=(
                            f"bind {cond.bind_target} <- {cond.name}"
                            f"{list(pattern)}: expected exactly 1 matching fact, "
                            f"found {len(values)} (deterministic lookup required)"
                        ),
                    )
                assert cond.bind_target is not None
                derived[cond.bind_target] = values[0]
                continue
            try:
                ground = substitute_condition(cond, scope)
            except RuleLanguageError as exc:
                return _GuardResult(ok=False, reason=str(exc))
            try:
                holds = state.satisfies(ground)
            except ConditionError as exc:
                return _GuardResult(ok=False, reason=str(exc))
            if not holds:
                return _GuardResult(
                    ok=False,
                    reason=f"guard literal does not hold: {ground.literal_key()}",
                )
        try:
            method = bind_method(method_t, call_args, extra_binding=derived)
        except RuleLanguageError as exc:
            return _GuardResult(ok=False, reason=str(exc))
        return _GuardResult(ok=True, method=method, derived={**binding, **derived})

    # -- core recursion ---------------------------------------------------
    def _solve_task(
        self,
        task: str,
        args: list[str],
        state: State,
        depth: int,
        active: set[tuple[tuple[str, tuple[str, ...]], tuple]],
        path: list[str],
        parent: Node | None,
    ) -> _SolveOutcome:
        assert self._budget is not None
        self._budget.max_depth_seen = max(self._budget.max_depth_seen, depth)

        if self.domain.primitives.get(task) is not None:
            return self._solve_primitive(task, args, state, depth, path)

        node = self._new_node("compound", task, args, depth)
        candidates = self.domain.methods_for(task)
        if not candidates:
            return self._unresolvable(task, args, depth, path, node)

        active_key = ((task, tuple(args)), state.signature())
        bound = self._compound_bound(task, args, depth, path, node, active_key,
                                     active)
        if bound is not None:
            return bound

        tried: list[str] = []
        rejected_guards: list[dict] = []
        for method_t in candidates:
            tried.append(method_t.name)
            attempt, guard_reason = self._try_method(
                method_t, task, args, state, depth, path, node, active, active_key,
                candidate_no=tried.index(method_t.name) + 1,
                candidate_count=len(candidates),
            )
            if attempt is None:
                # Guard failed; remember why and continue to the next method.
                rejected_guards.append(
                    {"method": method_t.name, "reason": guard_reason}
                )
                continue
            if attempt.ok or attempt.fatal:
                return attempt
            # Non-fatal branch failure: evidence recorded, try next method.

        node.status = "failed"
        node.detail = f"all {len(tried)} methods tried and rejected or infeasible"
        failure = self._evidence(
            FailureKind.NO_VIABLE_METHOD,
            f"no viable method for {task}({', '.join(args)}) at depth {depth}; "
            f"tried={tried}",
            depth, path, task=task, tried_methods=tried,
            rejected_guards=rejected_guards,
        )
        return _SolveOutcome(False, node.node_id, failure=failure)

    def _solve_primitive(self, task, args, state: State, depth: int,
                         path: list[str]) -> _SolveOutcome:
        """Bind and verify a primitive leaf against the current state."""
        node = self._new_node("primitive", task, args, depth)
        template = self.domain.primitives[task]
        try:
            prim = bind_primitive(template, args)
            executable, reason = state.can_execute(prim)
        except (RuleLanguageError, ConditionError) as exc:
            node.status = "failed"
            node.detail = str(exc)
            failure = self._evidence(
                FailureKind.PRECONDITION_NOT_STAT,
                f"malformed primitive invocation: {exc}",
                depth, path, task=task,
            )
            return _SolveOutcome(False, node.node_id, failure=failure)
        if not executable:
            node.status = "failed"
            node.detail = reason
            kind = (
                FailureKind.RESOURCE_UNAVAILABLE
                if "resource" in (reason or "")
                else FailureKind.PRECONDITION_NOT_STAT
            )
            self._audit(
                f"kernel:primitive@{depth}",
                f"REJECT action {task}({', '.join(args)}): {reason}",
            )
            failure = self._evidence(
                kind, f"action {task}({', '.join(args)}) not executable: {reason}",
                depth, path, task=task,
            )
            return _SolveOutcome(False, node.node_id, failure=failure)
        node.status = "executable"
        node.primitive = task
        return _SolveOutcome(
            True, node.node_id, order=[node.node_id], state=state.apply(prim),
        )

    def _unresolvable(self, task, args, depth, path, node: Node) -> _SolveOutcome:
        node.status = "failed"
        node.detail = "no primitive and no decomposition method is defined"
        failure = self._evidence(
            FailureKind.UNRESOLVABLE_TASK,
            f"task {task}({', '.join(args)}) has neither a primitive action "
            "nor any applicable method",
            depth, path, task=task,
        )
        return _SolveOutcome(False, node.node_id, failure=failure)

    def _compound_bound(self, task, args, depth, path, node, active_key,
                        active) -> _SolveOutcome | None:
        """Return a fatal bound outcome (cycle/depth), or None if clear."""
        if active_key in active:
            node.status = "failed"
            node.detail = (
                "identical task frame recurses with an unchanged state signature"
            )
            failure = self._evidence(
                FailureKind.METHOD_CYCLE,
                f"task {task}({', '.join(args)}) recurses into itself without "
                f"state progress along {' > '.join(path)}",
                depth, path, task=task,
            )
            return _SolveOutcome(False, node.node_id, failure=failure, fatal=True)
        assert self._budget is not None
        if depth >= self._budget.max_depth:
            node.status = "failed"
            node.detail = f"depth limit {self._budget.max_depth} reached"
            failure = self._evidence(
                FailureKind.DEPTH_EXCEEDED,
                f"decomposition of {task}({', '.join(args)}) exceeded depth "
                f"limit {self._budget.max_depth}",
                depth, path, task=task,
            )
            return _SolveOutcome(False, node.node_id, failure=failure, fatal=True)
        return None

    def _try_method(self, method_t, task, args, state: State, depth: int,
                    path: list[str], node: Node, active, active_key,
                    candidate_no: int, candidate_count: int,
                    ) -> tuple[_SolveOutcome | None, str | None]:
        """Attempt one method.

        Returns ``(outcome, None)`` when the guard held, or
        ``(None, reason)`` when the guard rejected it.
        """
        guard = self._evaluate_guards(method_t, args, state)
        if not guard.ok:
            self._evidence(
                FailureKind.GUARD_REJECTED,
                f"method {method_t.name!r} for {task}({', '.join(args)}) "
                f"rejected: {guard.reason}",
                depth, path, task=task, method=method_t.name,
            )
            return None, guard.reason
        method = guard.method
        self._audit(
            f"kernel:method-select@{depth}",
            f"SELECT {method.name} for {task}({', '.join(args)}) "
            f"(candidate {candidate_no}/{candidate_count})",
        )
        assert self._budget is not None
        if self._budget.expansions >= self._budget.max_expansions:
            node.status = "failed"
            failure = self._evidence(
                FailureKind.EXPANSION_BUDGET_EXHAUSTED,
                f"expansion budget {self._budget.max_expansions} exhausted "
                f"before applying method {method.name!r}",
                depth, path, task=task, method=method.name,
            )
            return _SolveOutcome(False, node.node_id, failure=failure,
                                 fatal=True), None

        self._budget.expansions += 1
        child_path = path + [f"{task}({', '.join(args)})/{method.name}"]
        outcome = self._solve_siblings(
            method, state, depth, active | {active_key}, child_path, node,
        )
        if outcome.ok:
            node.method = method.name
            node.status = "expanded"
            node.guard_conditions = [c.model_dump() for c in method.guard]
            node.guard_snapshot = state.fact_list()
            self._commit_children(method, node, outcome)
            self._audit(
                f"kernel:expand@{depth}",
                f"EXPAND {task} via {method.name} -> "
                f"{len(outcome.order)} ordered leaf action(s)",
            )
            return _SolveOutcome(
                True, node.node_id, order=outcome.order, state=outcome.state
            ), None
        if outcome.fatal:
            return _SolveOutcome(
                False, node.node_id, failure=outcome.failure, fatal=True
            ), None
        # Ordinary infeasible branch: caller tries the next method.
        return _SolveOutcome(False, node.node_id, failure=outcome.failure), None


    # -- sibling ordering (sequential / partial) --------------------------
    def _solve_siblings(
        self, method, state: State, depth: int,
        active: set[tuple[tuple[str, tuple[str, ...]], tuple]],
        path: list[str], parent: Node,
    ) -> _SolveOutcome:
        """Solve a method body.

        The parent tree node is **not** mutated here: children created while a
        losing branch is explored must not leak into the retained tree.  Only on
        success is ``child_by_sibling`` handed back to the caller, which commits
        the children and their ``after`` edges atomically.
        """
        by_id = {st.id: st for st in method.subtasks}
        if method.order == "sequential":
            return self._solve_sequential(
                method, state, depth, active, path, parent, by_id
            )
        return self._solve_partial(method, state, depth, active, path, parent, by_id)

    def _solve_sequential(self, method, state: State, depth: int, active,
                          path: list[str], parent: Node, by_id) -> _SolveOutcome:
        order: list[str] = []
        child_by_sibling: dict[str, str] = {}
        current = state
        for st in method.subtasks:
            child = self._solve_task(
                st.task, st.args, current, depth + 1, active,
                path + [f"{st.id}:{st.task}({', '.join(st.args)})"], parent,
            )
            if not child.ok:
                return _SolveOutcome(
                    False, parent.node_id, failure=child.failure, fatal=child.fatal
                )
            child_by_sibling[st.id] = child.node_id
            order.extend(child.order)
            assert child.state is not None
            current = child.state
        return _SolveOutcome(
            True, parent.node_id, order=order, state=current,
            child_by_sibling=child_by_sibling,
        )

    def _solve_partial(self, method, state: State, depth: int, active,
                       path: list[str], parent: Node, by_id) -> _SolveOutcome:
        """Interleave ready siblings, simulating every prefix.

        A topological ordering alone is never accepted: each next ready sibling
        is actually executed against the simulated state and precondition
        failures are treated as a failed ordering, not a structural error.
        """
        declared = [st.id for st in method.subtasks]
        deps = {sid: set(by_id[sid].after) for sid in declared}
        failed_prefixes: list[str] = []

        def search(done: frozenset[str], current: State, prefix: list[str],
                   chosen: dict[str, str]) -> _SolveOutcome | None:
            remaining = [sid for sid in declared if sid not in done]
            if not remaining:
                return _SolveOutcome(
                    True, parent.node_id, order=list(prefix), state=current,
                    child_by_sibling=dict(chosen),
                )
            ready = [sid for sid in remaining if deps[sid] <= done]
            if not ready:
                self._evidence(
                    FailureKind.PARTIAL_ORDER_INFEASIBLE,
                    f"method {method.name!r}: remaining {remaining} deadlock on "
                    "unsatisfiable order constraints",
                    depth, path, method=method.name,
                )
                return None
            for sid in ready:
                found, fatal, note = self._attempt_ready_sibling(
                    sid, by_id[sid], current, depth, active, path, parent,
                    done, prefix, chosen, search,
                )
                if fatal is not None:
                    return fatal
                if found is not None:
                    return found
                if note is not None:
                    failed_prefixes.append(note)
            return None

        found = search(frozenset(), state, [], {})
        if found is not None:
            return found
        failure = self._evidence(
            FailureKind.PARTIAL_ORDER_INFEASIBLE,
            f"method {method.name!r}: {len(declared)} siblings admit topological "
            "orders but no ready interleaving is executable from the current "
            f"state. Attempted-ready failures: {failed_prefixes}",
            depth, path, task=method.task, method=method.name,
        )
        return _SolveOutcome(False, parent.node_id, failure=failure)

    def _attempt_ready_sibling(self, sid, subtask, current: State, depth: int,
                               active, path: list[str], parent: Node,
                               done: frozenset[str], prefix: list[str],
                               chosen: dict[str, str], continue_search):
        """Try one ready sibling in a partial-order DFS.

        Returns ``(found, fatal, note)``: a successful continuation, a fatal
        bound that must propagate, or a human-readable failed-prefix note.
        """
        child = self._solve_task(
            subtask.task, subtask.args, current, depth + 1, active,
            path + [f"{sid}:{subtask.task}({', '.join(subtask.args)})"], parent,
        )
        if child.fatal:
            return None, _SolveOutcome(
                False, parent.node_id, failure=child.failure, fatal=True
            ), None
        if not child.ok:
            return None, None, (
                f"ready sibling {sid!r} failed at this prefix -> "
                f"{child.failure.kind.value if child.failure else 'unknown'}"
            )
        assert child.state is not None
        chosen_next = dict(chosen)
        chosen_next[sid] = child.node_id
        found = continue_search(
            done | {sid}, child.state, prefix + child.order, chosen_next
        )
        return found, None, None

    def _commit_children(self, method, node: Node, outcome: _SolveOutcome) -> None:
        """Atomically attach winning children (declaration order) + after edges."""
        by_id = {st.id: st for st in method.subtasks}
        node.children = [outcome.child_by_sibling[st.id] for st in method.subtasks]
        for st in method.subtasks:
            child_node = self._nodes[outcome.child_by_sibling[st.id]]
            child_node.after = [
                outcome.child_by_sibling[d] for d in st.after
            ]

    # -- independent post-construction validation -------------------------
    def _independently_validate(self, result: PlanResult, initial: State) -> None:
        """Re-simulate the retained tree without trusting the search's state.

        Confirms (1) every leaf action is executable in the returned order,
        (2) effects reconcile and resources never exceed capacity, and
        (3) the leaf order respects every recorded partial-order edge.  This is
        deliberately a second, independent pass over the retained tree rather
        than a re-read of search bookkeeping.
        """
        problems: list[str] = []
        current = initial.clone()
        position: dict[str, int] = {}
        for index, node_id in enumerate(result.execution_order):
            node = self._nodes[node_id]
            if node.kind != "primitive" or node.primitive is None:
                problems.append(f"{node_id}: execution order contains non-leaf")
                continue
            template = self.domain.primitives[node.primitive]
            try:
                prim = bind_primitive(template, node.args)
            except RuleLanguageError as exc:
                problems.append(f"{node_id}: rebind failed: {exc}")
                continue
            ok, reason = current.can_execute(prim)
            if not ok:
                problems.append(f"{node_id}: not executable in order: {reason}")
                continue
            current = current.apply(prim)
            position[node_id] = index

        # Partial-order edges recorded as node.after on the winning tree.
        for node in self._nodes.values():
            for child_id in node.children:
                child = self._nodes[child_id]
                for dep_id in child.after:
                    for b in self._leaf_descendants(self._nodes[dep_id]):
                        for a in self._leaf_descendants(child):
                            if position.get(b, -1) >= position.get(a, len(position)):
                                problems.append(
                                    f"order constraint violated: leaf {b} must "
                                    f"precede {a}"
                                )

        if problems:
            result.feasible = False
            result.execution_order = []
            for p in problems:
                self._evidence(
                    FailureKind.PARTIAL_ORDER_INFEASIBLE,
                    f"post-construction validation failed: {p}",
                    0, ["<validator>"],
                )

    def _leaf_descendants(self, node: Node) -> list[str]:
        if node.kind == "primitive":
            return [node.node_id]
        leaves: list[str] = []
        for cid in node.children:
            leaves.extend(self._leaf_descendants(self._nodes[cid]))
        return leaves
