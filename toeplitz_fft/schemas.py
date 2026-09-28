"""Request/response schemas for the HTTP service.

Complex numbers on the wire are ``[re, im]`` pairs; plain numbers are real.
A request is complex iff any of its entries is a pair.
"""

from __future__ import annotations

from typing import Literal, Union

import numpy as np
from pydantic import BaseModel, Field

Scalar = Union[float, list[float]]  # 5.0  or  [re, im]


def _to_complex(value: Scalar) -> complex:
    if isinstance(value, (list, tuple)):
        if len(value) != 2:
            raise ValueError(f"complex entries must be [re, im] pairs, got {value!r}")
        return complex(value[0], value[1])
    return complex(value, 0.0)


def decode_vector(values: list[Scalar]) -> np.ndarray:
    """Decode a wire vector; complex128 if any entry is a pair, else float64."""
    if any(isinstance(v, (list, tuple)) for v in values):
        return np.array([_to_complex(v) for v in values], dtype=np.complex128)
    return np.array(values, dtype=np.float64)


def encode_vector(a: np.ndarray) -> list:
    """Inverse of decode_vector: pairs for complex, plain floats for real."""
    a = np.asarray(a)
    if np.iscomplexobj(a):
        return [[float(z.real), float(z.imag)] for z in a.ravel()]
    return [float(v) for v in a.ravel()]


class MatvecRequest(BaseModel):
    c: list[Scalar] = Field(..., min_length=1, description="first column, length m")
    r: list[Scalar] = Field(..., min_length=1, description="first row, length n")
    x: list[Scalar] = Field(..., min_length=1, description="input vector, length n")
    mode: Literal["auto", "real", "complex"] = "auto"


class MatmatRequest(BaseModel):
    c: list[Scalar] = Field(..., min_length=1)
    r: list[Scalar] = Field(..., min_length=1)
    # Batch of column vectors: X[j] is the j-th input vector, each of length n.
    X: list[list[Scalar]] = Field(..., min_length=1, description="list of k column vectors, each length n")
    mode: Literal["auto", "real", "complex"] = "auto"


class ResponseMeta(BaseModel):
    request_id: str
    m: int
    n: int
    L: int
    k: int = 1
    mode: str
    dtype: str
    kernel_digest: str
    cache_hit: bool


class MatvecResponse(BaseModel):
    y: list[Scalar]
    meta: ResponseMeta


class MatmatResponse(BaseModel):
    Y: list[list[Scalar]]  # list of k output column vectors, each length m
    meta: ResponseMeta
