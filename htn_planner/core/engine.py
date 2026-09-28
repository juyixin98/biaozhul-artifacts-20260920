"""Bounded HTN decomposition search.

Semantics (contract summarized here; see docs/SEMANTICS.md)
-----------------------------------------------------------
* Compound tasks are reduced by *methods*.  A method applies only when its
  preconditions hold in the **current** state.  Candidate methods are tried
  in declaration order with full backtracking.  When none applies, the
  branch is a proved dead end and a ``no_applicable_method`` record is
  emitted carrying every rejected method plus the literal that rejected it.
* Primitive tasks (``!operator``) execute only when they are *minimal* in
  the remaining precedence graph (no surviving predecessor) **and** every
  precondition literal holds in the current state.  Effects apply deletes
  first, then adds.
* Ordered bodies become a precedence chain; partial bodies use the declared
  ``(:before i j)`` edges.  Decomposition rewires a replaced task's external
  edges through its first/last children, so order survives nesting.
* A task DAG is not automatically executable: if every remaining minimal
  task is primitive but none has its preconditions met, the branch is a
  ``deadlock``.  Shared-resource conflicts and partial-order interference
  show up exactly here; we never mistake a topological ordering for a plan.
* Recursion is bounded by nesting depth (local cut) and by global
  expansion / search-node budgets, plus a per-branch action horizon.
  Definitive dead ends prove failure; any budget cut makes a no-solution
  verdict *inconclusive* and is reported in a separate section.
"""

from __future__ import annotations

import dataclasses
import time
from typing import Any

from .. import __version__
from ..lang import (
    Atom,
    Bindings,
    Domain,
    Problem,
    first_failed_literal,
    first_unmet,
    ground_fact,
    satisfy_preconditions,
    unify_heads,
)
from .evidence import (
    ActionStep,
    ExpansionRecord,
    FailureCategory,
    FailureRecord,
    KeyStep,
    Outcome,
    PlanResult,
)

ENGINE_VERSION = __version__

ROOT_ID = "root"


class PlanningError(ValueError):
    """Malformed planning request (unknown task/operator, wrong domain)."""


@dataclasses.dataclass(frozen=True)
class Bounds:
    max_depth: int = 12
    max_expansions: int = 500
    max_actions: int = 100
    max_search_nodes: int = 5000
    max_evidence: int = 60
    max_steps: int = 120


@dataclasses.dataclass(frozen=True)
class TaskInst:
    id: str
    expr: tuple[Atom, ...]
    depth: int
    method_path: tuple[str, ...]
    # Ordered (outermost-first) chain of enclosing (ground task, world state
    # at expansion).  An exact (task, state) repeat is a sound non-progress
    # cycle; the same task under a changed state is not a repeat.
    ancestors: tuple[tuple[tuple[Atom, ...], frozenset[tuple[Atom, ...]]], ...]
    parent_id: str

    @property
    def head(self) -> str:
        return str(self.expr[0])

    @property
    def is_primitive(self) -> bool:
        return self.head.startswith("!")


@dataclasses.dataclass(frozen=True)
class SearchNode:
    state: frozenset[tuple[Atom, ...]]
    tasks: dict[str, TaskInst]
    edges: frozenset[tuple[str, str]]
    actions: tuple[ActionStep, ...]
    tree: dict[str, ExpansionRecord]
    next_id: int


@dataclasses.dataclass
class _Counters:
    search_nodes: int = 0
    expansions: int = 0
    method_tests: int = 0
    actions_executed: int = 0
    dead_ends: int = 0
    backtracks: int = 0


class _LocalCut(Exception):
    """A single branch was pruned by a local horizon (depth/actions)."""

    def __init__(self, category: str, detail: str, task: tuple[Atom, ...] | None,
                 method_path: tuple[str, ...]) -> None:
        super().__init__(detail)
        self.category = category
        self.detail = detail
        self.task = task
        self.method_path = method_path


