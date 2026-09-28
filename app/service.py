"""Service layer: request orchestration around the numerical engine.

The HTTP layer stays thin; this module owns request ids, configuration
overrides, redacted payload diagnostics and the accept/reject narrative.
"""

from __future__ import annotations

import time
import uuid
from dataclasses import dataclass

from .config import Config
from .input_parsing import PayloadError, parse_system
from .logging_setup import get_logger, reset_request_id, set_request_id
from .numerical import arithmetic as arith
from .numerical import workprec
from .numerical.engine import solve_system

LOG = get_logger("service")

_OK_STATUSES = ("accepted", "partially_accepted")


@dataclass(frozen=True)
class ServiceResponse:
    request_id: str
    result: dict
    http_status: int


class SolveService:
    def __init__(self, config: Config) -> None:
        self._config = config

    def solve(self, payload: dict, request_id: str | None = None) -> ServiceResponse:
        rid = request_id or f"req-{uuid.uuid4().hex[:12]}"
        token = set_request_id(rid)
        started = time.perf_counter()
        try:
            config = self._resolve_config(payload)
            parsed = parse_system(payload)
            self._log_redacted(parsed)
            result = solve_system(parsed.a, parsed.b, config, rid)
            status = 200 if result.status in _OK_STATUSES else 422
            return ServiceResponse(rid, result.to_dict(), status)
        except PayloadError as exc:
            LOG.warning("rejected: %s", exc)
            return self._error(rid, "invalid_request", str(exc), 400)
        except Exception:  # pragma: no cover - defensive envelope
            LOG.exception("unexpected failure")
            return self._error(
                rid,
                "internal_error",
                "unexpected internal error (correlation id recorded)",
                500,
            )
        finally:
            LOG.info("elapsed=%.4fs", time.perf_counter() - started)
            reset_request_id(token)

    def _resolve_config(self, payload: dict) -> Config:
        overrides = payload.get("config") if isinstance(payload, dict) else None
        if overrides is None:
            return self._config
        if not isinstance(overrides, dict):
            raise PayloadError("'config' must be an object")
        try:
            return self._config.with_overrides(overrides)
        except ValueError as exc:
            raise PayloadError(f"invalid config override: {exc}") from exc

    @staticmethod
    def _log_redacted(parsed) -> None:
        # Entries are never logged; only shapes and aggregate norms.
        with workprec(parsed.n * 4 + 40):
            a_norm = arith.infinity_norm(parsed.a)
            b_norm = arith.infinity_norm(parsed.b)
        LOG.info(
            "A: shape=%dx%d inf_norm=%s (entries redacted) | "
            "B: shape=%dx%d inf_norm=%s (entries redacted) | string_entries=%d",
            parsed.n, parsed.n, a_norm,
            parsed.n, parsed.nrhs, b_norm, parsed.string_entries,
        )

    @staticmethod
    def _error(rid: str, status: str, reason: str, http_status: int) -> ServiceResponse:
        return ServiceResponse(
            rid,
            {"request_id": rid, "status": status, "reason": reason},
            http_status,
        )
