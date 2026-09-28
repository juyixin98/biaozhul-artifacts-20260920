"""The Rete network: alpha memories, beta memories, join nodes, propagation.

Indexes (the heart of the review - both networks are indexed, none of the
behaviour is a linear brute-force scan of the rule set):

**Alpha memory** (one per constant-test pattern, shared between identical
patterns across rules):
    ``wmes[id]``                  - membership
    ``field_index[field][value]`` - WME ids by field value, used to resolve
                                    equality join probes from the left

**Beta memory** (one per (rule, condition depth)):
    ``tokens[key]``                 - partial match by physical WME tuple
    ``var_index[variable][value]``  - tokens by join-variable value, used to
                                      resolve equality join probes from right
    ``wme_index[wme_id]``           - tokens containing a WME, used to retract
                                      right activations
    ``children[parent_key]``        - tokens extending a parent token, used to
                                      retract left activations recursively

Retracting a WME therefore deletes *every* partial and complete match that
depended on it, and invalidates the corresponding agenda entries.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Callable, Iterable

from ..lang.compiler import AlphaTestPlan, CompiledRule
from ..lang.model import Operator
from ..lang.predicates import PredicateError, evaluate
from .agenda import Agenda
from .errors import RuleEvaluationError, UnknownFactError
from .tokens import DUMMY_KEY, Token, WME

MISSING = object()
LogSink = Callable[..., None] | None


def _freeze_binding(value: Any) -> Any:
    if isinstance(value, dict):
        return tuple(sorted((k, _freeze_binding(v)) for k, v in value.items()))
    if isinstance(value, (list, tuple)):
        return tuple(_freeze_binding(v) for v in value)
    return value


def _noop_log(*_args: Any, **_kwargs: Any) -> None:
    return None


# ---------------------------------------------------------------------------
# Memories and nodes
# ---------------------------------------------------------------------------

@dataclass
class AlphaMemory:
    key: tuple
    fact_type: str
    # Ordered alpha-stage tests (literal / binding / same-CE variable), kept
    # explicitly rather than recovered from the sharing key.
    alpha_tests: tuple[dict, ...]
    wmes: dict[int, WME] = field(default_factory=dict)
    field_index: dict[str, dict[Any, set[int]]] = field(default_factory=dict)
    consumers: list[JoinNode] = field(default_factory=list)

    def register_index_field(self, field_name: str) -> None:
        self.field_index.setdefault(field_name, {})
        for wme in self.wmes.values():
            value = wme.fields.get(field_name, MISSING)
            if value is not MISSING:
                self.field_index[field_name].setdefault(value, set()).add(wme.id)

    def add(self, wme: WME) -> None:
        self.wmes[wme.id] = wme
        for field_name, table in self.field_index.items():
            value = wme.fields.get(field_name, MISSING)
            if value is not MISSING:
                table.setdefault(value, set()).add(wme.id)

    def remove(self, wme_id: int) -> WME | None:
        wme = self.wmes.pop(wme_id, None)
        if wme is None:
            return None
        for table in self.field_index.values():
            for value_set in table.values():
                value_set.discard(wme_id)
        return wme

    def index_stats(self) -> dict[str, int]:
        return {field_name: len(table) for field_name, table in self.field_index.items()}


@dataclass
class BetaMemory:
    rule_name: str
    ce_index: int  # CE whose joins write here (0-based); depth == ce_index + 1
    depth: int
    terminal: bool
    tokens: dict[tuple[int, ...], Token] = field(default_factory=dict)
    var_index: dict[str, dict[Any, set[tuple[int, ...]]]] = field(default_factory=dict)
    wme_index: dict[int, set[tuple[int, ...]]] = field(default_factory=dict)
    children: dict[tuple[int, ...], set[tuple[int, ...]]] = field(default_factory=dict)
    consumers: list[JoinNode] = field(default_factory=list)

    def register_variables(self, variables: Iterable[str]) -> None:
        for variable in variables:
            self.var_index.setdefault(variable, {})

    def store(self, token: Token) -> bool:
        if token.key in self.tokens:
            return False
        self.tokens[token.key] = token
        for variable, table in self.var_index.items():
            if variable in token.bindings:
                table.setdefault(token.bindings[variable], set()).add(token.key)
        for wme_id in token.wme_ids:
            self.wme_index.setdefault(wme_id, set()).add(token.key)
        if token.parent_key is not None:
            self.children.setdefault(token.parent_key, set()).add(token.key)
        return True

    def erase(self, key: tuple[int, ...]) -> Token | None:
        token = self.tokens.pop(key, None)
        if token is None:
            return None
        for variable, table in self.var_index.items():
            if variable in token.bindings:
                bucket = table.get(token.bindings[variable])
                if bucket is not None:
                    bucket.discard(key)
        for wme_id in token.wme_ids:
            bucket = self.wme_index.get(wme_id)
            if bucket is not None:
                bucket.discard(key)
        if token.parent_key is not None:
            siblings = self.children.get(token.parent_key)
            if siblings is not None:
                siblings.discard(key)
        return token

    def index_stats(self) -> dict[str, int]:
        return {
            "tokens": len(self.tokens),
            "indexed_variables": len(self.var_index),
            "indexed_wmes": len(self.wme_index),
        }


class JoinNode:
    """Joins the left beta memory (or the dummy root) with one alpha memory."""

    def __init__(
        self,
        rule_name: str,
        ce_index: int,
        alpha: AlphaMemory,
        left: BetaMemory | None,
        output: BetaMemory,
        join_tests: tuple[dict, ...],
        binds: dict[str, str],
    ) -> None:
        self.rule_name = rule_name
        self.ce_index = ce_index
        self.alpha = alpha
        self.left = left
        self.output = output
        self.join_tests = join_tests
        self.binds = binds  # newly introduced variable -> WME field

    @property
    def equality_tests(self) -> list[dict]:
        return [t for t in self.join_tests if t["op"] == "=="]

    def __repr__(self) -> str:  # pragma: no cover - debug aid
        return f"<JoinNode {self.rule_name}#{self.ce_index}>"


# ---------------------------------------------------------------------------
# Network
# ---------------------------------------------------------------------------

class ReteNetwork:
    def __init__(self, agenda: Agenda | None = None) -> None:
        self.agenda = agenda if agenda is not None else Agenda()
        self.wmes: dict[int, WME] = {}
        self.alpha_memories: dict[tuple, AlphaMemory] = {}
        self.alpha_by_type: dict[str, list[AlphaMemory]] = {}
        # (rule_name, ce_index) -> beta memory written by that CE's join
        self.beta_memories: dict[tuple[str, int], BetaMemory] = {}
        self.terminals: dict[str, BetaMemory] = {}
        self.rules: dict[str, CompiledRule] = {}

    # -- rule construction --------------------------------------------------

    def add_rule(self, rule: CompiledRule) -> None:
        if rule.name in self.rules:
            raise ValueError(f"rule already installed: {rule.name!r}")
        if not rule.enabled:
            self.rules[rule.name] = rule
            return
        previous: BetaMemory | None = None
        for ce_index, plan in enumerate(rule.plans):
            alpha = self._get_or_create_alpha(plan)
            output = BetaMemory(
                rule_name=rule.name,
                ce_index=ce_index,
                depth=ce_index + 1,
                terminal=ce_index == len(rule.plans) - 1,
            )
            join = JoinNode(
                rule_name=rule.name,
                ce_index=ce_index,
                alpha=alpha,
                left=previous,
                output=output,
                join_tests=tuple(t for t in plan.tests if t["stage"] == "join"),
                binds=dict(plan.binds),
            )
            alpha.consumers.append(join)
            for test in join.join_tests:
                alpha.register_index_field(test["field"])
            if previous is not None:
                previous.consumers.append(join)
            output.register_variables(plan.binds.keys())
            if previous is not None:
                output.register_variables(previous.var_index.keys())
            self.alpha_memories[alpha.key] = alpha
            self.beta_memories[(rule.name, ce_index)] = output
            previous = output
            # Prime the new join with everything the alpha memory already
            # holds; token dedupe in BetaMemory.store makes this idempotent.
            for wme_id in sorted(alpha.wmes):
                self._right_activation(join, alpha.wmes[wme_id], _noop_log)
        self.terminals[rule.name] = previous  # type: ignore[assignment]
        self.rules[rule.name] = rule

    def _get_or_create_alpha(self, plan: AlphaTestPlan) -> AlphaMemory:
        key = plan.alpha_key()
        memory = self.alpha_memories.get(key)
        if memory is None:
            memory = AlphaMemory(
                key=key,
                fact_type=plan.fact_type,
                alpha_tests=plan.alpha_tests,
            )
            self.alpha_memories[key] = memory
            self.alpha_by_type.setdefault(plan.fact_type, []).append(memory)
        return memory

    # -- insert / retract ---------------------------------------------------

    def insert_wme(self, wme: WME, log: LogSink = None) -> list[tuple[str, tuple[int, ...]]]:
        """Route a new WME. Returns terminal activations created ``(rule,key)``.

        Evaluation is transactional: if any join comparison is undefined the
        partial propagation is rolled back and the WME ends up nowhere.
        """

        sink = log or _noop_log
        self.wmes[wme.id] = wme
        activated: list[tuple[str, tuple[int, ...]]] = []
        try:
            for memory in self.alpha_by_type.get(wme.type, []):
                if not self._alpha_accepts(memory, wme):
                    continue
                memory.add(wme)
                sink(
                    "alpha_memory_hit",
                    alpha_type=memory.fact_type,
                    wme_id=wme.id,
                    field_indexes=memory.index_stats(),
                )
                for join in list(memory.consumers):
                    self._right_activation(join, wme, sink, activated)
        except PredicateError as exc:
            self._rollback_insert(wme.id)
            raise RuleEvaluationError(
                f"wme {wme.id} ({wme.type}): undefined comparison: {exc}"
            ) from exc
        return activated

    def _rollback_insert(self, wme_id: int) -> None:
        try:
            self._retract(wme_id)
        finally:
            self.wmes.pop(wme_id, None)

    def retract_wme(self, wme_id: int, log: LogSink = None) -> WME:
        if wme_id not in self.wmes:
            raise UnknownFactError(f"cannot retract: working-memory id {wme_id} does not exist")
        return self._retract(wme_id, log or _noop_log)

    def _retract(self, wme_id: int, sink: LogSink = None) -> WME:
        log = sink or _noop_log
        wme = self.wmes.pop(wme_id)
        for memory in self.alpha_by_type.get(wme.type, []):
            if wme_id not in memory.wmes:
                continue
            memory.remove(wme_id)
            log("alpha_memory_retract", alpha_type=memory.fact_type, wme_id=wme_id)
            for join in list(memory.consumers):
                doomed = list(join.output.wme_index.get(wme_id, ()))
                for key in doomed:
                    self._erase_token(join.output, key, log)
        return wme

    # -- alpha evaluation ---------------------------------------------------

    @staticmethod
    def _alpha_accepts(memory: AlphaMemory, wme: WME) -> bool:
        # Phase 1: every binding field must exist; collect local bindings.
        # Done first so evaluation of same-CE variable tests is independent
        # of declared constraint ordering (shared memories key by test set).
        local_bindings: dict[str, Any] = {}
        for test in memory.alpha_tests:
            if test["kind"] != "bind":
                continue
            value = wme.fields.get(test["field"], MISSING)
            if value is MISSING:
                return False
            local_bindings[test["variable"]] = value
        # Phase 2: literal and same-CE variable predicates.
        for test in memory.alpha_tests:
            kind = test["kind"]
            if kind == "bind":
                continue
            value = wme.fields.get(test["field"], MISSING)
            if value is MISSING:
                return False
            if kind == "lit":
                if not evaluate(Operator(test["op"]), value, test["value"]):
                    return False
            elif kind == "var":
                if not evaluate(Operator(test["op"]), value, local_bindings[test["variable"]]):
                    return False
        return True

    # -- join propagation ---------------------------------------------------

    def _right_activation(
        self,
        join: JoinNode,
        wme: WME,
        sink: LogSink,
        activated: list[tuple[str, tuple[int, ...]]] | None = None,
    ) -> None:
        """A new WME arrives on the right: probe the left memory by index."""

        left_tokens: list[Token | None]
        if join.left is None:
            left_tokens = [None]
        else:
            left_tokens = list(self._probe_left_tokens(join, wme))
        sink(
            "join_right_activation",
            rule=join.rule_name,
            ce_index=join.ce_index,
            wme_id=wme.id,
            left_candidates=len(left_tokens),
        )
        for token in left_tokens:
            extended = self._extend(join, token, wme)
            if extended is not None:
                self._commit_token(join, extended, sink, activated)

    def _left_activation(self, join: JoinNode, token: Token, sink: LogSink) -> None:
        """A new token arrives on the left: probe the alpha memory by index."""

        wme_ids = list(self._probe_right_wmes(join, token))
        sink(
            "join_left_activation",
            rule=join.rule_name,
            ce_index=join.ce_index,
            parent_key=list(token.key),
            right_candidates=len(wme_ids),
        )
        for wme_id in wme_ids:
            extended = self._extend(join, token, join.alpha.wmes[wme_id])
            if extended is not None:
                self._commit_token(join, extended, sink)

    def _probe_left_tokens(self, join: JoinNode, wme: WME) -> set[Token]:
        assert join.left is not None
        candidate_keys: set[tuple[int, ...]] | None = None
        for test in join.equality_tests:
            value = wme.fields.get(test["field"], MISSING)
            if value is MISSING:
                return set()
            table = join.left.var_index.get(test["variable"], {})
            keys = table.get(value, set())
            candidate_keys = set(keys) if candidate_keys is None else candidate_keys & set(keys)
            if not candidate_keys:
                return set()
        if candidate_keys is None:
            candidate_keys = set(join.left.tokens)
        return {join.left.tokens[k] for k in candidate_keys}

    def _probe_right_wmes(self, join: JoinNode, token: Token) -> set[int]:
        candidate_ids: set[int] | None = None
        for test in join.equality_tests:
            if test["variable"] not in token.bindings:
                return set()
            value = token.bindings[test["variable"]]
            table = join.alpha.field_index.get(test["field"], {})
            ids = table.get(value, set())
            candidate_ids = set(ids) if candidate_ids is None else candidate_ids & set(ids)
            if not candidate_ids:
                return set()
        if candidate_ids is None:
            candidate_ids = set(join.alpha.wmes)
        return candidate_ids

    def _extend(self, join: JoinNode, token: Token | None, wme: WME) -> Token | None:
        bindings: dict[str, Any] = {} if token is None else dict(token.bindings)
        for test in join.join_tests:
            right = wme.fields.get(test["field"], MISSING)
            if right is MISSING or test["variable"] not in bindings:
                return None
            if not evaluate(Operator(test["op"]), right, bindings[test["variable"]]):
                return None
        for variable, field_name in join.binds.items():
            value = wme.fields.get(field_name, MISSING)
            if value is MISSING:
                return None
            bindings[variable] = value
        parent_wmes: tuple[int, ...] = () if token is None else token.wme_ids
        key = parent_wmes + (wme.id,)
        parent_key = DUMMY_KEY if token is None else token.key
        return Token(key=key, wme_ids=key, bindings=bindings, parent_key=parent_key)

    def _commit_token(
        self,
        join: JoinNode,
        token: Token,
        sink: LogSink,
        activated: list[tuple[str, tuple[int, ...]]] | None = None,
    ) -> None:
        memory = join.output
        if not memory.store(token):
            return
        sink(
            "beta_token_stored",
            rule=join.rule_name,
            depth=memory.depth,
            token=list(token.key),
            beta_indexes=memory.index_stats(),
        )
        if memory.terminal:
            self._activate(join, memory, token, sink, activated)
        else:
            for child in list(memory.consumers):
                self._left_activation(child, token, sink)

    def _activate(
        self,
        join: JoinNode,
        memory: BetaMemory,
        token: Token,
        sink: LogSink,
        activated: list[tuple[str, tuple[int, ...]]] | None,
    ) -> None:
        rule = self.rules[join.rule_name]
        activation = self.agenda.add(
            rule.name,
            rule.salience,
            token.wme_ids,
            dict(token.bindings),
            refraction=rule.refraction,
        )
        if activation is None:
            sink(
                "activation_suppressed_refraction",
                rule=rule.name,
                token=list(token.key),
            )
            return
        sink(
            "activation_queued",
            rule=rule.name,
            salience=rule.salience,
            sequence=activation.sequence,
            stable_key=activation.stable_key,
            token=list(token.key),
            bindings=dict(token.bindings),
            sources=self.sources_for(token.wme_ids),
        )
        if activated is not None:
            activated.append((rule.name, token.key))

    def _erase_token(self, memory: BetaMemory, key: tuple[int, ...], sink: LogSink) -> None:
        token = memory.erase(key)
        if token is None:
            return
        sink(
            "beta_token_removed",
            rule=memory.rule_name,
            depth=memory.depth,
            token=list(key),
        )
        if memory.terminal:
            entry = self.agenda.invalidate(memory.rule_name, key)
            self.agenda.unmark_fired(memory.rule_name, key)
            sink(
                "activation_invalidated",
                rule=memory.rule_name,
                token=list(key),
                was_queued=entry is not None,
                salience=entry.salience if entry is not None else 0,
                sequence=entry.sequence if entry is not None else 0,
            )
        # Cascade to every deeper token built on top of this one.
        for child_join in list(memory.consumers):
            child_keys = list(child_join.output.children.get(key, ()))
            for child_key in child_keys:
                self._erase_token(child_join.output, child_key, sink)

    # -- queries ------------------------------------------------------------

    def sources_for(self, wme_ids: tuple[int, ...]) -> list[dict[str, Any]]:
        sources: list[dict[str, Any]] = []
        for position, wme_id in enumerate(wme_ids):
            wme = self.wmes.get(wme_id)
            if wme is None:
                continue
            sources.append(
                {
                    "ce_index": position,
                    "wme_id": wme.id,
                    "type": wme.type,
                    "fields": dict(wme.fields),
                }
            )
        return sources

    def terminal_bindings_provider(self, rule_name: str, token_key: tuple[int, ...]) -> dict | None:
        terminal = self.terminals.get(rule_name)
        if terminal is None:
            return None
        token = terminal.tokens.get(token_key)
        return None if token is None else dict(token.bindings)

    def terminal_conflict_set(self) -> set[tuple[str, tuple[int, ...], tuple]]:
        """All terminal tokens as ``(rule, wme-id tuple, frozen bindings)``.

        Reads the beta terminal memories directly (not the agenda), so the
        result is the pure match result independent of refraction/firing -
        exactly what the brute-force reference matcher is compared against.
        """

        conflict: set[tuple[str, tuple[int, ...], tuple]] = set()
        for rule_name, memory in self.terminals.items():
            for token in memory.tokens.values():
                frozen = tuple(sorted(
                    (k, _freeze_binding(v)) for k, v in token.bindings.items()
                ))
                conflict.add((rule_name, token.wme_ids, frozen))
        return conflict

    def snapshot_activations(self) -> list[dict[str, Any]]:
        rows: list[dict[str, Any]] = []
        for entry in self.agenda.peek_ordered():
            bindings = self.terminal_bindings_provider(entry.rule_name, entry.token_key) or {}
            rows.append(
                {
                    "rule": entry.rule_name,
                    "salience": entry.salience,
                    "sequence": entry.sequence,
                    "stable_key": entry.stable_key,
                    "wme_ids": list(entry.token_key),
                    "bindings": bindings,
                    "sources": self.sources_for(entry.token_key),
                }
            )
        return rows

    def network_digest(self) -> dict[str, Any]:
        return {
            "wme_count": len(self.wmes),
            "alpha_memories": [
                {
                    "type": m.fact_type,
                    "members": len(m.wmes),
                    "field_indexes": m.index_stats(),
                    "consumers": [f"{j.rule_name}#{j.ce_index}" for j in m.consumers],
                }
                for m in self.alpha_memories.values()
            ],
            "beta_memories": [
                {
                    "rule": m.rule_name,
                    "ce_index": m.ce_index,
                    "depth": m.depth,
                    "terminal": m.terminal,
                    **m.index_stats(),
                }
                for m in self.beta_memories.values()
            ],
            "agenda_size": len(self.agenda),
        }