class _CyclePruned(Exception):
    """A branch re-expanded an identical ground task: definitive dead end."""

    def __init__(self, record: FailureRecord) -> None:
        super().__init__(record.detail)
        self.record = record


class _GlobalCut(Exception):
    """A global budget was consumed; further feasibility is unproved."""

    def __init__(self, category: str, detail: str) -> None:
        super().__init__(detail)
        self.category = category
        self.detail = detail


class _SearchContext:
    def __init__(self, bounds: Bounds) -> None:
        self.bounds = bounds
        self.counters = _Counters()
        self.failures: list[FailureRecord] = []
        self.uncertain: list[FailureRecord] = []
        self.steps: list[KeyStep] = []
        self.global_cut = False
        self.truncated = 0
        self._step_seq = 0
        self._failure_fingerprints: set[str] = set()
        self._uncertain_fingerprints: set[str] = set()

    def step(self, name: str, detail: dict[str, Any]) -> None:
        self._step_seq += 1
        if len(self.steps) < self.bounds.max_steps:
            self.steps.append(KeyStep(self._step_seq, name, detail))

    def failure(self, record: FailureRecord) -> None:
        # Backtracking reaches equivalent dead ends through several routes;
        # collapse records with the same category/task/evidence fingerprint.
        blocked_key = tuple(
            (b.get("task"), b.get("unmet")) for b in record.blocked
        )
        rejected_key = tuple(
            (m.get("method"), m.get("unmet")) for m in record.rejected_methods
        )
        fingerprint = repr(
            (record.category, record.task, blocked_key, rejected_key, record.action_prefix)
        )
        self.counters.dead_ends += 1
        if fingerprint in self._failure_fingerprints:
            return
        self._failure_fingerprints.add(fingerprint)
        if len(self.failures) < self.bounds.max_evidence:
            self.failures.append(record)
        else:
            self.truncated += 1

    def cut(self, record: FailureRecord) -> None:
        # Any budget prune means a no-solution verdict is off the table; keep
        # one representative record per (category, task, prefix) shape.
        self.global_cut = True
        fingerprint = repr(
            (record.category, record.task, record.detail, record.action_prefix)
        )
        if fingerprint in self._uncertain_fingerprints:
            return
        self._uncertain_fingerprints.add(fingerprint)
        if len(self.uncertain) < self.bounds.max_evidence:
            self.uncertain.append(record)
        else:
            self.truncated += 1


