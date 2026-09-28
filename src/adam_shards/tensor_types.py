"""Tensor types: parameter identity, dense tensors, serialization, content hashing.

Parameter identity is a *stable name plus shape*, never a traversal index:
parameters can be reordered between save and restore and must still line up.
"""
from __future__ import annotations

import hashlib
from dataclasses import dataclass

import numpy as np

# Wire/storage dtype used inside checkpoint shards.
STORAGE_DTYPE = np.dtype(np.float64)

# Named categories of tensor state that travel together per parameter.
PARAM = "param"
MOMENT1 = "moment1"
MOMENT2 = "moment2"
STATE_KINDS = (PARAM, MOMENT1, MOMENT2)


@dataclass(frozen=True)
class ParamId:
    """Stable identity of a parameter.

    ``name`` is a qualified, order-independent string (e.g. ``layers.1.weight``)
    and ``shape`` is part of the identity: a checkpoint that supplies a tensor
    of a different shape for the same name is corrupt and must be rejected.
    """

    name: str
    shape: tuple[int, ...]

    def __post_init__(self) -> None:
        if not isinstance(self.name, str) or not self.name:
            raise ValueError("ParamId.name must be a non-empty string")
        shape = tuple(int(d) for d in self.shape)
        if any(d <= 0 for d in shape):
            raise ValueError(f"ParamId {self.name!r} has non-positive shape {shape}")
        object.__setattr__(self, "shape", shape)

    def to_dict(self) -> dict:
        return {"name": self.name, "shape": list(self.shape)}

    @classmethod
    def from_dict(cls, d: dict) -> "ParamId":
        return cls(name=str(d["name"]), shape=tuple(d["shape"]))

    def __str__(self) -> str:
        return f"{self.name}{tuple(self.shape)}"


@dataclass(frozen=True)
class Tensor:
    """A dense array tagged with the parameter it belongs to."""

    param: ParamId
    kind: str
    data: np.ndarray

    def __post_init__(self) -> None:
        if self.kind not in STATE_KINDS:
            raise ValueError(f"unknown tensor kind {self.kind!r}")
        arr = np.asarray(self.data, dtype=STORAGE_DTYPE)
        if arr.shape != self.param.shape:
            raise ValueError(
                f"shape mismatch for {self.param.name}.{self.kind}: "
                f"declared {self.param.shape}, got {arr.shape}"
            )
        # Defensive copy + non-writeable view keeps tensors immutable.
        arr = arr.copy(order="C")
        arr.setflags(write=False)
        object.__setattr__(self, "data", arr)

    def to_bytes(self) -> bytes:
        return np.ascontiguousarray(self.data, dtype=STORAGE_DTYPE).tobytes()

    @classmethod
    def from_bytes(cls, param: ParamId, kind: str, raw: bytes) -> "Tensor":
        expected = int(np.prod(param.shape))
        got = len(raw) // STORAGE_DTYPE.itemsize
        if len(raw) != expected * STORAGE_DTYPE.itemsize:
            raise ValueError(
                f"byte length mismatch for {param.name}.{kind}: "
                f"expected {expected} elements, found {got}"
            )
        arr = np.frombuffer(raw, dtype=STORAGE_DTYPE, count=expected).reshape(param.shape)
        return cls(param=param, kind=kind, data=arr)


def tensor_digest(t: Tensor) -> str:
    """SHA-256 over (param name, kind, shape, dtype, raw bytes).

    The identity is folded in so a tensor cannot be silently swapped between
    parameters even if its values happen to match.
    """
    h = hashlib.sha256()
    h.update(t.param.name.encode("utf-8"))
    h.update(b"|")
    h.update(t.kind.encode("ascii"))
    h.update(b"|")
    h.update(np.asarray(t.param.shape, dtype=np.int64).tobytes())
    h.update(b"|")
    h.update(STORAGE_DTYPE.str.encode("ascii"))
    h.update(b"|")
    h.update(t.to_bytes())
    return h.hexdigest()


def array_fingerprint(arr: np.ndarray) -> str:
    """Content hash of a bare array (used for independent reference answers)."""
    a = np.ascontiguousarray(arr, dtype=STORAGE_DTYPE)
    return hashlib.sha256(a.tobytes()).hexdigest()
