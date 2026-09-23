"""Typed errors for the contract language and checker."""

from __future__ import annotations

from typing import Any


class ContractError(ValueError):
    """A contract JSON document is malformed or violates the language rules."""

    def __init__(self, message: str, path: str | None = None) -> None:
        if path:
            message = f"{path}: {message}"
        super().__init__(message)
        self.path = path


class ReplayError(ValueError):
    """A solver trace cannot be replayed concretely (model/contract mismatch)."""


class UnsupportedOperator(ContractError):
    def __init__(self, op: str, path: str, allowed: tuple[str, ...]) -> None:
        super().__init__(
            f"unknown operator {op!r}; allowed operators: {', '.join(allowed)}",
            path,
        )


def type_error(expected: str, got: Any, path: str) -> ContractError:
    return ContractError(
        f"expected {expected}, got {type(got).__name__}", path
    )
