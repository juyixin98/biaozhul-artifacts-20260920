"""The ATMS kernel.

An Assumption-based TMS (de Kleer 1986) maintains, for every node, a
**label**: a set of environments (sets of assumptions) under which the
node holds.  Labels are kept:

* **consistent** -- no environment is a nogood or a superset of one;
* **minimal** -- no environment is a superset of another (antichain);
* **sound** -- every environment is derivable via justifications;
* **complete** -- every minimal consistent environment that derives the
  node is present -- *unless propagation was stopped by a budget*, in
  which case ``incomplete`` is reported and absence must not be read as
  "not derivable".

The engine is deliberately synchronous and in-memory; persistence of
facts, rules and labels is the storage layer's responsibility.
"""

from __future__ import annotations

from collections import defaultdict, deque
from itertools import product
from typing import Dict, FrozenSet, List, Optional, Sequence, Set, Tuple

from .budgets import BudgetExceeded, BudgetTracker, Budgets
from .types import Environment, FALSE_NODE, Justification


def _is_minimal_new(antichain: Sequence[Environment], env: Environment) -> bool:
    """Non-mutating check: would ``env`` be accepted into the antichain?"""
    if env in antichain:
        return False
    return not any(existing <= env for existing in antichain)


def _add_minimal(antichain: List[Environment], env: Environment) -> None:
    """Insert ``env`` and drop supersets (caller checked ``_is_minimal_new``)."""
    antichain[:] = [e for e in antichain if not (env <= e)]
    antichain.append(env)


def _subsumes_any(env: Environment, blockers: Sequence[Environment]) -> bool:
    return any(b <= env for b in blockers)


class PropagateResult:
    """Outcome of one propagation pass."""

    def __init__(self) -> None:
        self.incomplete: bool = False
        self.reason: Optional[str] = None
        self.new_nogoods: int = 0
        self.steps: int = 0
        self.total_envs: int = 0

    def as_dict(self) -> dict:
        return {
            "incomplete": self.incomplete,
            "reason": self.reason,
            "new_nogoods": self.new_nogoods,
            "steps": self.steps,
            "total_envs": self.total_envs,
        }


