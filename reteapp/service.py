"""Application service: one run/session = engine + evidence + correlation id.

The service is the only place where the matching core and the evidence store
meet. Every operation is recorded against a ``run_id`` so that API logs,
SQLite rows and structured trace lines are correlatable.
"""

from __future__ import annotations

import threading
import uuid
from typing import Any

from .config import Settings
from .core.engine import (
    DUPLICATE_MULTISET,
    DUPLICATE_POLICIES,
    Engine,
    FireReport,
    InsertResult,
)
from .core.errors import ReteError
from .lang.compiler import CompiledRule, compile_rules
from .lang.parser import RuleParseError, parse_rules_json
from .logging_setup import RunLogger, utc_now_iso
from .storage.evidence import EvidenceStore
from .version import __version__

# Network-internal events are emitted at DEBUG; everything the engine itself
# emits is INFO. Used when projecting recorder calls into the evidence trace.
_DEBUG_EVENTS = frozenset(
    {
        "alpha_memory_hit",
        "alpha_memory_retract",
        "join_right_activation",
        "join_left_activation",
        "beta_token_stored",
        "beta_token_removed",
    }
)


class SessionError(ReteError):
    category = "session_error"


class UnknownSessionError(SessionError):
    category = "unknown_session_error"


class RunSession:
    def __init__(
        self,
        run_id: str,
        settings: Settings,
        compiled: list[CompiledRule],
        store: EvidenceStore,
        *,
        duplicate_policy: str,
    ) -> None:
        self.run_id = run_id
        self.settings = settings
        self.store = store
        self.logger = RunLogger(run_id, level=settings.log_level)
        self._lock = threading.RLock()
        self._trace_seq = 0
        self._fire_order = 0
        self.store.start_run(run_id, settings.describe(), note=f"engine {__version__}")
        self.store.record_rules(run_id, compiled)
        # Emit the ordered run-start marker into the evidence trace BEFORE the
        # engine is constructed, so it precedes rules_loaded/network events.
        self.store.append_trace(
            run_id,
            self._next_trace_seq(),
            utc_now_iso(),
            "INFO",
            "run_started",
            {
                "settings": settings.describe(),
                "rules": [r.name for r in compiled],
                "duplicate_policy": duplicate_policy,
            },
        )
        self.engine = Engine(
            compiled,
            max_fire_rounds=settings.max_fire_rounds,
            duplicate_policy=duplicate_policy,
            recorder=self._record,
            logger=self.logger,
        )
        self.logger.info(
            "run_started",
            settings=settings.describe(),
            rules=[r.name for r in compiled],
            duplicate_policy=duplicate_policy,
        )

    def _next_trace_seq(self) -> int:
        self._trace_seq += 1
        return self._trace_seq

    def _record(self, event: str, fields: dict[str, Any]) -> None:
        """Recorder fed by the engine/network; projects events onto evidence."""

        seq = self._next_trace_seq()
        level = "DEBUG" if event in _DEBUG_EVENTS else "INFO"
        self.store.append_trace(
            self.run_id, seq, utc_now_iso(), level, event, fields,
        )
        if event == "fact_inserted":
            self.store.record_fact(
                self.run_id, fields["wme_id"], "insert", fields["type"], fields["fields"],
                fields["content_key"], fields.get("origin", "external"),
                duplicate=bool(fields.get("duplicate")),
            )
        elif event == "fact_retracted":
            self.store.record_fact(
                self.run_id, fields["wme_id"], "retract", fields["type"], fields["fields"],
                fields["content_key"], fields.get("origin", "external"),
            )
        elif event == "activation_queued":
            self.store.record_activation(
                self.run_id,
                "queued",
                rule_name=fields["rule"],
                stable_key=fields["stable_key"],
                wme_ids=fields["token"],
                bindings=fields.get("bindings", {}),
                sources=fields.get("sources", []),
                salience=fields["salience"],
                seq=fields["sequence"],
            )
        elif event == "activation_invalidated":
            self.store.record_activation(
                self.run_id,
                "invalidated",
                rule_name=fields["rule"],
                stable_key=f"{fields['rule']}[{'-'.join(map(str, fields['token']))}]",
                wme_ids=fields["token"],
                bindings={},
                sources=[],
                salience=fields.get("salience", 0),
                seq=fields.get("sequence", 0),
            )
        elif event == "activation_suppressed_refraction":
            self.store.record_activation(
                self.run_id,
                "suppressed",
                rule_name=fields["rule"],
                stable_key=f"{fields['rule']}[{'-'.join(map(str, fields['token']))}]",
                wme_ids=fields["token"],
                bindings={},
                sources=[],
                salience=0,
                seq=-1,  # never obtained an agenda sequence
            )

    # -- operations ----------------------------------------------------------

    def insert(self, payload: dict[str, Any], duplicate_policy: str | None = None) -> InsertResult:
        with self._lock:
            return self.engine.insert_fact(payload, duplicate_policy=duplicate_policy)

    def retract(self, wme_id: int) -> dict[str, Any]:
        with self._lock:
            return self.engine.retract_fact(wme_id)

    def fire(self, mode: str, max_firings: int | None = None) -> FireReport:
        with self._lock:
            if mode == "next":
                _last, report = self.engine.fire_next()
            elif mode == "step":
                limit = max_firings or 1
                report = self.engine.fire_step(limit)
            elif mode == "all":
                report = self.engine.fire_all()
            else:
                raise SessionError(f"unknown fire mode {mode!r}")
            for offset, fired in enumerate(report.fired):
                self._fire_order += 1
                self.store.record_firing(self.run_id, fired, self._fire_order)
                self.store.record_activation(
                    self.run_id,
                    "fired",
                    rule_name=fired.rule,
                    stable_key=fired.stable_key,
                    wme_ids=fired.wme_ids,
                    bindings=fired.bindings,
                    sources=fired.sources,
                    salience=fired.salience,
                    seq=fired.sequence,
                )
            return report

    def agenda(self) -> list[dict[str, Any]]:
        with self._lock:
            return self.engine.activations()

    def working_memory(self) -> list[dict[str, Any]]:
        with self._lock:
            return self.engine.working_memory()

    def digest(self) -> dict[str, Any]:
        with self._lock:
            return self.engine.digest()

    def trace(self) -> list[dict[str, Any]]:
        return self.store.get_trace(self.run_id)

    def evidence(self) -> dict[str, Any]:
        return self.store.get_evidence_summary(self.run_id)


