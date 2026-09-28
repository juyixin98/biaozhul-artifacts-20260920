"""Pydantic request schemas (kept separate from response serialization)."""
from __future__ import annotations

from typing import Any
from pydantic import BaseModel, Field


class OntologyRequest(BaseModel):
    # Axiom bodies are validated by the restricted language parser, which can
    # point at the exact offending JSON path; Pydantic only enforces shape.
    axioms: list[dict[str, Any]] = Field(
        ..., description="Restricted OWL axioms (sub/equivalent/disjoint/instance)"
    )


class SubclassRequest(BaseModel):
    sub: str = Field(..., min_length=1)
    super: str = Field(..., min_length=1)
    axioms: list[dict[str, Any]]
