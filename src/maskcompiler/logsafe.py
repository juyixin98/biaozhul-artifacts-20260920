"""Logging that can never emit original sensitive values.

Defense in depth: while a masking run traverses input data, every original
scalar value in the input is registered with the run's :class:`SecretScope`.
A logging filter then replaces any occurrence of those strings in log records
(message template, format args *and* exception tracebacks) with ``***``.

Scopes form a stack for nesting. When a run fails and the exception unwinds
past its scope, the scope's secrets are retained on a thread-local "escaped"
set keyed by the exception object: the very exception being logged carries
them, so a handler anywhere up the call stack (including one outside the
``with`` block) still gets scrubbing. Escaped secrets are dropped when the
exception object is garbage collected.

Exception types themselves are written to carry no data (see
``errors.py``); this filter also covers accidental ``logger.error("%s",
value)`` style logging anywhere in the stack.
"""

import contextvars
import logging
import weakref
from contextlib import contextmanager
from typing import Any, Iterator, List, Optional, Set

_REDACTION = "***"
_scope_stack: "contextvars.ContextVar[List[SecretScope]]" = contextvars.ContextVar(
    "maskcompiler_secret_scopes", default=[]
)
# Escaped exception -> its secret set (weak keys: auto cleanup with the
# exception object, no cross-request retention in the threaded server).
_escaped: "weakref.WeakKeyDictionary[BaseException, Set[str]]" = weakref.WeakKeyDictionary()


class SecretScope:
    def __init__(self) -> None:
        self._values: Set[str] = set()

    def add(self, value: Any) -> None:
        # Only non-empty strings can meaningfully leak through text logs.
        if isinstance(value, str) and value:
            self._values.add(value)

    def values(self) -> List[str]:
        # Longest first so that a long secret is redacted before its prefixes.
        return sorted(self._values, key=len, reverse=True)

    def snapshot(self) -> Set[str]:
        return set(self._values)


@contextmanager
def secret_scope() -> Iterator[SecretScope]:
    """Push a fresh secret registry for the duration of a masking run."""
    scope = SecretScope()
    stack = list(_scope_stack.get())
    stack.append(scope)
    token = _scope_stack.set(stack)
    try:
        yield scope
    except BaseException as exc:
        # The run failed: keep its secrets attached to the escaping exception
        # so loggers in outer handlers stay safe.
        _escaped[exc] = scope.snapshot()
        raise
    finally:
        _scope_stack.reset(token)


def register_secret(value: Any) -> None:
    """Register ``value`` with the innermost active scope (no-op if none)."""
    stack = _scope_stack.get()
    if stack:
        stack[-1].add(value)


def _all_secrets(record: Optional[logging.LogRecord] = None) -> List[str]:
    seen: Set[str] = set()
    for scope in _scope_stack.get():
        seen.update(scope.values())
    if record is not None and record.exc_info is not None:
        exc = record.exc_info[1]
        if exc is not None:
            seen.update(_escaped.get(exc, ()))
            # Chained exceptions (raise ... from ...) carry their own sets.
            for other in (exc.__cause__, exc.__context__):
                if other is not None:
                    seen.update(_escaped.get(other, ()))
    return sorted(seen, key=len, reverse=True)


def _scrub_text(text: str, secrets: List[str]) -> str:
    for secret in secrets:
        if secret and secret in text:
            text = text.replace(secret, _REDACTION)
    return text


def _scrub_args(args: Any, secrets: List[str]) -> Any:
    if isinstance(args, dict):
        return {k: _scrub_args(v, secrets) for k, v in args.items()}
    if isinstance(args, tuple):
        return tuple(_scrub_args(v, secrets) for v in args)
    if isinstance(args, list):
        return [_scrub_args(v, secrets) for v in args]
    if isinstance(args, str):
        return _scrub_text(args, secrets)
    return args


class SecretRedactionFilter(logging.Filter):
    """Replace registered secrets everywhere in a :class:`logging.LogRecord`."""

    def filter(self, record: logging.LogRecord) -> bool:
        secrets = _all_secrets(record)
        if not secrets:
            return True
        if isinstance(record.msg, str):
            record.msg = _scrub_text(record.msg, secrets)
        if record.args:
            record.args = _scrub_args(record.args, secrets)
        if record.exc_info:
            record.exc_text = _scrub_text(
                logging.Formatter().formatException(record.exc_info), secrets
            )
        return True


def install_safe_logging(level: int = logging.INFO) -> None:
    """Attach the redaction filter to the root logger's handlers (idempotent)."""
    root = logging.getLogger()
    root.setLevel(level)
    if not root.handlers:
        root.addHandler(logging.StreamHandler())
    for handler in root.handlers:
        if not any(isinstance(f, SecretRedactionFilter) for f in handler.filters):
            handler.addFilter(SecretRedactionFilter())
