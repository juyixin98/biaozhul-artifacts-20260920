"""Structured diagnostics for accept / reject / undetermined decisions.

Every query answer carries a :class:`DecisionRecord` with a request id,
the key engine state behind the verdict and a machine-readable reason.
Records render to redacted dictionaries for logs and API responses.

The system itself only handles synthetic identifiers, but the redaction
helper exists so any future free-text/sensitive payload is masked rather
than printed verbatim.
"""

from __future__ import annotations

import re
import uuid
from dataclasses import asdict, dataclass, field
from typing import Any, Dict, FrozenSet, List, Optional
ACCEPTED = "accepted"
REJECTED = "rejected"
UNDETERMINED = "undetermined"

# Verdict -> reason codes
REASON_SUPPORTED = "supported_by_environment"
REASON_NOGOOD = "blocked_by_nogood"
REASON_NO_ENV = "no_consistent_environment"
REASON_INCOMPLETE = "propagation_incomplete"

_SENSITIVE_KEY = re.compile(r"(token|secret|password|passwd|key|credential)", re.I)
_MASK = "***REDACTED***"


def new_request_id() -> str:
    return "req-" + uuid.uuid4().hex[:12]


def redact(payload: Any) -> Any:
    """Recursively mask values belonging to sensitive-looking keys."""
    if isinstance(payload, dict):
        return {
            k: (_MASK if _SENSITIVE_KEY.search(str(k)) else redact(v))
            for k, v in payload.items()
        }
    if isinstance(payload, (list, tuple)):
        return [redact(v) for v in payload]
    return payload


def _env(env: Optional[FrozenSet[str]]) -> Optional[List[str]]:
    return sorted(env) if env is not None else None


@dataclass
class DecisionRecord:
    request_id: str
    problem_id: str
    node_id: str
    decision: str
    reason: str
    query_environment: Optional[List[str]] = None
    supporting_environments: List[List[str]] = field(default_factory=list)
    nogood_blockers: List[List[str]] = field(default_factory=list)
    nogoods: List[List[str]] = field(default_factory=list)
    incomplete: bool = False
    incomplete_reason: Optional[str] = None

    def as_log_dict(self) -> Dict[str, Any]:
        """Safe-for-logs representation (sensitive values masked)."""
        return redact(asdict(self))
