"""Shared API dependencies.

A single service instance owns the SQLite connection and the cached treaps;
tests build their own app against a temp database by overriding
``get_settings``. A small fixed-window limiter guards write endpoints (local
in-process policy, adequate for the single-node demo).
"""
from __future__ import annotations

import threading
import time
from dataclasses import dataclass

from fastapi import Header, HTTPException, Request

from .config import Settings
from .diagnostics import DiagnosticLog, new_request_id
from .mining.lexer import Lexer
from .service import DocumentService


@dataclass
class AppState:
    settings: Settings
    service: DocumentService
    diagnostics: DiagnosticLog
    lexer: Lexer


class FixedWindowLimiter:
    def __init__(self, max_requests: int, window_seconds: float = 60.0) -> None:
        self._max = max_requests
        self._window = window_seconds
        self._hits: dict[str, tuple[float, int]] = {}
        self._lock = threading.Lock()

    def check(self, key: str) -> None:
        now = time.monotonic()
        with self._lock:
            started, count = self._hits.get(key, (now, 0))
            if now - started >= self._window:
                started, count = now, 0
            count += 1
            self._hits[key] = (started, count)
            if count > self._max:
                raise HTTPException(status_code=429, detail="rate limit exceeded")


def get_state(request: Request) -> AppState:
    return request.app.state.app_state


def request_id(x_request_id: str | None = Header(default=None)) -> str:
    return x_request_id or new_request_id()
