"""Independent verification of planner output.

This module deliberately does **not** import the planner's search engine.
It answers, from scratch:

1. Is the action sequence executable from the problem's initial state?
   Every operator precondition is re-evaluated against an independently
   replayed state; effects are applied delete-then-add.
2. Does the sequence respect the *hierarchy constraints* of the expansion
   tree?  For every expanded compound task, the relative order of the
   primitive descendants of its children must admit at least one linear
   extension consistent with the method's declared precedence; for ordered
   methods the descendants of child ``i`` must all precede those of child
   ``i+1``.  The tree must also cover exactly the actions returned.
3. Is the reported partial order consistent with the plan (plan is a
   linearization of the root task network), and is the order graph a DAG?

A topological sort is *not* treated as executability: item 1 alone decides
executability; items 2-3 decide hierarchy/order consistency.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any

from .lang import (
    Atom,
    Domain,
    Problem,
    ground_fact,
    ground_literal,
    literal_holds,
    satisfy_preconditions,
    unify_heads,
)


@dataclass(frozen=True)
class Violation:
    code: str
    detail: str

    def to_dict(self) -> dict[str, str]:
        return {"code": self.code, "detail": self.detail}


@dataclass
class VerificationReport:
    executable: bool
    hierarchy_consistent: bool
    order_consistent: bool
    failure_sound: bool
    ok: bool
    final_state: list[list[Any]] = field(default_factory=list)
    violations: list[Violation] = field(default_factory=list)
    # Linearizations of the aggregate precedence graph that are executable
    # (independently re-simulated).  Bounded enumeration for tractability.
    feasible_linearizations: list[list[str]] = field(default_factory=list)
    linearizations_examined: int = 0
    linearizations_truncated: bool = False
    method_evidence: list[dict[str, Any]] = field(default_factory=list)
    failure_checks: list[dict[str, Any]] = field(default_factory=list)

    def to_dict(self) -> dict[str, Any]:
        return {
            "ok": self.ok,
            "executable": self.executable,
            "hierarchy_consistent": self.hierarchy_consistent,
            "order_consistent": self.order_consistent,
            "failure_sound": self.failure_sound,
            "final_state": self.final_state,
            "violations": [v.to_dict() for v in self.violations],
            "feasible_linearizations": self.feasible_linearizations,
            "linearizations_examined": self.linearizations_examined,
            "linearizations_truncated": self.linearizations_truncated,
            "method_evidence": self.method_evidence,
            "failure_checks": self.failure_checks,
        }


# Bounds on independent enumeration so a check can never hang a request.
MAX_LINEARIZATIONS = 5000


class Verifier:
    """Re-derives correctness properties without using the planner search."""

    def verify(
        self,
        domain: Domain,
        problem: Problem,
        result: Any,
    ) -> VerificationReport:
        violations: list[Violation] = []

        # The recorded action prefix is replayed independently regardless of
        # the verdict: even a failure branch may have executed some actions,
        # and every one of those actions must really have been applicable.
        state_ok, final_state, replay_violations = self._replay(domain, problem, result)
        violations.extend(replay_violations)

        failure_sound = True
        method_evidence: list[dict[str, Any]] = []
        feasible: list[list[str]] = []
        examined = 0
        truncated = False
        hierarchy_ok = True
        order_ok = True

        if result.status == "success":
            hierarchy_ok, hier_violations, method_evidence = self._check_hierarchy(
                domain, result
            )
            violations.extend(hier_violations)
            order_ok, order_violations, feasible, examined, truncated = self._check_orders(
                domain, problem, result
            )
            violations.extend(order_violations)
        elif result.status == "failure":
            # No successful expansion tree exists; independently corroborate
            # each claimed dead end instead of requiring one.
            hierarchy_ok = order_ok = True  # vacuous: no plan to check
            failure_sound, fail_violations, fail_checks = self._check_failures(
                domain, problem, result
            )
            violations.extend(fail_violations)
        else:  # inconclusive: budgets cut the search before a verdict
            hierarchy_ok = order_ok = True
            failure_sound = False
            violations.append(
                Violation(
                    "inconclusive_not_verifiable",
                    "search was truncated by a budget; neither feasibility nor"
                    " infeasibility was established, so no correctness claim"
                    " can be confirmed",
                )
            )

        ok = state_ok and hierarchy_ok and order_ok and failure_sound
        return VerificationReport(
            executable=state_ok,
            hierarchy_consistent=hierarchy_ok,
            order_consistent=order_ok,
            failure_sound=failure_sound,
            ok=ok,
            final_state=[list(f) for f in sorted(final_state, key=self._state_key)],
            violations=violations,
            feasible_linearizations=feasible,
            linearizations_examined=examined,
            linearizations_truncated=truncated,
            method_evidence=method_evidence,
            failure_checks=fail_checks if result.status == "failure" else [],
        )

    # ------------------------------------------------------------------ #
    # 1. independent state replay
    # ------------------------------------------------------------------ #

    def _replay(
        self, domain: Domain, problem: Problem, result: Any
    ) -> tuple[bool, frozenset[tuple[Atom, ...]], list[Violation]]:
        violations: list[Violation] = []
        state = frozenset(problem.init)
        actions = list(result.plan)
        for step in actions:
            op = domain.operators.get(step.operator)
            if op is None:
                violations.append(
                    Violation("unknown_operator", f"action {step.seq}: {step.operator}")
                )
                return False, state, violations
            if len(step.args) != len(op.params):
                violations.append(
                    Violation(
                        "arity_mismatch",
                        f"action {step.seq} {op.name}: {len(step.args)} args"
                        f" vs {len(op.params)} params",
                    )
                )
                return False, state, violations
            env = unify_heads(op.params, tuple(step.args), {})
            if env is None:
                violations.append(
                    Violation("binding_failure", f"action {step.seq} {op.name}")
                )
                return False, state, violations
            for lit in op.pre:
                grounded = ground_literal(lit, env)
                if not literal_holds(grounded, state):
                    violations.append(
                        Violation(
                            "precondition_failed",
                            f"action {step.seq} {op.name}{tuple(step.args)}:"
                            f" required {grounded}, state lacked/held it incorrectly",
                        )
                    )
                    return False, state, violations
            deletes = {ground_fact(f, env) for f in op.delete}
            adds = {ground_fact(f, env) for f in op.add}
            state = frozenset((set(state) - deletes) | adds)
        return True, state, violations

    @staticmethod
    def _state_key(fact: tuple[Atom, ...]) -> tuple[str, ...]:
        return tuple(map(str, fact))

    def _replay_actions(
        self,
        domain: Domain,
        init: frozenset[tuple[Atom, ...]],
        actions: list[tuple[str, tuple[Atom, ...]]],
    ) -> tuple[frozenset[tuple[Atom, ...]], str | None]:
        """Replay generic (operator, args) pairs; return (state, error)."""
        state = frozenset(init)
        for index, (op_name, args) in enumerate(actions, start=1):
            op = domain.operators.get(op_name)
            if op is None:
                return state, f"prefix action {index}: unknown operator {op_name}"
            env = unify_heads(op.params, tuple(args), {})
            if env is None:
                return state, f"prefix action {index}: binding failure"
            for lit in op.pre:
                if not literal_holds(ground_literal(lit, env), state):
                    return (
                        state,
                        f"prefix action {index} {op_name}{tuple(args)}:"
                        f" precondition {ground_literal(lit, env)} failed",
                    )
            deletes = {ground_fact(f, env) for f in op.delete}
            adds = {ground_fact(f, env) for f in op.add}
            state = frozenset((set(state) - deletes) | adds)
        return state, None

    # ------------------------------------------------------------------ #
    # 2b. independent corroboration of definitive failures
    # ------------------------------------------------------------------ #

    def _check_failures(
        self,
        domain: Domain,
        problem: Problem,
        result: Any,
    ) -> tuple[bool, list[Violation], list[dict[str, Any]]]:
        """Each claimed dead end must really be one when re-derived.

        Crucially this never asks the planner: prefixes are replayed from the
        problem init, method preconditions are re-evaluated, and minimality in
        the residual task graph is recomputed from the snapshot edges.
        """
        violations: list[Violation] = []
        checks: list[dict[str, Any]] = []
        if not result.failures:
            violations.append(
                Violation(
                    "failure_without_evidence",
                    "planner reported failure but supplied no failure records",
                )
            )
            return False, violations, checks

        for record in result.failures:
            prefix = [
                (name, tuple(args)) for name, args in record.action_prefix
            ]
            state, err = self._replay_actions(domain, problem.init, prefix)
            if err is not None:
                violations.append(Violation("unsound_failure_prefix", err))
                checks.append({"category": record.category, "sound": False, "error": err})
                continue

            if record.category == "no_applicable_method":
                sound, detail = self._check_no_method(domain, record, state)
            elif record.category == "deadlock":
                sound, detail = self._check_deadlock(domain, record, state)
            elif record.category == "cycle":
                sound, detail = self._check_cycle(domain, record, state)
            else:
                sound, detail = False, f"unexpected definitive category {record.category}"
            checks.append({"category": record.category, "sound": sound, "detail": detail})
            if not sound:
                violations.append(
                    Violation(
                        "unsound_failure",
                        f"{record.category} at {record.task}: {detail}",
                    )
                )
        return not violations, violations, checks

    def _check_no_method(
        self, domain: Domain, record: Any, state: frozenset[tuple[Atom, ...]]
    ) -> tuple[bool, str]:
        task = tuple(record.task)
        head = str(task[0])
        methods = domain.methods.get(head, ())
        if not methods:
            return False, f"domain has no methods for {head}"
        named = {m.name for m in methods}
        cited = {m.get("method") for m in record.rejected_methods}
        if cited != named:
            return False, f"rejected set {sorted(str(c) for c in cited)} != domain methods {sorted(named)}"
        args = tuple(task[1:])
        for method in methods:
            env = unify_heads(method.params, args, {})
            if env is None:
                continue
            if satisfy_preconditions(method.pre, env, state):
                return (
                    False,
                    f"method {method.name} IS applicable in the replayed state;"
                    f" the no-method dead end is spurious",
                )
        return True, f"independently confirmed all {len(methods)} method(s) inapplicable"

    def _check_deadlock(
        self, domain: Domain, record: Any, state: frozenset[tuple[Atom, ...]]
    ) -> tuple[bool, str]:
        tasks = {t["node"]: tuple(t["task"]) for t in record.remaining_tasks}
        if not tasks:
            return False, "deadlock snapshot has no remaining tasks"
        incoming: dict[str, set[str]] = {tid: set() for tid in tasks}
        for before, after in record.remaining_edges:
            if before in tasks and after in tasks:
                incoming[after].add(before)
        minimal = {tid for tid, preds in incoming.items() if not preds}
        if not minimal:
            return False, "residual graph has a cycle, so it is not a partial-order DAG"

        executable_minimals = 0
        reducible_minimals = 0
        for tid in minimal:
            expr = tasks[tid]
            head = str(expr[0])
            if head.startswith("!"):
                op = domain.operators.get(head)
                if op is None:
                    return False, f"minimal task uses unknown operator {head}"
                env = unify_heads(op.params, tuple(expr[1:]), {})
                if env is not None and all(
                    literal_holds(ground_literal(lit, env), state) for lit in op.pre
                ):
                    executable_minimals += 1
            else:
                for method in domain.methods.get(head, ()):
                    env = unify_heads(method.params, tuple(expr[1:]), {})
                    if env is not None and satisfy_preconditions(
                        method.pre, env, state
                    ):
                        reducible_minimals += 1
                        break
        if executable_minimals:
            return False, f"{executable_minimals} minimal action(s) are actually executable"
        if reducible_minimals:
            return False, f"{reducible_minimals} minimal compound task(s) still reducible"

        # Independently confirm the cited per-task unmet literals really fail.
        for blocked in record.blocked:
            btask = tuple(blocked["task"])
            op = domain.operators.get(str(btask[0]))
            if op is None:
                return False, f"blocked task names unknown operator {btask[0]}"
            env = unify_heads(op.params, tuple(btask[1:]), {})
            if env is None:
                return False, "blocked task arity mismatch"
            failing = [
                str(ground_literal(lit, env))
                for lit in op.pre
                if not literal_holds(ground_literal(lit, env), state)
            ]
            if not failing:
                return False, f"blocked task {list(btask)} has no failing precondition"
        return (
            True,
            f"independently confirmed {len(minimal)} minimal task(s) all stuck;"
            " no legal progress exists in the replayed state",
        )

    def _check_cycle(
        self, domain: Domain, record: Any, state: frozenset[tuple[Atom, ...]]
    ) -> tuple[bool, str]:
        task = tuple(record.task) if record.task else None
        if task is None:
            return False, "cycle record has no task"
        head = str(task[0])
        methods = domain.methods.get(head)
        if head.startswith("!") or not methods:
            return False, f"{head} is not a compound task with methods"

        chain = [tuple(t) for t in record.cycle_chain]
        if len(chain) < 2 or chain[0] != chain[-1] or tuple(chain[-1]) != task:
            return (
                False,
                f"cycle_chain must begin and end on the same task; got"
                f" {[list(t) for t in chain]}",
            )

        # Independently re-derive an *immediate* non-progress loop: a method
        # applicable in the replayed state whose subtasks reintroduce the same
        # ground task without a single primitive action between them (so no
        # state change can occur and the reduction cannot make progress).
        args = tuple(task[1:])
        for method in methods:
            env = unify_heads(method.params, args, {})
            if env is None or not satisfy_preconditions(method.pre, env, state):
                continue
            grounded_children = [
                tuple(ground_fact(child, env)) for child in method.subtasks
            ]
            has_primitive = any(str(c[0]).startswith("!") for c in grounded_children)
            reintroduces = task in grounded_children
            if reintroduces and not has_primitive:
                return (
                    True,
                    f"method {method.name} is applicable and immediately"
                    f" reintroduces {list(task)} with no primitive action in"
                    f" between: independently confirmed non-progress loop",
                )
        return (
            False,
            "no applicable method independently reintroduces the identical"
            " task without an intervening primitive; the engine's cycle prune"
            " cannot be confirmed here (deeper loop structure)",
        )

    # ------------------------------------------------------------------ #
    # 2. hierarchy constraints from the expansion tree
    # ------------------------------------------------------------------ #

    def _check_hierarchy(
        self, domain: Domain, result: Any
    ) -> tuple[bool, list[Violation], list[dict[str, Any]]]:
        violations: list[Violation] = []
        evidence: list[dict[str, Any]] = []
        tree = {record.node_id: record for record in result.expansion}
        if self._root_record(tree) is None:
            return False, [Violation("malformed_tree", "no root network node")], evidence

        # Map action tree nodes to their *execution* position via node_id:
        # leaf node ids are allocation order, not execution order (a task
        # expanded late can execute before an earlier-allocated sibling), so
        # the position map must come from the plan itself.
        position: dict[str, int] = {
            step.node_id: step.seq - 1 for step in result.plan
        }
        primitive_nodes = [r for r in result.expansion if r.kind == "primitive"]
        if len(primitive_nodes) != len(result.plan):
            violations.append(
                Violation(
                    "tree_action_coverage",
                    f"{len(primitive_nodes)} primitive leaves but"
                    f" {len(result.plan)} actions",
                )
            )

        for record in primitive_nodes:
            if record.node_id not in position:
                violations.append(
                    Violation(
                        "leaf_not_executed",
                        f"leaf {record.node_id} {record.operator} is absent"
                        f" from the action sequence",
                    )
                )
                continue
            plan_op = result.plan[position[record.node_id]]
            if record.operator != plan_op.operator or tuple(record.task[1:]) != tuple(
                plan_op.args
            ):
                violations.append(
                    Violation(
                        "tree_action_mismatch",
                        f"leaf {record.node_id} is {record.operator}{tuple(record.task[1:])}"
                        f" but plan position {position[record.node_id] + 1} is"
                        f" {plan_op.operator}{tuple(plan_op.args)}",
                    )
                )

        # Every internal node: children exist, and the leaves under the
        # children occur in an order compatible with the method precedence.
        for record in result.expansion:
            if record.kind not in ("compound", "network"):
                continue
            if record.kind == "network" and not record.children:
                # An empty root network is a legitimate trivial success.
                continue
            # A compound method may legitimately expand to an empty task
            # network (a no-op/'already achieved' method).
            if not record.children:
                continue
            missing = [c for c in record.children if c not in tree]
            if missing:
                violations.append(
                    Violation(
                        "dangling_child",
                        f"node {record.node_id} references missing children {missing}",
                    )
                )
                continue
            child_leaves = {
                c: self._leaf_positions(tree, c, position) for c in record.children
            }
            ok, detail = self._child_order_ok(record, child_leaves)
            evidence.append(
                {
                    "node": record.node_id,
                    "task": list(record.task),
                    "expanded_by": record.expanded_by,
                    "ordered": record.ordered,
                    "detail": detail,
                }
            )
            if not ok:
                violations.append(
                    Violation(
                        "hierarchy_order_violation",
                        f"node {record.node_id} {list(record.task)}: {detail}",
                    )
                )

            # Independent re-check that the recorded method preconditions
            # could have held: method must exist with matching subtask heads.
            if record.kind == "compound":
                method = self._find_method(domain, record)
                if method is None:
                    violations.append(
                        Violation(
                            "unknown_method",
                            f"node {record.node_id} claims method"
                            f" {record.expanded_by}",
                        )
                    )
                else:
                    actual_heads = [
                        str(tree[c].task[0]) for c in record.children
                    ]
                    expected_heads = [str(s[0]) for s in method.subtasks]
                    if actual_heads != expected_heads:
                        violations.append(
                            Violation(
                                "method_shape_mismatch",
                                f"node {record.node_id}: method {method.name}"
                                f" expands to {expected_heads}, tree has"
                                f" {actual_heads}",
                            )
                        )

        return not violations, violations, evidence

    @staticmethod
    def _root_record(tree: dict[str, Any]) -> Any:
        return tree.get("root")

    @staticmethod
    def _find_method(domain: Domain, record: Any) -> Any:
        head = str(record.task[0])
        for method in domain.methods.get(head, ()):
            if method.name == record.expanded_by:
                if len(method.params) != len(record.task) - 1:
                    continue
                return method
        return None

    def _leaf_positions(
        self,
        tree: dict[str, Any],
        node_id: str,
        position: dict[str, int],
    ) -> list[int]:
        node = tree[node_id]
        if node.kind == "primitive":
            return [position[node_id]] if node_id in position else []
        positions: list[int] = []
        for child in node.children:
            positions.extend(self._leaf_positions(tree, child, position))
        return sorted(positions)

    def _child_order_ok(
        self,
        record: Any,
        child_leaves: dict[str, list[int]],
    ) -> tuple[bool, str]:
        child_ids = list(record.children)
        # Position index map for constraint lookup.
        idx = {cid: i for i, cid in enumerate(child_ids)}

        # Internal precedence: for ordered nodes, full chain; else declared.
        if record.ordered:
            precedences = [
                (child_ids[i], child_ids[i + 1]) for i in range(len(child_ids) - 1)
            ]
        else:
            precedences = [
                (child_ids[i], child_ids[j]) for i, j in record.ordering
            ]

        for before, after in precedences:
            before_pos = child_leaves.get(before, [])
            after_pos = child_leaves.get(after, [])
            if not before_pos or not after_pos:
                return False, f"empty leaf set around edge {before}->{after}"
            if max(before_pos) >= min(after_pos):
                return (
                    False,
                    f"precedence {before}->{after} violated: descendant actions"
                    f" interleave or invert (positions {before_pos} vs"
                    f" {after_pos})",
                )

        # For partial orders, every pair of *unrelated* children must at
        # least be non-contradictory: we already enforce declared edges;
        # interleaving among unrelated children is legal.
        _ = idx
        return True, "all declared precedence edges respected"

    # ------------------------------------------------------------------ #
    # 3. order consistency + independent linearization executability
    # ------------------------------------------------------------------ #

    def _check_orders(
        self,
        domain: Domain,
        problem: Problem,
        result: Any,
    ) -> tuple[bool, list[Violation], list[list[str]], int, bool]:
        violations: list[Violation] = []

        # Build the aggregate precedence graph over *action positions* from
        # the root network plus every expansion independently.
        n = len(result.plan)
        if n == 0:
            return True, violations, [], 0, False

        # Leaf order within the tree must itself be a DAG linearization.
        tree = {r.node_id: r for r in result.expansion}
        leaf_index = {step.node_id: step.seq - 1 for step in result.plan}

        edges: set[tuple[int, int]] = set()
        for record in result.expansion:
            if record.kind not in ("compound", "network") or not record.children:
                continue
            groups = [
                set(self._leaf_positions(tree, c, leaf_index))
                for c in record.children
            ]
            if record.ordered:
                pairs = [
                    (record.children[i], record.children[i + 1])
                    for i in range(len(record.children) - 1)
                ]
            else:
                pairs = [(record.children[i], record.children[j])
                         for i, j in record.ordering]
            group_of = {
                c: gi for gi, c in enumerate(record.children)
            }
            for before, after in pairs:
                gb = groups[group_of[before]]
                ga = groups[group_of[after]]
                for x in gb:
                    for y in ga:
                        if x == y:
                            violations.append(
                                Violation(
                                    "shared_leaf",
                                    f"action at position {x + 1} on both sides"
                                    f" of edge {before}->{after}",
                                )
                            )
                        edges.add((x, y))

        cycle = self._first_cycle(n, edges)
        if cycle is not None:
            violations.append(
                Violation("order_cycle", f"precedence cycle at positions {cycle}")
            )

        # The returned plan must respect every aggregate edge.
        for x, y in edges:
            if x >= n or y >= n:
                continue
            if x > y:
                violations.append(
                    Violation(
                        "plan_not_linearization",
                        f"action position {x + 1} must precede {y + 1}"
                        f" but appears after it",
                    )
                )

        # Independently enumerate topological orders of the aggregate
        # precedence DAG reconstructed above, replaying each from the
        # problem's initial state.  This is the crux check: a topological
        # ordering is *not* assumed executable; only an order whose every
        # operator precondition holds during replay counts.  A task network
        # can be a perfectly good DAG yet have zero feasible linearizations
        # (shared-resource / partial-order interference).
        feasible: list[list[str]] = []
        examined = 0
        truncated = False
        if not any(v.code == "order_cycle" for v in violations):
            dag_edges = {(x, y) for x, y in edges if x < n and y < n}
            for order in self._topological_orders(n, dag_edges, MAX_LINEARIZATIONS):
                examined += 1
                labels = [
                    f"{result.plan[p].operator}{tuple(result.plan[p].args)}"
                    for p in order
                ]
                if self._replay_order(domain, problem, result, order):
                    feasible.append(labels)
                if examined >= MAX_LINEARIZATIONS:
                    truncated = True
                    break

        return not violations, violations, feasible, examined, truncated

    @staticmethod
    def _topological_orders(
        n: int, edges: set[tuple[int, int]], limit: int
    ) -> list[list[int]]:
        """Enumerate (bounded) all topological orders of a DAG on 0..n-1."""
        succ: dict[int, set[int]] = {i: set() for i in range(n)}
        indeg: dict[int, int] = {i: 0 for i in range(n)}
        for x, y in edges:
            if y not in succ[x]:
                succ[x].add(y)
                indeg[y] += 1
        results: list[list[int]] = []

        def backtrack(available: list[int], order: list[int], deg: dict[int, int]) -> None:
            if len(results) >= limit:
                return
            if len(order) == n:
                results.append(list(order))
                return
            for pos in range(len(available)):
                node = available[pos]
                rest = available[:pos] + available[pos + 1:]
                for nxt in sorted(succ[node]):
                    deg[nxt] -= 1
                    if deg[nxt] == 0:
                        rest.append(nxt)
                order.append(node)
                backtrack(sorted(rest), order, deg)
                order.pop()
                for nxt in succ[node]:
                    deg[nxt] += 1
                if len(results) >= limit:
                    return

        start = sorted(i for i in range(n) if indeg[i] == 0)
        backtrack(start, [], dict(indeg))
        return results

    @staticmethod
    def _first_cycle(n: int, edges: set[tuple[int, int]]) -> list[int] | None:
        succ: dict[int, set[int]] = {i: set() for i in range(n)}
        for x, y in edges:
            if x < n and y < n:
                succ[x].add(y)
        color: dict[int, int] = {}

        def dfs(node: int, stack: list[int]) -> list[int] | None:
            color[node] = 1
            stack.append(node)
            for nxt in sorted(succ[node]):
                if color.get(nxt, 0) == 0:
                    found = dfs(nxt, stack)
                    if found:
                        return found
                elif color.get(nxt) == 1:
                    return stack[stack.index(nxt):] + [nxt]
            stack.pop()
            color[node] = 2
            return None

        for i in range(n):
            if color.get(i, 0) == 0:
                found = dfs(i, [])
                if found:
                    return found
        return None

    def _replay_order(
        self, domain: Domain, problem: Problem, result: Any, order: list[int]
    ) -> bool:
        """Replay the plan actions in an independently chosen position order."""
        state = frozenset(problem.init)
        for pos in order:
            step = result.plan[pos]
            op = domain.operators.get(step.operator)
            if op is None:
                return False
            env = unify_heads(op.params, tuple(step.args), {})
            if env is None:
                return False
            try:
                if not all(
                    literal_holds(ground_literal(lit, env), state) for lit in op.pre
                ):
                    return False
            except Exception:
                return False
            deletes = {ground_fact(f, env) for f in op.delete}
            adds = {ground_fact(f, env) for f in op.add}
            state = frozenset((set(state) - deletes) | adds)
        return True
