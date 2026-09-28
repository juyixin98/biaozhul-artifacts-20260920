"""Independent reference oracle (standard library only).

This is a *second* implementation of the stream/block specification
defined in ``stratblock/rng.py``. It deliberately:

* imports neither NumPy/SciPy nor any ``stratblock`` module;
* takes a plain ``dict`` config (JSON-shaped) rather than production
  dataclasses;
* is written top-down in a different style.

Its purpose is oracle cross-validation: production and reference must
agree, bit for bit, on the arm sequence of every stratum. Agreement of
two independent implementations plus hard-coded test vectors is much
stronger evidence than the core testifying about itself.

Expected config dict::

    {
      "arms": ["control", "treatment"],
      "block_sizes": [2, 4],
      "allocation_ratio": [1, 1],
      "tail_policy": "permuted" | "balanced_prefix",
      "master_seed": 20260927,
      "study_id": "demo"
    }
"""
from __future__ import annotations

import hashlib
import hmac
import itertools
from typing import Any

_SALT = b"stratblock/stream-key/v1"
_INFO_TAIL = b"\x00study-id\x00stratum\x00v1"  # structure documentation only
_TWO32 = 1 << 32


def _hkdf(ikm: bytes, salt: bytes, info: bytes, length: int) -> bytes:
    prk = hmac.new(salt, ikm, hashlib.sha256).digest()
    out = b""
    previous = b""
    counter = 1
    while len(out) < length:
        previous = hmac.new(
            prk, previous + info + bytes([counter]), hashlib.sha256
        ).digest()
        out += previous
        counter += 1
    return out[:length]


def stream_key(master_seed: int, study_id: str, stratum_key: str) -> bytes:
    info = study_id.encode() + b"\x00" + stratum_key.encode() + b"\x00v1"
    return _hkdf(master_seed.to_bytes(8, "big"), _SALT, info, 32)


class _Words:
    """SHA256-counter 32-bit big-endian word stream (independent re-code)."""

    def __init__(self, key: bytes) -> None:
        self._key = key
        self._block_number = 0
        self._block = b""
        self._cursor = 0

    def take(self) -> int:
        if self._cursor >= len(self._block):
            self._block = hashlib.sha256(
                self._key + self._block_number.to_bytes(8, "big")
            ).digest()
            self._block_number += 1
            self._cursor = 0
        chunk = self._block[self._cursor : self._cursor + 4]
        self._cursor += 4
        return int.from_bytes(chunk, "big")

    def below(self, modulus: int) -> int:
        highest = _TWO32 - (_TWO32 % modulus)
        while True:
            candidate = self.take()
            if candidate < highest:
                return candidate % modulus


def _fisher_yates(items: list[int], words: _Words) -> list[int]:
    for i in range(len(items) - 1, 0, -1):
        j = words.below(i + 1)
        items[i], items[j] = items[j], items[i]
    return items


def _hamilton(n: int, ratio: tuple[int, ...]) -> tuple[int, ...]:
    total = sum(ratio)
    raw = [n * r / total for r in ratio]
    seats = [int(x) for x in raw]
    left = n - sum(seats)
    ranked = sorted(range(len(ratio)), key=lambda i: (-(raw[i] - seats[i]), i))
    for k in range(left):
        seats[ranked[k]] += 1
    return tuple(seats)


def _plan(block_size: int, ratio: tuple[int, ...]) -> tuple[int, ...]:
    total = sum(ratio)
    counts = tuple(block_size * r // total for r in ratio)
    assert sum(counts) == block_size, "config validation should have prevented this"
    return counts


def arm_sequence(config: dict[str, Any], stratum_key: str, n_draws: int) -> list[int]:
    """Return the first ``n_draws`` arm indices for one stratum."""
    arms = config["arms"]
    sizes = sorted(config["block_sizes"])
    ratio = tuple(config.get("allocation_ratio", [1] * len(arms)))
    policy = config.get("tail_policy", "permuted")
    key = stream_key(int(config["master_seed"]), config["study_id"], stratum_key)
    words = _Words(key)

    produced: list[int] = []
    while len(produced) < n_draws:
        size = sizes[words.below(len(sizes))]
        plan = _plan(size, ratio)
        if policy == "permuted":
            bag: list[int] = []
            for arm_idx, how_many in enumerate(plan):
                bag.extend([arm_idx] * how_many)
            positions = _fisher_yates(list(range(size)), words)
            block_arms = [bag[p] for p in positions]
            produced.extend(block_arms)
        elif policy == "balanced_prefix":
            used = [0] * len(arms)
            for position in range(size):
                targets = _hamilton(position + 1, ratio)
                choices = [i for i in range(len(arms)) if used[i] < targets[i]]
                pick = choices[words.below(len(choices))]
                used[pick] += 1
                produced.append(pick)
        else:
            raise ValueError(f"unknown tail_policy: {policy!r}")
    return produced[:n_draws]


def block_diary(config: dict[str, Any], stratum_key: str, n_draws: int) -> list[dict[str, Any]]:
    """Per-draw diary including block size and position (for replay diffs)."""
    arms = config["arms"]
    sizes = sorted(config["block_sizes"])
    ratio = tuple(config.get("allocation_ratio", [1] * len(arms)))
    policy = config.get("tail_policy", "permuted")
    key = stream_key(int(config["master_seed"]), config["study_id"], stratum_key)
    words = _Words(key)

    diary: list[dict[str, Any]] = []
    block_index = 0
    while len(diary) < n_draws:
        size = sizes[words.below(len(sizes))]
        if policy == "permuted":
            plan = _plan(size, ratio)
            bag = list(
                itertools.chain.from_iterable(
                    [i] * c for i, c in enumerate(plan)
                )
            )
            positions = _fisher_yates(list(range(size)), words)
            block_arms = [bag[p] for p in positions]
        else:
            used = [0] * len(arms)
            block_arms = []
            for position in range(size):
                targets = _hamilton(position + 1, ratio)
                choices = [i for i in range(len(arms)) if used[i] < targets[i]]
                pick = choices[words.below(len(choices))]
                used[pick] += 1
                block_arms.append(pick)
        for position, arm_idx in enumerate(block_arms):
            diary.append(
                {
                    "block_index": block_index,
                    "position_in_block": position,
                    "block_size": size,
                    "arm_index": arm_idx,
                    "sequence_index": len(diary),
                }
            )
        block_index += 1
    return diary[:n_draws]


def key_fingerprint(master_seed: int, study_id: str, stratum_key: str) -> str:
    return stream_key(master_seed, study_id, stratum_key).hex()
