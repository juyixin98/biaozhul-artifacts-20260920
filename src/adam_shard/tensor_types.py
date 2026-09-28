"""Stable tensor identity, dtype handling and checksum helpers.

Parameter identity is the pair (stable name, shape) -- never a traversal
ordinal.  Every checkpoint artifact carries that identity so that a
reordered parameter set cannot silently alias the wrong Adam state.
"""

from __future__ import annotations

import hashlib
import json
from dataclasses import dataclass
from typing import Any

import numpy as np

SUPPORTED_DTYPES: dict[str, type] = {
    "float32": np.float32,
    "float64": np.float64,
}

# Canonical byte order used inside checkpoint payloads.
RAW_DTYPE = np.dtype(np.float64)


def resolve_dtype(name: str) -> np.dtype:
    try:
        return np.dtype(SUPPORTED_DTYPES[name])
    except KeyError as exc:  # pragma: no cover - trivial
        raise ValueError(f"unsupported dtype {name!r}; want one of {sorted(SUPPORTED_DTYPES)}") from exc


@dataclass(frozen=True)
class TensorId:
    """Stable identity of a parameter tensor."""

    name: str
    shape: tuple[int, ...]

    def __post_init__(self) -> None:
        if not self.name or not isinstance(self.name, str):
            raise ValueError("parameter name must be a non-empty string")
        if not self.shape or any(d <= 0 for d in self.shape):
            raise ValueError(f"parameter {self.name!r} has invalid shape {self.shape}")

    @property
    def numel(self) -> int:
        return int(np.prod(self.shape))

    def to_dict(self) -> dict[str, Any]:
        return {"name": self.name, "shape": list(self.shape)}

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> "TensorId":
        return cls(name=str(data["name"]), shape=tuple(int(d) for d in data["shape"]))

    def __str__(self) -> str:
        return f"{self.name}{tuple(self.shape)}"


def array_digest(arr: np.ndarray) -> str:
    """SHA-256 over the *canonical* bytes of an array (dtype + C-order values).

    Independent of the in-memory layout/strides the caller happens to hold.
    """

    flat = np.ascontiguousarray(arr, dtype=RAW_DTYPE).reshape(-1)
    h = hashlib.sha256()
    h.update(str(flat.size).encode("ascii"))
    h.update(b":")
    h.update(flat.tobytes())
    return h.hexdigest()


def json_digest(payload: Any) -> str:
    """Stable digest over JSON-serializable manifest metadata."""

    blob = json.dumps(payload, sort_keys=True, separators=(",", ":")).encode("utf-8")
    return hashlib.sha256(blob).hexdigest()
