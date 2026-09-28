"""Request-scoped telemetry for explainable failures.

Every request owns a ``Trace`` that records:

* the request id echoed back to the client and into the log line;
* ordered ``steps`` (what happened, where, under which index version);
* ``failures`` (categorical error reasons);
* ``uncertainties`` (degraded-mode conclusions that are correct but not
  guaranteed by an index seek, e.g. a full-scan fallback for prefix search).

The same trace object is rendered into both the HTTP response envelope and
the structured server log, so a reproduced failure can be traced step by step.
"""
from __future__ import annotations

import json
import logging
import sys
import uuid
from dataclasses import dataclass, field
from typing import Any

logger = logging.getLogger("collsvc")


def configure_logging(level: int = logging.INFO) -> None:
    if logger.handlers:
        return
    handler = logging.StreamHandler(sys.stderr)
    handler.setFormatter(
        logging.Formatter(
            fmt="%(message)s",
            validate=False,
        )
    )
    logger.addHandler(handler)
    logger.setLevel(level)
    logger.propagate = False


@dataclass(frozen=True)
class Step:
    name: str
    location: str
    detail: dict[str, Any] = field(default_factory=dict)


@dataclass(frozen=True)
class Failure:
    category: str
    reason: str
    location: str
    detail: dict[str, Any] = field(default_factory=dict)


@dataclass
class Trace:
    request_id: str = field(default_factory=lambda: uuid.uuid4().hex[:12])
    index_version: str | None = None
    steps: list[Step] = field(default_factory=list)
    failures: list[Failure] = field(default_factory=list)
    uncertainties: list[dict[str, Any]] = field(default_factory=list)

    def bind_version(self, version: str) -> None:
        self.index_version = version

    def step(self, name: str, location: str, **detail: Any) -> None:
        self.steps.append(Step(name=name, location=location, detail=detail))

    def fail(self, category: str, reason: str, location: str, **detail: Any) -> None:
        self.failures.append(Failure(category=category, reason=reason, location=location, detail=detail))

    def uncertain(self, conclusion: str, location: str, **detail: Any) -> None:
        self.uncertainties.append(
            {"conclusion": conclusion, "location": location, "detail": detail}
        )

    def to_dict(self) -> dict[str, Any]:
        return {
            "request_id": self.request_id,
            "index_version": self.index_version,
            "steps": [
                {"name": s.name, "location": s.location, "detail": s.detail}
                for s in self.steps
            ],
            "failures": [
                {
                    "category": f.category,
                    "reason": f.reason,
                    "location": f.location,
                    "detail": f.detail,
                }
                for f in self.failures
            ],
            "uncertainties": self.uncertainties,
        }

    def log(self, level: int = logging.INFO) -> None:
        """Emit one JSON log line carrying the whole trace."""
        payload = {"level": logging.getLevelName(level), **self.to_dict()}
        logger.log(level, json.dumps(payload, ensure_ascii=False, default=str))