class ATMS:
    """Maintains labels, nogoods and the justification network."""

    def __init__(self, budgets: Optional[Budgets] = None) -> None:
        self.budgets = budgets or Budgets()
        self.labels: Dict[str, List[Environment]] = defaultdict(list)
        self.nogoods: List[Environment] = []  # minimal inconsistent environments
        self.justifications: Dict[str, Justification] = {}
        # node -> rules that have that node as an antecedent (propagation)
        self._consumers: Dict[str, List[str]] = defaultdict(list)
        # node -> rules whose consequent is that node (explanation lookup)
        self._producers: Dict[str, List[str]] = defaultdict(list)
        # assumption nodes carry their own singleton environment
        self._assumptions: Set[str] = set()
        self.incomplete: bool = False
        self.incomplete_reason: Optional[str] = None
        self.fixpoint_reached: bool = False

    # ------------------------------------------------------------------ setup

    def declare_assumption(self, node_id: str) -> None:
        if node_id == FALSE_NODE:
            raise ValueError("FALSE is reserved and cannot be an assumption")
        self._assumptions.add(node_id)

    def add_justification(self, just: Justification) -> None:
        if just.rule_id in self.justifications:
            raise ValueError(f"duplicate justification id: {just.rule_id}")
        if just.consequent == FALSE_NODE and not just.antecedents:
            raise ValueError("a premise cannot directly assert FALSE")
        self.justifications[just.rule_id] = just
        # A rule is re-evaluated whenever one of its *antecedent* nodes
        # gains a new environment.
        for ant in just.antecedents:
            self._consumers[ant].append(just.rule_id)
        self._producers[just.consequent].append(just.rule_id)

    def is_assumption(self, node_id: str) -> bool:
        return node_id in self._assumptions

    def hydrate(self, labels: Dict[str, List[Environment]],
                nogoods: List[Environment],
                incomplete: bool = False,
                incomplete_reason: Optional[str] = None) -> None:
        """Restore labels/nogoods from a persisted snapshot.

        Used to resume a budget-interrupted run or to answer queries from
        storage without re-deriving everything.  Incoming labels are
        re-filtered through the nogood antichain so the restored state is
        always internally consistent.  A complete snapshot counts as a
        reached fixpoint (re-propagation would reject every insert); an
        incomplete one leaves the engine ready to resume.
        """
        self.nogoods[:] = []
        for env in sorted(nogoods, key=lambda e: (len(e), sorted(e))):
            if not _subsumes_any(env, self.nogoods):
                self.nogoods[:] = [ng for ng in self.nogoods if not (env <= ng)]
                self.nogoods.append(env)
        self.labels = defaultdict(list)
        for node, envs in labels.items():
            cleaned = [
                e for e in envs if not _subsumes_any(e, self.nogoods)
            ]
            # Recompute the antichain in case the stored order differs.
            minimal: List[Environment] = []
            for env in sorted(cleaned, key=lambda e: (len(e), sorted(e))):
                if _is_minimal_new(minimal, env):
                    _add_minimal(minimal, env)
            self.labels[node] = minimal
        self.incomplete = incomplete
        self.incomplete_reason = incomplete_reason
        self.fixpoint_reached = not incomplete

    # ------------------------------------------------------------- nogood ops

    def _record_nogood(self, env: Environment, result: PropagateResult) -> bool:
        """Add a nogood if it is not already blocked; return whether it is new."""
        if not env:
            raise ValueError("empty environment is inconsistent: premises derive FALSE")
        if _subsumes_any(env, self.nogoods):
            return False
        self.nogoods[:] = [ng for ng in self.nogoods if not (env <= ng)]
        self.nogoods.append(env)
        result.new_nogoods += 1

        # Consistency maintenance: a newly recorded nogood (or the smaller
        # nogoods that replaced redundant ones) invalidates environments
        # already sitting in other node labels.  Removal cannot create new
        # support, so nothing needs to be re-propagated.
        for label in self.labels.values():
            label[:] = [e for e in label if not _subsumes_any(e, self.nogoods)]
        return True

    def is_consistent_env(self, env: Environment) -> bool:
        """True iff no known nogood is a subset of ``env``."""
        return not _subsumes_any(env, self.nogoods)

    # --------------------------------------------------------------- insertion

    def _insert(self, node: str, env: Environment, queue: deque,
                tracker: BudgetTracker, result: PropagateResult) -> None:
        """Try to add env to node's label; enqueue consumers when accepted."""
        if env and _subsumes_any(env, self.nogoods):
            return  # inconsistent environment: never valid support
        label = self.labels[node]
        if not _is_minimal_new(label, env):
            return  # already present or subsumed by a smaller environment
        # Account *before* committing: if the budget is exhausted neither
        # this environment nor any supersets it would remove may leave the
        # label in an unbudgeted partial state.
        tracker.reserve_label_slot(node, len(label))
        _add_minimal(label, env)
        if node == FALSE_NODE:
            # The inserted env was checked against prior nogoods, so it is
            # a genuinely new minimal contradiction.
            self._record_nogood(env, result)
        for rule_id in self._consumers.get(node, []):
            queue.append(("fire", rule_id))

    # ----------------------------------------------------------- justification

    def _fire(self, rule_id: str, tracker: BudgetTracker,
              queue: deque, result: PropagateResult) -> None:
        just = self.justifications[rule_id]
        ant_labels: List[List[Environment]] = []
        for ant in just.antecedents:
            label = [e for e in self.labels.get(ant, [])
                     if not _subsumes_any(e, self.nogoods)]
            if not label:
                return  # antecedent unsupported (yet): nothing to propagate
            ant_labels.append(label)

        seen: Set[Environment] = set()
        if not ant_labels:
            # Premise justification: supported by the empty environment.
            combo: Environment = frozenset()
            self._insert(just.consequent, combo, queue, tracker, result)
            return

        for pieces in product(*ant_labels):
            env = frozenset().union(*pieces)
            if env in seen:
                continue
            seen.add(env)
            if _subsumes_any(env, self.nogoods):
                continue
            self._insert(just.consequent, env, queue, tracker, result)

    # --------------------------------------------------------------- main loop

    def _seed_queue(self) -> deque:
        """Queue work implied by the current declarations.

        Every rule is seeded once, followed by assumption singletons.
        Seeding all rules is what makes *resumption* sound: when an
        incomplete run is restarted from a snapshot, assumption singletons
        are already present (so their insertions would not re-trigger
        consumers), but a fresh rule firing recomputes every environment
        combination from the restored labels and picks up the missing work.
        Firing a rule whose antecedents are unsupported or whose output
        envs already exist is a cheap no-op.
        """
        queue: deque = deque()
        for rule_id in sorted(self.justifications):
            queue.append(("fire", rule_id))
        for a in sorted(self._assumptions):
            queue.append(("assume", a))
        return queue

    def propagate(self) -> PropagateResult:
        """Run the label-propagation loop to a fixpoint or until budgeted out.

        On budget overflow the engine retains every environment derived so
        far, marks itself incomplete and stops.  A later call (also after
        ``hydrate`` from a snapshot) re-seeds every rule and assumption;
        existing inserts are rejected by the minimality check while the
        rule re-firings pick up the environments that were never reached.
        """
        result = PropagateResult()
        tracker = BudgetTracker(self.budgets)
        queue = self._seed_queue()
        try:
            while queue:
                kind, payload = queue.popleft()
                tracker.count_step(f"{kind}:{payload}")
                if kind == "assume":
                    self._insert(
                        payload, frozenset({payload}), queue, tracker, result
                    )
                else:
                    self._fire(payload, tracker, queue, result)
        except BudgetExceeded as exc:
            self.incomplete = True
            self.incomplete_reason = str(exc)
            result.incomplete = True
            result.reason = str(exc)
        if not result.incomplete:
            self.incomplete = False
            self.incomplete_reason = None
            self.fixpoint_reached = True
        result.steps = tracker.steps
        result.total_envs = tracker.total_envs
        return result

    # ----------------------------------------------------------------- queries

    def holding_environments(self, node_id: str) -> List[Environment]:
        """Consistent, subset-minimal support environments for a node."""
        return [
            e for e in self.labels.get(node_id, [])
            if not _subsumes_any(e, self.nogoods)
        ]

    def holds(self, node_id: str, under: Optional[FrozenSet[str]] = None) -> bool:
        """Does the node hold (optionally under a fixed set of assumptions)?

        When ``under`` is given, an environment supports the node only if it
        is a subset of ``under`` *and* ``under`` itself is consistent.  In
        incomplete state this answer is conservative: False means "no
        environment was derived", not "provably unsupported".
        """
        if under is not None and _subsumes_any(under, self.nogoods):
            return False
        for env in self.holding_environments(node_id):
            if under is None or env <= under:
                return True
        return False

    def explain(self, node_id: str) -> List[Tuple[Environment, List[str]]]:
        """Pair each support environment with the rules capable of deriving it.

        The explanation is reconstructed from current labels: a rule can
        support env for node when each antecedent has a supporting
        environment contained in env whose union equals env.
        """
        out: List[Tuple[Environment, List[str]]] = []
        for env in self.holding_environments(node_id):
            # Assumption nodes are supported by their own declaration.
            if node_id in self._assumptions and env == frozenset({node_id}):
                out.append((env, [f"<assumption:{node_id}>"]))
                continue
            via: List[str] = []
            for rule_id in self._producers.get(node_id, []):
                just = self.justifications[rule_id]
                if not just.antecedents and not env:
                    via.append(rule_id)
                    continue
                choices = []
                ok = True
                for ant in just.antecedents:
                    candidates = [
                        e for e in self.holding_environments(ant)
                        if e <= env
                    ]
                    if not candidates:
                        ok = False
                        break
                    choices.append(candidates)
                if not ok:
                    continue
                for pieces in product(*choices):
                    if frozenset().union(*pieces) == env:
                        via.append(rule_id)
                        break
            out.append((env, via))
        return out
