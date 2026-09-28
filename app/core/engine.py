"""ATMS reasoning kernel.

Maintains, per node, a *label*: the set of assumption environments under
which the node is derivable. Labels are kept subset-minimal, and no label
ever contains an environment that is a superset of a known nogood.

Propagation is a bounded work-queue loop. Every budget breach flips the
session's ``incomplete`` flag and records a reason; results computed so far
stay sound (every reported environment is genuine support), but the caller
is told the picture may be partial -- contract: over-limit means
"incomplete", never "the missing environments do not exist".

Retraction of an assumption is implemented as a full rebuild from the
recorded premises, active assumptions and rules. That is deliberate: it
guarantees a conclusion is never dropped while another environment still
supports it, at the cost of recomputation (fine for teaching workloads).
"""

from __future__ import annotations

from collections import defaultdict, deque
from itertools import product

from ..config import Budget
from .types import (
    CONTRADICTION,
    Environment,
    OpResult,
    Rule,
    is_nogood_superset,
    minimize,
)

# Query statuses (contract item 4: unknown != unsupported).
STATUS_SUPPORTED = "supported"
STATUS_SUPPORTED_PARTIAL = "supported-partial"
STATUS_UNSUPPORTED = "unsupported"
STATUS_UNKNOWN = "unknown"


