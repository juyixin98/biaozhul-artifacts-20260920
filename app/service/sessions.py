"""In-memory session store (local, process scoped)."""

from __future__ import annotations

import threading
from dataclasses import dataclass, field
from typing import Any, Dict, List, Mapping, Optional

import numpy as np

from ..core.graph import ValidatedGraph
from ..core.planner import CheckpointPlan
from ..core.state import RunState, TrainingState


@dataclass
class Session:
    session_id: str
    graph: ValidatedGraph
    training: TrainingState
    inputs: Mapping[str, np.ndarray]
    spec: Dict[str, Any]
    fixture_name: Optional[str]
    master_seed: int
    rng_strategy: str = "counter"
    plan: Optional[CheckpointPlan] = None
    plan_run_id: Optional[str] = None
    runs: List[RunState] = field(default_factory=list)
    last_run_id: Optional[str] = None

    @property
    def planned(self) -> bool:
        return self.plan is not None


class SessionStore:
    def __init__(self) -> None:
        self._sessions: Dict[str, Session] = {}
        self._lock = threading.Lock()

    def create(self, session: Session) -> Session:
        with self._lock:
            if session.session_id in self._sessions:
                raise KeyError("duplicate session id")
            self._sessions[session.session_id] = session
        return session

    def get(self, session_id: str) -> Session:
        with self._lock:
            try:
                return self._sessions[session_id]
            except KeyError:
                from ..core.errors import InvalidInputError

                raise InvalidInputError(
                    f"unknown session {session_id!r}",
                    code="E_SESSION_UNKNOWN",
                    context={"session_id": session_id},
                )

    def all(self) -> List[Session]:
        with self._lock:
            return list(self._sessions.values())
