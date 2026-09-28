"""Plan cache for prepared Toeplitz operators.

Repeated calls with the same kernel reuse the FFT of the circulant first
column. The cache key is a SHA-256 digest bound to the *full* problem
description: mode, dtypes, embedding length, and the exact bytes of the
canonicalized first column and first row. Any change to sizes or values
produces a different key, so a stale plan can never be reused.
"""

from __future__ import annotations

import hashlib
import struct
from collections import OrderedDict
from typing import Any

import numpy as np


def kernel_digest(
    c: np.ndarray,
    r: np.ndarray,
    *,
    mode: str,
    L: int,
    real_dtype: str,
    complex_dtype: str,
) -> str:
    """Content-addressed digest of a prepared operator's full identity."""
    h = hashlib.sha256()
    h.update(mode.encode("utf-8"))
    h.update(real_dtype.encode("utf-8"))
    h.update(complex_dtype.encode("utf-8"))
    h.update(struct.pack("<qqq", c.shape[0], r.shape[0], L))
    h.update(np.ascontiguousarray(c).tobytes())
    h.update(np.ascontiguousarray(r).tobytes())
    return h.hexdigest()


class PlanCache:
    """Bounded LRU cache mapping kernel digests to prepared operators."""

    def __init__(self, maxsize: int = 64) -> None:
        if maxsize < 1:
            raise ValueError("maxsize must be >= 1")
        self._maxsize = maxsize
        self._store: OrderedDict[str, Any] = OrderedDict()
        self.hits = 0
        self.misses = 0

    @property
    def maxsize(self) -> int:
        return self._maxsize

    def get(self, key: str) -> tuple[Any, bool]:
        """Return (value, True) on hit, (None, False) on miss."""
        if key in self._store:
            self.hits += 1
            self._store.move_to_end(key)
            return self._store[key], True
        self.misses += 1
        return None, False

    def put(self, key: str, value: Any) -> None:
        self._store[key] = value
        self._store.move_to_end(key)
        while len(self._store) > self._maxsize:
            self._store.popitem(last=False)

    def stats(self) -> dict[str, int]:
        return {
            "size": len(self._store),
            "maxsize": self._maxsize,
            "hits": self.hits,
            "misses": self.misses,
        }

    def clear(self) -> None:
        self._store.clear()
        self.hits = 0
        self.misses = 0