class ATMS:
    def __init__(self, budget: Budget):
        self.budget = budget
        self.assumptions: set[str] = set()  # currently active
        self.premises: set[str] = set()  # nodes asserted unconditionally
        self.rules: dict[str, Rule] = {}
        self.labels: dict[str, set[Environment]] = defaultdict(set)
        self.nogoods: set[Environment] = set()
        self.incomplete: bool = False
        self.incomplete_reasons: list[str] = []
        self._queue: deque[tuple[str, Environment]] = deque()
        self._rules_by_antecedent: dict[str, list[Rule]] = defaultdict(list)

    # ------------------------------------------------------------------ #
    # mutations
    # ------------------------------------------------------------------ #

    def add_assumption(self, name: str) -> OpResult:
        if name == CONTRADICTION:
            return OpResult(False, "reserved-name", {"name": name})
        if name in self.assumptions:
            return OpResult(False, "assumption-already-active", {"name": name})
        self.assumptions.add(name)
        self._enqueue(name, frozenset({name}))
        self._propagate()
        return OpResult(True, "assumption-activated", {"name": name})

    def retract_assumption(self, name: str) -> OpResult:
        if name not in self.assumptions:
            return OpResult(False, "assumption-not-active", {"name": name})
        self.assumptions.discard(name)
        self._rebuild()
        return OpResult(True, "assumption-retracted-rebuilt", {"name": name})

    def add_premise(self, node: str) -> OpResult:
        if node == CONTRADICTION:
            return OpResult(False, "reserved-name", {"node": node})
        if node in self.premises:
            return OpResult(False, "premise-already-asserted", {"node": node})
        self.premises.add(node)
        self._enqueue(node, frozenset())
        self._propagate()
        return OpResult(True, "premise-asserted", {"node": node})

    def add_rule(self, rule: Rule) -> OpResult:
        if rule.rule_id in self.rules:
            return OpResult(False, "rule-id-exists", {"rule_id": rule.rule_id})
        self.rules[rule.rule_id] = rule
        for antecedent in rule.antecedents:
            self._rules_by_antecedent[antecedent].append(rule)
        # Fire the new rule against whatever is already derived.
        self._fire_rule(rule)
        self._propagate()
        return OpResult(True, "rule-added", {"rule_id": rule.rule_id})

    # ------------------------------------------------------------------ #
    # queries
    # ------------------------------------------------------------------ #

    def label(self, node: str) -> set[Environment]:
        return set(self.labels.get(node, set()))

    def query(self, node: str) -> dict:
        """Label plus a completeness-aware status.

        An empty label only means "unsupported" when propagation completed
        within budget; otherwise the honest answer is "unknown".
        """
        envs = self.label(node)
        complete = not self.incomplete
        if envs:
            status = STATUS_SUPPORTED if complete else STATUS_SUPPORTED_PARTIAL
        else:
            status = STATUS_UNSUPPORTED if complete else STATUS_UNKNOWN
        return {
            "node": node,
            "environments": sorted(
                (sorted(env) for env in envs), key=lambda e: (len(e), e)
            ),
            "complete": complete,
            "status": status,
            "incomplete_reasons": list(self.incomplete_reasons),
        }

    def known_nogoods(self) -> list[list[str]]:
        return sorted(
            (sorted(ng) for ng in self.nogoods), key=lambda e: (len(e), e)
        )

    # ------------------------------------------------------------------ #
    # propagation internals
    # ------------------------------------------------------------------ #

    def _enqueue(self, node: str, env: Environment) -> None:
        self._queue.append((node, env))

    def _mark_incomplete(self, reason: str) -> None:
        self.incomplete = True
        if reason not in self.incomplete_reasons:
            self.incomplete_reasons.append(reason)

    def _propagate(self) -> None:
        steps = 0
        while self._queue:
            steps += 1
            if steps > self.budget.max_propagation_steps:
                self._queue.clear()
                self._mark_incomplete(
                    f"propagation-step-budget-exceeded:{self.budget.max_propagation_steps}"
                )
                return
            node, env = self._queue.popleft()
            self._add_env(node, env)

    def _add_env(self, node: str, env: Environment) -> None:
        if is_nogood_superset(env, self.nogoods):
            return  # contradictory support is never a valid support
        label = self.labels[node]
        if any(existing <= env for existing in label):
            return  # already covered by a strictly smaller/equal environment
        if len(label) >= self.budget.max_envs_per_label:
            self._mark_incomplete(
                f"label-budget-exceeded:{node}:{self.budget.max_envs_per_label}"
            )
            return
        # Keep the label subset-minimal: drop environments this one subsumes.
        label.difference_update({existing for existing in label if env < existing})
        label.add(env)
        if node == CONTRADICTION:
            self._record_nogood(env)
        for rule in self._rules_by_antecedent.get(node, ()):
            self._fire_rule(rule)

    def _fire_rule(self, rule: Rule) -> None:
        if not rule.antecedents:
            self._enqueue(rule.consequent, frozenset())
            return
        labels = [self.labels.get(a, set()) for a in rule.antecedents]
        if any(not label for label in labels):
            return
        combos = 1
        for label in labels:
            combos *= len(label)
        if combos > self.budget.max_combinations_per_rule:
            self._mark_incomplete(
                f"combination-budget-exceeded:{rule.rule_id}:{combos}"
            )
            return
        for combo in product(*labels):
            env: Environment = frozenset().union(*combo)
            self._enqueue(rule.consequent, env)

    def _record_nogood(self, env: Environment) -> None:
        if any(ng <= env for ng in self.nogoods):
            return
        self.nogoods.difference_update({ng for ng in self.nogoods if env < ng})
        self.nogoods.add(env)
        # Purge now-invalid supports from every label. Derived environments
        # only grow by union, so anything built on a nogood superset is itself
        # a nogood superset and is caught by the same filter.
        for node, label in self.labels.items():
            label.difference_update(
                {e for e in label if is_nogood_superset(e, self.nogoods)}
            )

    def _rebuild(self) -> None:
        """Recompute all labels/nogoods from recorded facts (retraction)."""
        self.labels = defaultdict(set)
        self.nogoods = set()
        self.incomplete = False
        self.incomplete_reasons = []
        self._queue = deque()
        for node in sorted(self.premises):
            self._enqueue(node, frozenset())
        for name in sorted(self.assumptions):
            self._enqueue(name, frozenset({name}))
        self._propagate()
        for rule in list(self.rules.values()):
            self._fire_rule(rule)
        self._propagate()
