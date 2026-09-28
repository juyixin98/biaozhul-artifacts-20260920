"""Structured request tracing.

Every request gets a correlation id (client supplied ``X-Request-ID`` or a
generated UUID) and a step trace that records *what happened where*: stage
name, elapsed time, and the algorithm/library version responsible. The same
correlation id is attached to log records via a contextvar + filter.
"""

from contextvars import ContextVar
from dataclasses import dataclass, field
import logging
import os
import platform
import re
import time
import uuid

from sym_eig.version import __version__

_request_id: ContextVar[str] = ContextVar("request_id", default="-")

# Correlation ids are reflected into logs and responses; restrict them to a
# safe charset with a length cap to prevent log injection / forgery via
# CR/LF/ANSI and unbounded reflection.
_REQUEST_ID_RE = re.compile(r"[^A-Za-z0-9._-]")
_REQUEST_ID_MAX_LEN = 128


def set_request_id(request_id: str | None) -> str:
    if not request_id:
        rid = str(uuid.uuid4())
    else:
        cleaned = _REQUEST_ID_RE.sub("_", str(request_id))[:_REQUEST_ID_MAX_LEN]
        rid = cleaned or str(uuid.uuid4())
    _request_id.set(rid)
    return rid


def get_request_id() -> str:
    return _request_id.get()


class RequestIdFilter(logging.Filter):
    def filter(self, record: logging.LogRecord) -> bool:
        record.request_id = _request_id.get()
        return True


def configure_logging(level: int = logging.INFO) -> logging.Logger:
    logger = logging.getLogger("sym_eig")
    if not logger.handlers:
        handler = logging.StreamHandler()
        handler.setFormatter(
            logging.Formatter(
                "%(asctime)s %(levelname)s [request_id=%(request_id)s] "
                "%(name)s: %(message)s"
            )
        )
        handler.addFilter(RequestIdFilter())
        logger.addHandler(handler)
        logger.setLevel(level)
        logger.propagate = False
    return logger


@dataclass
class StepTrace:
    step: str
    elapsed_ms: float
    detail: dict = field(default_factory=dict)

    def to_dict(self) -> dict:
        return {
            "step": self.step,
            "elapsed_ms": round(self.elapsed_ms, 3),
            "detail": self.detail,
        }


class Tracer:
    """Collects key processing steps with timings and logs them."""

    def __init__(self) -> None:
        self.steps: list[StepTrace] = []
        self._logger = configure_logging()

    def record(self, step: str, detail: dict | None = None) -> None:
        detail = detail or {}
        self._logger.info("%s %s", step, detail if detail else "")
        self.steps.append(
            StepTrace(step=step, elapsed_ms=0.0, detail=detail)
        )

    class _Span:
        def __init__(self, tracer: "Tracer", step: str, detail: dict) -> None:
            self.tracer = tracer
            self.step = step
            self.detail = detail
            self.start = 0.0

        def __enter__(self) -> "Tracer._Span":
            self.start = time.perf_counter()
            return self

        def __exit__(self, *exc: object) -> None:
            elapsed_ms = (time.perf_counter() - self.start) * 1000.0
            self.tracer._logger.info(
                "%s (%.3f ms) %s",
                self.step, elapsed_ms, self.detail if self.detail else "",
            )
            self.tracer.steps.append(
                StepTrace(
                    step=self.step, elapsed_ms=elapsed_ms, detail=self.detail
                )
            )

    def step(self, step: str, **detail: object) -> "Tracer._Span":
        return Tracer._Span(self, step, dict(detail))


def processing_location() -> dict:
    """Where this result was produced (host + process + versions)."""
    import numpy
    import scipy
    import mpmath

    return {
        "host": platform.node(),
        "python": platform.python_version(),
        "pid": os.getpid(),
        "service_version": __version__,
        "numpy": numpy.__version__,
        "scipy": scipy.__version__,
        "mpmath": mpmath.__version__,
    }
