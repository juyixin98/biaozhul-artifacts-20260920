"""Orchestration: parse -> compile -> semi-naive evaluate -> persist.

The service keeps an in-process registry of compiled programs keyed by
content hash so repeat requests skip compilation and evaluation; the
SQLite store provides the same deduplication across restarts.
"""

from __future__ import annotations

import dataclasses
import threading
from dataclasses import dataclass
from hashlib import sha256
from typing import Dict, Optional, Sequence

from .engine.fixpoint import Materialization, evaluate
from .language.ast import Atom, Program
from .language.compiler import CompiledProgram, compile_program
from .language.parser import parse_program, parse_query
from .storage.evidence_store import EvidenceStore


@dataclass(frozen=True)
class PreparedProgram:
    program_id: str
    compiled: CompiledProgram
    facts: Sequence[Atom]
    materialization: Materialization
    reused: bool


class DatalogService:
    def __init__(self, store: EvidenceStore):
        self.store = store
        self._cache: Dict[str, PreparedProgram] = {}
        self._lock = threading.RLock()

    def submit(self, source: str) -> PreparedProgram:
        program: Program = parse_program(source)
        compiled = compile_program(program)
        materialization = evaluate(compiled, program.facts)
        program_id = sha256(
            (compiled.rule_version + ":" + materialization.fact_set_version).encode()
        ).hexdigest()[:16]

        with self._lock:
            cached = self._cache.get(program_id)
            if cached is not None:
                if cached.reused:
                    return cached
                reused = dataclasses.replace(cached, reused=True)
                self._cache[program_id] = reused
                return reused

            reused_existing = not self.store.save_program(
                program_id, compiled, program.facts, materialization.fact_set_version
            )
            self.store.save_materialization(materialization, program_id)
            prepared = PreparedProgram(
                program_id=program_id,
                compiled=compiled,
                facts=program.facts,
                materialization=materialization,
                reused=reused_existing,
            )
            self._cache[program_id] = prepared
            return prepared

    def get(self, program_id: str) -> Optional[PreparedProgram]:
        with self._lock:
            return self._cache.get(program_id)

    @staticmethod
    def parse_goal(text: str) -> Atom:
        return parse_query(text)
