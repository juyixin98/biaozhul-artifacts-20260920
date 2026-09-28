"""Pydantic request/response models for the HTTP API."""

from __future__ import annotations

from typing import Any

from pydantic import BaseModel, Field

__all__ = [
    "CreateOntology",
    "ReasoningResult",
    "ErrorBody",
]


class CreateOntology(BaseModel):
    ontology_id: str | None = Field(default=None, description="Stable id; generated when omitted")
    label: str | None = None
    # JSON-constructor axioms, e.g.
    # {"type": "SubClassOf", "sub": {"type": "Class", "name": "A"}, ...}
    axioms: list[dict[str, Any]] = Field(default_factory=list)
    # Alternatively (or additionally), functional-syntax text, one axiom per line.
    functional: str | None = None


class ErrorBody(BaseModel):
    error: dict[str, Any]
    request_id: str
