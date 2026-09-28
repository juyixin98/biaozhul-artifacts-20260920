"""HTTP request/response schemas.

Payloads are validated at the boundary. Responses never echo caller tensor
payloads back — only integer/verdict metadata — to keep the service safe to
log.
"""

from __future__ import annotations

from typing import Any

from pydantic import BaseModel, Field


class InferRequest(BaseModel):
    model_id: str = Field(..., min_length=1, max_length=128)
    model_version: str | None = Field(None, max_length=64)
    # Flat row-major values plus explicit shape: keeps JSON simple to validate.
    shape: list[int] = Field(..., min_length=2, max_length=2)
    values: list[float] = Field(..., min_length=1)
    request_id: str | None = Field(None, max_length=64)


class LayerResult(BaseModel):
    name: str
    output_shape: list[int]
    output_codes: list[list[int]]
    saturated_outputs: int
    dequantized: list[list[float]]


class InferResponse(BaseModel):
    request_id: str
    model_id: str
    model_version: str
    calibration_fingerprint: str
    verdict: str
    verification: dict[str, Any]
    layers: list[LayerResult]


class ModelInfo(BaseModel):
    model_id: str
    model_version: str
    calibration_fingerprint: str
    layer_order: list[str]
    state: str
