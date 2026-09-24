"""Pydantic request/response models."""

from __future__ import annotations

from typing import Any

from pydantic import BaseModel, Field


class AppendRequest(BaseModel):
    actor: str = Field(..., min_length=1, max_length=512)
    action: str = Field(..., min_length=1, max_length=512)
    resource: str = Field("", max_length=4096)
    payload: Any = None


class VerifyRequest(BaseModel):
    """Stateless verification: the caller supplies everything, including the
    trust anchor, so this endpoint can serve as a reference verifier without
    the server ever learning which anchor a verifier trusts."""

    trust_anchor: str = Field(..., description="Ed25519 public key, raw hex")
    bundle: dict[str, Any]
    external_anchor: dict[str, Any] | None = None
    expect_tail: bool = True
