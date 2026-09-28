"""Production engine around the Rete network.

Responsibilities beyond pure matching:

* fact validation and working-memory identity policy;
* agenda-driven firing with explicit, bounded outcomes;
* action execution (assert / retract matched facts / stop) and chaining;
* structured trace + evidence recording for every step.

Rules can **never** spin forever: ``auto`` firing is capped by
``max_fire_rounds`` (config layer) and returns ``status="limit_reached"`` with
the remaining activation count instead of looping without bound.
"""

from __future__ import annotations

import hashlib
import json
from dataclasses import dataclass, field
from typing import Any, Callable

from ..lang.compiler import CompiledRule, compile_rules
from ..lang.model import Rule
from ..lang.parser import parse_rules_json
from ..version import __version__
from .agenda import Agenda
from .errors import FactValidationError
from .network import ReteNetwork
from .tokens import WME

# Insertion policies for structurally duplicate facts.
DUPLICATE_MULTISET = "multiset"   # both copies coexist (default; CLIPS-like)
DUPLICATE_IGNORE = "ignore"       # keep the first, return its id
DUPLICATE_REJECT = "reject"       # FactValidationError
DUPLICATE_POLICIES = (DUPLICATE_MULTISET, DUPLICATE_IGNORE, DUPLICATE_REJECT)

_PRIMITIVE = (str, int, float, bool, type(None))
Recorder = Callable[[str, dict[str, Any]], None]


@dataclass(frozen=True)
class FiredRecord:
    rule: str
    salience: int
    sequence: int
    stable_key: str
    wme_ids: list[int]
    bindings: dict[str, Any]
    sources: list[dict[str, Any]]
    asserted: list[int]
    retracted: list[int]
    stopped: bool


@dataclass(frozen=True)
class FireReport:
    status: str  # "fired" | "agenda_empty" | "stopped" | "limit_reached"
    fired: tuple[FiredRecord, ...]
    rounds_used: int
    remaining_activations: int
    max_rounds: int

    def to_dict(self) -> dict[str, Any]:
        return {
            "status": self.status,
            "rounds_used": self.rounds_used,
            "remaining_activations": self.remaining_activations,
            "max_rounds": self.max_rounds,
            "fired": [
                {
                    "rule": r.rule,
                    "salience": r.salience,
                    "sequence": r.sequence,
                    "stable_key": r.stable_key,
                    "wme_ids": list(r.wme_ids),
                    "bindings": r.bindings,
                    "sources": r.sources,
                    "asserted_wme_ids": r.asserted,
                    "retracted_wme_ids": r.retracted,
                    "stop": r.stopped,
                }
                for r in self.fired
            ],
        }


@dataclass
class InsertResult:
    wme_id: int
    accepted: bool
    duplicate: bool
    activations: list[str] = field(default_factory=list)