class Planner:
    """Deterministic, reentrant bounded HTN planner."""

    def __init__(self, bounds: Bounds | None = None) -> None:
        self.bounds = bounds or Bounds()

    # ------------------------------------------------------------------ #
    # entry point
    # ------------------------------------------------------------------ #

    def solve(
        self,
        domain: Domain,
        problem: Problem,
        request_id: str,
        domain_version: str = "unknown",
    ) -> PlanResult:
        if problem.domain_name != domain.name:
            raise PlanningError(
                f"problem {problem.name} references domain {problem.domain_name!r},"
                f" loaded domain is {domain.name!r}"
            )
        self._validate_references(domain, problem)

        ctx = _SearchContext(self.bounds)
        start = time.perf_counter()
        global_cut_detail: tuple[str, str] | None = None
        try:
            root = self._build_root(ctx, problem)
            solution = self._search(ctx, domain, root)
        except _GlobalCut as exc:
            solution = None
            global_cut_detail = (exc.category, exc.detail)

        if solution is not None:
            status = Outcome.SUCCESS
            # Budget cuts on abandoned branches are ordinary backtracking once
            # a real solution exists; they must not read as 'inconclusive'.
            ctx.uncertain = []
            ctx.step("solution", {"actions": len(solution.actions)})
        elif ctx.global_cut or global_cut_detail is not None:
            status = Outcome.INCONCLUSIVE
            if global_cut_detail is not None:
                category, detail = global_cut_detail
                ctx.uncertain.append(
                    self._budget_record(category, detail, None, (), ())
                )
        else:
            status = Outcome.FAILURE

        expansion = ()
        plan = ()
        if solution is not None:
            plan = solution.actions
            expansion = tuple(sorted(solution.tree.values(), key=lambda r: (r.depth, r.node_id)))

        return PlanResult(
            request_id=request_id,
            status=status.value,
            domain_name=domain.name,
            problem_name=problem.name,
            engine_version=ENGINE_VERSION,
            domain_version=domain_version,
            bounds=dataclasses.asdict(self.bounds),
            counters=dataclasses.asdict(ctx.counters),
            plan=plan,
            expansion=expansion,
            root_order=problem.root.order,
            root_ordered=problem.root.ordered,
            failures=tuple(ctx.failures),
            uncertain=tuple(ctx.uncertain),
            steps=tuple(ctx.steps),
            duration_ms=round((time.perf_counter() - start) * 1000, 3),
            truncated_failures=ctx.truncated,
        )

    # ------------------------------------------------------------------ #
    # validation
    # ------------------------------------------------------------------ #

    def _validate_references(self, domain: Domain, problem: Problem) -> None:
        """Reject problems/domains referencing unknown heads or bad arities."""

        def check_expr(expr: tuple[Atom, ...], where: str) -> None:
            head = str(expr[0])
            if head.startswith("!"):
                op = domain.operators.get(head)
                if op is None:
                    raise PlanningError(f"{where}: unknown operator {head}")
                if len(op.params) != len(expr) - 1:
                    raise PlanningError(
                        f"{where}: operator {head} expects {len(op.params)}"
                        f" args, got {len(expr) - 1}"
                    )
            else:
                methods = domain.methods.get(head)
                if not methods:
                    raise PlanningError(f"{where}: no method for compound task {head}")
                arities = {len(m.params) for m in methods}
                if len(expr) - 1 not in arities:
                    raise PlanningError(
                        f"{where}: task {head} called with {len(expr) - 1} args,"
                        f" methods declare {sorted(arities)}"
                    )

        for task in problem.root.tasks:
            check_expr(tuple(task), f"problem {problem.name} root task")
        for methods in domain.methods.values():
            for m in methods:
                for st in m.subtasks:
                    check_expr(tuple(st), f"method {m.name} subtask")

    # ------------------------------------------------------------------ #
    # root network
    # ------------------------------------------------------------------ #

    def _build_root(self, ctx: _SearchContext, problem: Problem) -> SearchNode:
        ids = [f"t{i + 1}" for i in range(len(problem.root.tasks))]
        tasks: dict[str, TaskInst] = {}
        tree: dict[str, ExpansionRecord] = {}
        for tid, expr in zip(ids, problem.root.tasks):
            tasks[tid] = TaskInst(
                id=tid,
                expr=tuple(expr),
                depth=0,
                method_path=("root",),
                ancestors=(),
                parent_id=ROOT_ID,
            )
        edges = self._internal_edges(ids, problem.root.ordered, problem.root.order)
        tree[ROOT_ID] = ExpansionRecord(
            node_id=ROOT_ID,
            parent_id=None,
            task=("::root-network::", problem.name),
            kind="network",
            depth=-1,
            expanded_by=None,
            operator=None,
            children=tuple(ids),
            ordering=problem.root.order,
            ordered=problem.root.ordered,
        )
        ctx.step(
            "root_network",
            {
                "tasks": [list(t) for t in problem.root.tasks],
                "ordered": problem.root.ordered,
                "constraints": [list(p) for p in problem.root.order],
            },
        )
        return SearchNode(problem.init, tasks, frozenset(edges), (), tree, len(ids) + 1)

    @staticmethod
    def _internal_edges(
        ids: list[str], ordered: bool, order: tuple[tuple[int, int], ...]
    ) -> set[tuple[str, str]]:
        if ordered:
            return {(ids[i], ids[i + 1]) for i in range(len(ids) - 1)}
        return {(ids[i], ids[j]) for i, j in order}

    # ------------------------------------------------------------------ #
    # search
    # ------------------------------------------------------------------ #

    def _search(
        self, ctx: _SearchContext, domain: Domain, node: SearchNode
    ) -> SearchNode | None:
        ctx.counters.search_nodes += 1
        if ctx.counters.search_nodes > self.bounds.max_search_nodes:
            raise _GlobalCut(
                FailureCategory.SEARCH_BUDGET,
                f"search-node budget {self.bounds.max_search_nodes} exhausted",
            )

        if not node.tasks:
            return node

        moves, blocked, stuck = self._moves(ctx, domain, node)

        if not moves:
            self._record_dead_end(ctx, node, blocked, stuck)
            ctx.counters.backtracks += 1
            return None

        for move in moves:
            kind, tid = move[0], move[1]
            try:
                if kind == "exec":
                    nxt = self._apply_exec(ctx, domain, node, tid)
                else:
                    nxt = self._apply_expand(ctx, domain, node, tid, move[2])
            except _CyclePruned as exc:
                ctx.failure(exc.record)
                continue
            except _LocalCut as exc:
                ctx.cut(
                    self._budget_record(
                        exc.category,
                        exc.detail,
                        exc.task,
                        exc.method_path,
                        tuple((a.operator, a.args) for a in node.actions),
                    )
                )
                continue
            result = self._search(ctx, domain, nxt)
            if result is not None:
                return result
            ctx.counters.backtracks += 1
        return None

    # ------------------------------------------------------------------ #
    # move generation
    # ------------------------------------------------------------------ #

    def _minimal(self, node: SearchNode) -> list[str]:
        incoming: dict[str, set[str]] = {tid: set() for tid in node.tasks}
        for pred, succ in node.edges:
            if succ in incoming:
                incoming[succ].add(pred)
        return sorted((tid for tid, preds in incoming.items() if not preds), key=lambda t: int(t[1:]))

    def _moves(
        self,
        ctx: _SearchContext,
        domain: Domain,
        node: SearchNode,
    ) -> tuple[
        list[tuple[str, str, Any]],
        list[dict[str, Any]],
        list[tuple[str, list[dict[str, Any]]]],
    ]:
        """Return (moves, blocked-primitives, stuck-compounds).

        Move shapes: ``("exec", task_id, None)`` or
        ``("expand", task_id, (method, env))``.
        """
        moves: list[tuple[str, str, Any]] = []
        blocked: list[dict[str, Any]] = []
        stuck: list[tuple[str, list[dict[str, Any]]]] = []

        for tid in self._minimal(node):
            task = node.tasks[tid]
            if task.is_primitive:
                op = domain.operators[task.head]
                env = unify_heads(op.params, tuple(task.expr[1:]), {})
                if env is None:  # validated earlier, defensive only
                    blocked.append(
                        {"task": list(task.expr), "reason": "arity_mismatch"}
                    )
                    continue
                unmet = first_unmet(op.pre, env, node.state)
                ctx.counters.method_tests += 1
                if unmet is None:
                    moves.append(("exec", tid, None))
                else:
                    blocked.append(
                        {
                            "task": list(task.expr),
                            "operator": op.name,
                            "reason": "precondition",
                            "unmet": str(unmet),
                        }
                    )
            else:
                rejected, applicable = self._applicable_methods(ctx, domain, node, task)
                if applicable:
                    for method, env in applicable:
                        moves.append(("expand", tid, (method, env)))
                else:
                    stuck.append((tid, rejected))
        return moves, blocked, stuck

    def _applicable_methods(
        self,
        ctx: _SearchContext,
        domain: Domain,
        node: SearchNode,
        task: TaskInst,
    ) -> tuple[list[dict[str, Any]], list[tuple[Any, Bindings]]]:
        rejected: list[dict[str, Any]] = []
        applicable: list[tuple[Any, Bindings]] = []
        for method in domain.methods.get(task.head, ()):
            ctx.counters.method_tests += 1
            env = unify_heads(method.params, tuple(task.expr[1:]), {})
            if env is None:
                rejected.append(
                    {
                        "method": method.name,
                        "reason": "arity_mismatch",
                        "detail": (
                            f"method takes {len(method.params)} params,"
                            f" task supplies {len(task.expr) - 1}"
                        ),
                    }
                )
                continue
            # A conjunctive precondition may have several groundings when it
            # introduces existential variables; each grounding is one branch.
            groundings = satisfy_preconditions(method.pre, env, node.state)
            if groundings:
                seen: set[tuple[tuple[str, Atom], ...]] = set()
                for grounding in groundings:
                    key = tuple(sorted(grounding.items()))
                    if key in seen:
                        continue
                    seen.add(key)
                    applicable.append((method, grounding))
            else:
                failed = first_failed_literal(method.pre, env, node.state)
                rejected.append(
                    {
                        "method": method.name,
                        "reason": "precondition",
                        "unmet": failed["literal"] if failed else "precondition",
                        "failure_kind": failed["reason"] if failed else "unknown",
                    }
                )
        return rejected, applicable

    # ------------------------------------------------------------------ #
    # transitions
    # ------------------------------------------------------------------ #

    def _apply_exec(
        self, ctx: _SearchContext, domain: Domain, node: SearchNode, tid: str
    ) -> SearchNode:
        task = node.tasks[tid]
        if len(node.actions) >= self.bounds.max_actions:
            raise _LocalCut(
                FailureCategory.ACTION_BUDGET,
                f"action horizon {self.bounds.max_actions} reached on {tuple(task.expr)}",
                task.expr,
                task.method_path,
            )
        op = domain.operators[task.head]
        env = unify_heads(op.params, tuple(task.expr[1:]), {})
        if env is None:  # pragma: no cover - validated
            raise PlanningError(f"arity mismatch executing {task.expr}")
        deletes = tuple(ground_fact(f, env) for f in op.delete)
        adds = tuple(ground_fact(f, env) for f in op.add)
        new_state = frozenset((set(node.state) - set(deletes)) | set(adds))
        step = ActionStep(
            seq=len(node.actions) + 1,
            node_id=tid,
            operator=op.name,
            args=tuple(task.expr[1:]),
            method_path=task.method_path,
            added=adds,
            deleted=deletes,
        )
        edges = {e for e in node.edges if tid not in e}
        tasks = {k: v for k, v in node.tasks.items() if k != tid}
        tree = dict(node.tree)
        tree[tid] = ExpansionRecord(
            node_id=tid,
            parent_id=task.parent_id,
            task=task.expr,
            kind="primitive",
            depth=task.depth,
            expanded_by=None,
            operator=op.name,
            children=(),
            ordering=(),
            ordered=True,
        )
        ctx.counters.actions_executed += 1
        ctx.step(
            "action_executed",
            {
                "seq": step.seq,
                "operator": op.name,
                "args": list(step.args),
                "added": [list(f) for f in adds],
                "deleted": [list(f) for f in deletes],
            },
        )
        return SearchNode(
            new_state, tasks, frozenset(edges), node.actions + (step,), tree, node.next_id
        )

    def _apply_expand(
        self,
        ctx: _SearchContext,
        domain: Domain,
        node: SearchNode,
        tid: str,
        choice: tuple[Any, Bindings],
    ) -> SearchNode:
        task = node.tasks[tid]
        method, env = choice

        repeat_index = next(
            (
                i
                for i, (ancestor_task, ancestor_state) in enumerate(task.ancestors)
                if ancestor_task == task.expr and ancestor_state == node.state
            ),
            None,
        )
        if repeat_index is not None:
            # Sound non-progress cycle: the identical ground compound task was
            # already expanded under the identical world state.  Method
            # applicability is a deterministic function of the state, so this
            # expansion would reproduce the same subtree forever.  The same
            # task under a *changed* state is not a cycle and is left to the
            # depth budget (reported as inconclusive rather than failure).
            cycle_chain = tuple(
                list(ancestor_task)
                for ancestor_task, _ in task.ancestors[: repeat_index + 1]
            ) + (list(task.expr),)
            record = FailureRecord(
                category=FailureCategory.CYCLE,
                task=list(task.expr),
                detail=(
                    f"method {method.name} re-expands {list(task.expr)} under an"
                    f" identical world state (non-progress cycle)"
                ),
                depth=task.depth,
                method_chain=task.method_path,
                action_prefix=tuple((a.operator, a.args) for a in node.actions),
                cycle_chain=cycle_chain,
            )
            ctx.step("dead_end", {"category": FailureCategory.CYCLE, "task": list(task.expr)})
            raise _CyclePruned(record)

        if task.depth + 1 > self.bounds.max_depth:
            raise _LocalCut(
                FailureCategory.DEPTH_BUDGET,
                f"depth bound {self.bounds.max_depth} reached expanding {list(task.expr)}"
                f" via {method.name}",
                task.expr,
                task.method_path,
            )
        ctx.counters.expansions += 1
        if ctx.counters.expansions > self.bounds.max_expansions:
            raise _GlobalCut(
                FailureCategory.EXPANSION_BUDGET,
                f"expansion budget {self.bounds.max_expansions} exhausted",
            )

        child_exprs = tuple(self._ground_expr(expr, env) for expr in method.subtasks)
        child_ids = [f"t{node.next_id + i}" for i in range(len(child_exprs))]
        next_id = node.next_id + len(child_ids)
        child_path = task.method_path + (f"{task.head}/{method.name}",)
        child_ancestors = task.ancestors + ((task.expr, node.state),)

        tasks = {k: v for k, v in node.tasks.items() if k != tid}
        for cid, cexpr in zip(child_ids, child_exprs):
            tasks[cid] = TaskInst(
                id=cid,
                expr=cexpr,
                depth=task.depth + 1,
                method_path=child_path,
                ancestors=child_ancestors,
                parent_id=tid,
            )

        edges = {e for e in node.edges if tid not in e}
        predecessors = {p for p, s in node.edges if s == tid}
        successors = {s for p, s in node.edges if p == tid}
        if not child_ids:
            # Empty (no-op) method: the task vanishes, so its external
            # predecessors now directly precede its successors; dropping these
            # edges would wrongly release the successors early.
            for p in predecessors:
                for s in successors:
                    edges.add((p, s))
        else:
            internal = self._internal_edges(child_ids, method.ordered, method.order)
            edges.update(internal)
            minimal_children = self._minimal_in(child_ids, internal)
            maximal_children = self._maximal_in(child_ids, internal)
            for p in predecessors:
                for cid in minimal_children:
                    edges.add((p, cid))
            for cid in maximal_children:
                for s in successors:
                    edges.add((cid, s))

        tree = dict(node.tree)
        tree[tid] = ExpansionRecord(
            node_id=tid,
            parent_id=task.parent_id,
            task=task.expr,
            kind="compound",
            depth=task.depth,
            expanded_by=method.name,
            operator=None,
            children=tuple(child_ids),
            ordering=method.order,
            ordered=method.ordered,
        )

        ctx.step(
            "method_applied",
            {
                "task": list(task.expr),
                "method": method.name,
                "depth": task.depth + 1,
                "children": [list(c) for c in child_exprs],
                "ordered": method.ordered,
            },
        )
        return SearchNode(
            node.state, tasks, frozenset(edges), node.actions, tree, next_id
        )

    @staticmethod
    def _ground_expr(expr: tuple, env: Bindings) -> tuple[Atom, ...]:
        return tuple(ground_fact(expr, env))

    @staticmethod
    def _minimal_in(ids: list[str], edges: set[tuple[str, str]]) -> list[str]:
        incoming: dict[str, set[str]] = {tid: set() for tid in ids}
        for p, s in edges:
            if s in incoming and p in incoming:
                incoming[s].add(p)
        return [tid for tid in ids if not incoming[tid]]

    @staticmethod
    def _maximal_in(ids: list[str], edges: set[tuple[str, str]]) -> list[str]:
        outgoing: dict[str, set[str]] = {tid: set() for tid in ids}
        for p, s in edges:
            if s in outgoing and p in outgoing:
                outgoing[p].add(s)
        return [tid for tid in ids if not outgoing[tid]]

    # ------------------------------------------------------------------ #
    # dead ends / evidence
    # ------------------------------------------------------------------ #

    def _record_dead_end(
        self,
        ctx: _SearchContext,
        node: SearchNode,
        blocked: list[dict[str, Any]],
        stuck: list[tuple[str, list[dict[str, Any]]]],
    ) -> None:
        prefix = tuple((a.operator, a.args) for a in node.actions)
        minimal_ids = set(self._minimal(node))
        waiting = [
            list(node.tasks[tid].expr)
            for tid in node.tasks
            if tid not in minimal_ids
        ]
        if stuck:
            for tid, rejected in stuck:
                task = node.tasks[tid]
                rec = FailureRecord(
                    category=FailureCategory.NO_APPLICABLE_METHOD,
                    task=list(task.expr),
                    detail=(
                        f"no applicable method for {task.head} in the current state"
                    ),
                    depth=task.depth,
                    method_chain=task.method_path,
                    action_prefix=prefix,
                    rejected_methods=tuple(rejected),
                )
                ctx.failure(rec)
                ctx.step(
                    "dead_end",
                    {
                        "category": FailureCategory.NO_APPLICABLE_METHOD,
                        "task": list(task.expr),
                        "rejected": [r["method"] for r in rejected],
                    },
                )
        elif blocked:
            chains = {node.tasks[b_tid].method_path for b_tid in minimal_ids}
            remaining_tasks = tuple(
                {
                    "node": tid,
                    "task": list(node.tasks[tid].expr),
                    "depth": node.tasks[tid].depth,
                    "minimal": tid in minimal_ids,
                }
                for tid in sorted(node.tasks, key=lambda t: int(t[1:]))
            )
            remaining_edges = tuple(
                [p, s]
                for p, s in sorted(node.edges, key=lambda e: (int(e[0][1:]), int(e[1][1:])))
                if p in node.tasks and s in node.tasks
            )
            rec = FailureRecord(
                category=FailureCategory.DEADLOCK,
                task=None,
                detail=(
                    "every minimal remaining task is primitive but none of its"
                    " preconditions holds (a topological order exists but is"
                    " not executable)"
                ),
                depth=max((node.tasks[t].depth for t in minimal_ids), default=0),
                method_chain=next(iter(chains), ()) if chains else (),
                action_prefix=prefix,
                blocked=tuple(blocked),
                remaining_tasks=remaining_tasks,
                remaining_edges=remaining_edges,
            )
            ctx.failure(rec)
            ctx.step(
                "dead_end",
                {
                    "category": FailureCategory.DEADLOCK,
                    "blocked": [b["task"] for b in blocked],
                    "waiting": waiting,
                },
            )

    @staticmethod
    def _budget_record(
        category: str,
        detail: str,
        task: tuple[Atom, ...] | None,
        method_chain: tuple[str, ...],
        action_prefix: tuple[tuple[str, tuple[Atom, ...]], ...],
    ) -> FailureRecord:
        return FailureRecord(
            category=category,
            task=list(task) if task is not None else None,
            detail=detail,
            depth=-1,
            method_chain=method_chain if method_chain else (),
            action_prefix=action_prefix if action_prefix else (),
        )
