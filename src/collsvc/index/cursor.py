"""Opaque, version-bound continuation cursors.

A cursor encodes:

* the index version it was issued for — an old cursor can never be resumed on
  a rebuilt index (the store rejects the mismatch categorically);
* the last seen sort key + ``seq`` (stable keyset-pagination position);
* a mode tag so a prefix cursor cannot be replayed on a value-range query.

The token is base64url over JSON — opaque to clients, tamper-evident enough
for a local service (corruption yields :class:`InvalidCursor`).
"""
from __future__ import annotations

import base64
import json
from dataclasses import dataclass

from ..collation.versioning import InvalidCursor

MODE_LIST = "list"
MODE_RANGE = "range"
MODE_PREFIX = "prefix"
VALID_MODES = frozenset({MODE_LIST, MODE_RANGE, MODE_PREFIX})


@dataclass(frozen=True)
class Cursor:
    index_version: str
    mode: str
    last_sort_key: bytes
    last_seq: int

    def __post_init__(self) -> None:
        if self.mode not in VALID_MODES:
            raise InvalidCursor(f"unknown cursor mode {self.mode!r}")
        if not isinstance(self.last_sort_key, bytes):
            raise InvalidCursor("cursor position must carry a sort key")
        if self.last_seq < 0:
            raise InvalidCursor("cursor seq must be non-negative")

    def encode(self) -> str:
        raw = json.dumps(
            {
                "v": 1,
                "idx": self.index_version,
                "m": self.mode,
                "k": base64.urlsafe_b64encode(self.last_sort_key).decode("ascii"),
                "s": self.last_seq,
            },
            separators=(",", ":"),
        ).encode("utf-8")
        return base64.urlsafe_b64encode(raw).decode("ascii").rstrip("=")

    @staticmethod
    def decode(token: str) -> "Cursor":
        try:
            padded = token + "=" * (-len(token) % 4)
            raw = json.loads(base64.urlsafe_b64decode(padded.encode("ascii")))
            return Cursor(
                index_version=raw["idx"],
                mode=raw["m"],
                last_sort_key=base64.urlsafe_b64decode(raw["k"].encode("ascii")),
                last_seq=int(raw["s"]),
            )
        except (KeyError, ValueError, TypeError) as exc:
            raise InvalidCursor(f"malformed cursor token: {exc}") from exc