class Engine:
    def __init__(
        self,
        rules: list[Rule] | list[CompiledRule] | None = None,
        *,
        max_fire_rounds: int = 100,
        duplicate_policy: str = DUPLICATE_MULTISET,
        recorder: Recorder | None = None,
        logger: Any = None,
    ) -> None:
        if max_fire_rounds <= 0:
            raise ValueError("max_fire_rounds must be positive")
        if duplicate_policy not in DUPLICATE_POLICIES:
            raise ValueError(f"duplicate_policy must be one of {DUPLICATE_POLICIES}")
        self.max_fire_rounds = max_fire_rounds
        self.duplicate_policy = duplicate_policy
        self.recorder = recorder
        self.logger = logger
        self.agenda = Agenda()
        self.network = ReteNetwork(self.agenda)
        self._next_id = 1
        self._content_index: dict[str, int] = {}
        self.compiled_rules: dict[str, CompiledRule] = {}
        if rules:
            self.load_rules(rules)

    # -- logging / recording -------------------------------------------------

    def _emit(self, event: str, **fields: Any) -> None:
        if self.logger is not None:
            self.logger.info(event, **fields)
        if self.recorder is not None:
            self.recorder(event, fields)

    def _log_sink(self) -> Callable[..., None]:
        def sink(event: str, **fields: Any) -> None:
            if self.logger is not None:
                self.logger.debug(event, **fields)
            if self.recorder is not None:
                self.recorder(event, fields)

        return sink

    # -- rule loading --------------------------------------------------------

    def load_rules(self, rules: list[Rule] | list[CompiledRule] | list[dict]) -> None:
        if rules and isinstance(rules[0], CompiledRule):
            compiled = list(rules)  # type: ignore[arg-type]
        elif rules and isinstance(rules[0], Rule):
            compiled = compile_rules(list(rules))  # type: ignore[arg-type]
        else:
            compiled = compile_rules(parse_rules_json(rules))  # type: ignore[arg-type]
        for rule in compiled:
            self.network.add_rule(rule)
            self.compiled_rules[rule.name] = rule
        self._emit("rules_loaded", count=len(compiled), rules=[r.name for r in compiled])

    # -- validation ----------------------------------------------------------

    @staticmethod
    def content_key(fact_type: str, fields: dict[str, Any]) -> str:
        blob = json.dumps(
            {"type": fact_type, "fields": fields}, sort_keys=True, separators=(",", ":"),
            ensure_ascii=False,
        )
        return hashlib.sha256(blob.encode("utf-8")).hexdigest()[:16]

    @staticmethod
    def _validate_fact(payload: Any) -> tuple[str, dict[str, Any]]:
        if not isinstance(payload, dict):
            raise FactValidationError("fact must be a JSON object {'type': ..., 'fields': {...}}")
        fact_type = payload.get("type")
        if not isinstance(fact_type, str) or not fact_type.strip():
            raise FactValidationError("fact requires a non-empty string 'type'")
        fields = payload.get("fields", {})
        if not isinstance(fields, dict) or not all(isinstance(k, str) for k in fields):
            raise FactValidationError("fact 'fields' must be an object with string keys")
        for name, value in fields.items():
            if not isinstance(value, _PRIMITIVE):
                raise FactValidationError(
                    f"fact {fact_type!r} field {name!r}: value must be a JSON primitive "
                    f"(string/number/boolean/null); joins index primitive values"
                )
        return fact_type, dict(fields)

    # -- working memory ------------------------------------------------------

    def insert_fact(
        self,
        payload: dict[str, Any],
        *,
        duplicate_policy: str | None = None,
        origin: str = "external",
    ) -> InsertResult:
        fact_type, fields = self._validate_fact(payload)
        policy = duplicate_policy or self.duplicate_policy
        if policy not in DUPLICATE_POLICIES:
            raise FactValidationError(f"unknown duplicate policy {policy!r}")
        content = self.content_key(fact_type, fields)

        existing_id = self._content_index.get(content)
        if existing_id is not None:
            if policy == DUPLICATE_REJECT:
                raise FactValidationError(
                    f"duplicate fact rejected: {fact_type} {fields} already exists as wme {existing_id}"
                )
            if policy == DUPLICATE_IGNORE:
                self._emit(
                    "fact_duplicate_ignored",
                    wme_id=existing_id,
                    type=fact_type,
                    fields=fields,
                    content_key=content,
                    origin=origin,
                )
                return InsertResult(wme_id=existing_id, accepted=False, duplicate=True, activations=[])

        wme_id = self._next_id
        self._next_id += 1
        wme = WME(id=wme_id, type=fact_type, fields=fields, content_key=content)
        activated = self.network.insert_wme(wme, self._log_sink())
        self._content_index.setdefault(content, wme_id)
        rule_names = [name for name, _key in activated]
        self._emit(
            "fact_inserted",
            wme_id=wme_id,
            type=fact_type,
            fields=fields,
            content_key=content,
            duplicate=existing_id is not None,
            origin=origin,
            activations=rule_names,
        )
        return InsertResult(wme_id=wme_id, accepted=True, duplicate=existing_id is not None,
                            activations=rule_names)

    def retract_fact(self, wme_id: int, *, origin: str = "external") -> dict[str, Any]:
        wme = self.network.retract_wme(wme_id, self._log_sink())
        # Content index points at the first surviving WME of each content. If
        # that one disappears while an identical twin survives (multiset
        # policy), repoint; otherwise drop the entry.
        if self._content_index.get(wme.content_key) == wme_id:
            survivor = next(
                (
                    other.id
                    for other in self.network.wmes.values()
                    if other.content_key == wme.content_key
                ),
                None,
            )
            if survivor is None:
                del self._content_index[wme.content_key]
            else:
                self._content_index[wme.content_key] = survivor
        # Terminal tokens that contained this WME were erased (and their
        # refraction marks cleared) by the beta-memory cascade; nothing else
        # to clean here.
        self._emit(
            "fact_retracted",
            wme_id=wme_id,
            type=wme.type,
            fields=dict(wme.fields),
            content_key=wme.content_key,
            origin=origin,
        )
        return {"wme_id": wme_id, "type": wme.type, "fields": dict(wme.fields)}

    def working_memory(self) -> list[dict[str, Any]]:
        return [
            {"wme_id": w.id, "type": w.type, "fields": dict(w.fields), "content_key": w.content_key}
            for w in sorted(self.network.wmes.values(), key=lambda w: w.id)
        ]

    # -- firing --------------------------------------------------------------

    def fire_next(self) -> tuple[FiredRecord | None, FireReport]:
        return self._fire(limit=1, hard_cap=False)

    def fire_step(self, max_firings: int) -> FireReport:
        if max_firings <= 0:
            raise ValueError("max_firings must be positive")
        _record, report = self._fire(limit=max_firings, hard_cap=False)
        return report

    def fire_all(self) -> FireReport:
        _record, report = self._fire(limit=self.max_fire_rounds, hard_cap=True)
        return report

    def _fire(self, *, limit: int, hard_cap: bool) -> tuple[FiredRecord | None, FireReport]:
        fired: list[FiredRecord] = []
        last: FiredRecord | None = None
        status = "agenda_empty"
        for _round in range(limit):
            activation = self.agenda.pop(self.network.terminal_bindings_provider)
            if activation is None:
                status = "agenda_empty"
                break
            self.agenda.mark_fired(activation.rule_name, activation.token_key)
            sources = self.network.sources_for(activation.wme_ids)
            self._emit(
                "activation_firing",
                rule=activation.rule_name,
                salience=activation.salience,
                sequence=activation.sequence,
                stable_key=activation.stable_key,
                token=list(activation.wme_ids),
                bindings=activation.bindings,
                sources=sources,
            )
            asserted, retracted, stopped = self._execute_action(activation.rule_name, activation)
            record = FiredRecord(
                rule=activation.rule_name,
                salience=activation.salience,
                sequence=activation.sequence,
                stable_key=activation.stable_key,
                wme_ids=list(activation.wme_ids),
                bindings=activation.bindings,
                sources=sources,
                asserted=asserted,
                retracted=retracted,
                stopped=stopped,
            )
            fired.append(record)
            last = record
            self._emit(
                "activation_fired",
                rule=activation.rule_name,
                stable_key=activation.stable_key,
                asserted_wme_ids=asserted,
                retracted_wme_ids=retracted,
                stop=stopped,
                agenda_remaining=len(self.agenda),
            )
            if stopped:
                status = "stopped"
                break
        else:
            # The whole budget was consumed. For fire_all that is the bounded
            # loop guard; an explicit step/next simply used its batch.
            if hard_cap and len(self.agenda) > 0:
                status = "limit_reached"
                self._emit(
                    "fire_limit_reached",
                    max_rounds=limit,
                    remaining_activations=len(self.agenda),
                )
            else:
                status = "fired"
        if fired and status == "agenda_empty":
            status = "fired"
        report = FireReport(
            status=status,
            fired=tuple(fired),
            rounds_used=len(fired),
            remaining_activations=len(self.agenda),
            max_rounds=limit,
        )
        return last, report

    def _execute_action(self, rule_name: str, activation: Any) -> tuple[list[int], list[int], bool]:
        rule = self.compiled_rules[rule_name]
        action = rule.action
        asserted: list[int] = []
        retracted: list[int] = []
        for template in action["assert"]:
            payload = self._build_assert_payload(template, activation.bindings)
            result = self.insert_fact(payload, origin=f"rule:{rule_name}")
            asserted.append(result.wme_id)
        for ce_index in action["retract_ce_indices"]:
            target = activation.wme_ids[ce_index]
            if target in self.network.wmes:
                removed = self.retract_fact(target, origin=f"rule:{rule_name}")
                retracted.append(removed["wme_id"])
        return asserted, retracted, bool(action["stop"])

    @staticmethod
    def _build_assert_payload(template: dict, bindings: dict[str, Any]) -> dict[str, Any]:
        fields: dict[str, Any] = {}
        for field_name, spec in template["fields"].items():
            if isinstance(spec, dict) and "variable" in spec:
                fields[field_name] = bindings[spec["variable"]]
            else:
                fields[field_name] = spec["value"]
        return {"type": template["type"], "fields": fields}

    # -- introspection -------------------------------------------------------

    def activations(self) -> list[dict[str, Any]]:
        return self.network.snapshot_activations()

    def conflict_set(self) -> set[tuple[str, tuple[int, ...], tuple]]:
        """Pure match result from terminal beta memories (agenda-independent)."""

        return self.network.terminal_conflict_set()

    def digest(self) -> dict[str, Any]:
        return {"version": __version__, **self.network.network_digest()}