class SessionManager:
    """In-process registry of run sessions."""

    def __init__(self) -> None:
        self._sessions: dict[str, RunSession] = {}
        self._lock = threading.RLock()

    def create(
        self,
        rules_document: Any,
        settings: Settings,
        *,
        run_id: str | None = None,
        duplicate_policy: str = DUPLICATE_MULTISET,
        store: EvidenceStore | None = None,
    ) -> RunSession:
        if duplicate_policy not in DUPLICATE_POLICIES:
            raise SessionError(f"duplicate_policy must be one of {DUPLICATE_POLICIES}")
        try:
            rules = parse_rules_json(rules_document)
            compiled = compile_rules(rules)
        except (RuleParseError, ValueError) as exc:
            raise SessionError(f"invalid rule set: {exc}") from exc
        run_id = run_id or f"run-{uuid.uuid4().hex[:12]}"
        with self._lock:
            if run_id in self._sessions:
                raise SessionError(f"run_id {run_id!r} already exists")
            db_path = store.db_path if store is not None else settings.db_path
            evidence = store or EvidenceStore(db_path)
            session = RunSession(
                run_id, settings, compiled, evidence, duplicate_policy=duplicate_policy
            )
            self._sessions[run_id] = session
        return session

    def get(self, run_id: str) -> RunSession:
        session = self._sessions.get(run_id)
        if session is None:
            raise UnknownSessionError(f"unknown run_id: {run_id!r}")
        return session

    def list(self) -> list[dict[str, Any]]:
        with self._lock:
            return [
                {
                    "run_id": s.run_id,
                    "settings": s.settings.describe(),
                    "wme_count": len(s.engine.network.wmes),
                    "agenda_size": len(s.engine.agenda),
                }
                for s in self._sessions.values()
            ]
