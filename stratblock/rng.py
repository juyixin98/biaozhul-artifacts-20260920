"""Random stream derivation and the allocation kernel.

Random stream spec (``stratblock-deterministic-rng-v1``)
--------------------------------------------------------
The RNG is deliberately *not* opaque NumPy state. It is a short,
specifiable construction so an independent implementation
(``reference/pbr.py``) can reproduce every bit:

1. ``stream_key = HKDF-SHA256(salt, ikm, info)`` per RFC 5869 with
   - ``salt``  = ``b"stratblock/stream-key/v1"``
   - ``ikm``   = master seed as 8-byte unsigned big-endian
   - ``info``  = ``study_id + NUL + stratum_key + NUL + "v1"``
2. Blocks are ``SHA256(stream_key || counter_64_be)``; each 32-byte block
   yields eight 32-bit big-endian words, consumed low-word-first.
3. ``uniform(bound)`` is rejection sampling on the 32-bit words
   (``x < 2**32 - 2**32 % bound``, then ``x % bound``).
4. Permutations are Fisher-Yates using that ``uniform``.

Consumption order for a new block is fixed: one word-sequence draw for the
block size, then (permuted policy) one Fisher-Yates shuffle of the arm
multiset. The balanced-prefix policy instead consumes one
``uniform(len(eligible))`` per allocation.
"""
from __future__ import annotations

import hashlib
import hmac
from dataclasses import dataclass
from typing import Any

from .contract import (
    STREAM_SCHEME,
    BlockDraw,
    StudyConfig,
    TailPolicy,
    hamilton_counts,
)

STREAM_SALT = b"stratblock/stream-key/v1"
STREAM_INFO_SUFFIX = b"\x00v1"
WORD_MASK = 0xFFFFFFFF
WORDS_PER_BLOCK = 8
MASTER_SEED_MAX = 1 << 64


def hkdf_sha256(ikm: bytes, salt: bytes, info: bytes, length: int = 32) -> bytes:
    """RFC 5869 HKDF-SHA256 (extract-and-expand)."""
    prk = hmac.new(salt, ikm, hashlib.sha256).digest()
    okm = b""
    t = b""
    for i in range(1, (length + 31) // 32 + 1):
        t = hmac.new(prk, t + info + bytes([i]), hashlib.sha256).digest()
        okm += t
    return okm[:length]


def derive_stream_key(master_seed: int, study_id: str, stratum_key: str) -> bytes:
    """Deterministic 32-byte per-(study, stratum) stream key."""
    if not 0 <= master_seed < MASTER_SEED_MAX:
        raise ValueError("master_seed must fit in unsigned 64 bits")
    ikm = master_seed.to_bytes(8, "big")
    info = study_id.encode("utf-8") + b"\x00" + stratum_key.encode("utf-8") + STREAM_INFO_SUFFIX
    return hkdf_sha256(ikm, STREAM_SALT, info, 32)


def stream_identifier(study_id: str, stratum_key: str) -> str:
    return f"{STREAM_SCHEME}:study={study_id};stratum={stratum_key}"


@dataclass(frozen=True)
class RngState:
    """Serializable position in the counter stream."""

    counter: int = 0
    word: int = 0

    def to_dict(self) -> dict[str, int]:
        return {"counter": self.counter, "word": self.word}

    @staticmethod
    def from_dict(data: dict[str, int]) -> "RngState":
        return RngState(counter=int(data["counter"]), word=int(data["word"]))


class CounterStream:
    """SHA256 counter PRNG implementing the documented spec.

    State invariant: ``counter`` is the index of the block currently
    buffered and ``word`` is the offset of the next word inside it. This is
    stable under serialisation: a stream rehydrated from ``(counter, word)``
    reads exactly the word a long-running stream would read next.
    """

    def __init__(self, key: bytes, state: RngState | None = None) -> None:
        self.key = key
        self.counter = state.counter if state else 0
        self.word = state.word if state else 0
        self._buffer = hashlib.sha256(
            self.key + self.counter.to_bytes(8, "big")
        ).digest()

    def rng_state(self) -> RngState:
        return RngState(counter=self.counter, word=self.word)

    def _next_word(self) -> int:
        if self.word >= WORDS_PER_BLOCK:
            self.counter += 1
            self.word = 0
            self._buffer = hashlib.sha256(
                self.key + self.counter.to_bytes(8, "big")
            ).digest()
        start = self.word * 4
        value = int.from_bytes(self._buffer[start : start + 4], "big")
        self.word += 1
        return value

    def uniform(self, bound: int) -> int:
        """Uniform integer in ``[0, bound)`` via rejection sampling.

        Note: ``bound == 1`` still *consumes one word*. The short-circuit
        ``return 0`` would silently change every later draw in the stream
        (e.g. studies with a single allowed block size); the reference
        oracle relies on this consumption being identical.
        """
        if bound <= 0:
            raise ValueError("bound must be positive")
        limit = 2**32 - (2**32 % bound)
        while True:
            word = self._next_word()
            if word < limit:
                return word % bound

    def shuffled(self, values: list[int]) -> list[int]:
        """Fisher-Yates shuffle of ``values`` (returns a new list)."""
        result = list(values)
        for i in range(len(result) - 1, 0, -1):
            j = self.uniform(i + 1)
            result[i], result[j] = result[j], result[i]
        return result


@dataclass(frozen=True)
class StratumRuntimeState:
    """Everything needed to continue a stratum's stream after restart.

    For the permuted policy ``permutation`` holds the current block's arm
    indices and ``used`` is ``None``. For the balanced-prefix policy
    ``permutation`` is ``None`` and ``used`` holds realised per-arm counts
    inside the open block.
    """

    block_index: int
    position: int
    block_size: int
    rng: RngState
    sequence_index: int
    sealed: bool = False
    permutation: tuple[int, ...] | None = None
    used: tuple[int, ...] | None = None

    def to_dict(self) -> dict[str, Any]:
        return {
            "block_index": self.block_index,
            "position": self.position,
            "block_size": self.block_size,
            "rng": self.rng.to_dict(),
            "sequence_index": self.sequence_index,
            "sealed": self.sealed,
            "permutation": list(self.permutation) if self.permutation is not None else None,
            "used": list(self.used) if self.used is not None else None,
        }

    @staticmethod
    def from_dict(data: dict[str, Any]) -> "StratumRuntimeState":
        return StratumRuntimeState(
            block_index=int(data["block_index"]),
            position=int(data["position"]),
            block_size=int(data["block_size"]),
            rng=RngState.from_dict(data["rng"]),
            sequence_index=int(data["sequence_index"]),
            sealed=bool(data["sealed"]),
            permutation=tuple(data["permutation"]) if data["permutation"] is not None else None,
            used=tuple(data["used"]) if data["used"] is not None else None,
        )


def initial_state() -> StratumRuntimeState:
    return StratumRuntimeState(
        block_index=0,
        position=0,
        block_size=0,
        rng=RngState(),
        sequence_index=0,
    )


def _open_block(
    cfg: StudyConfig, rng: CounterStream, block_index: int
) -> tuple[int, tuple[int, ...] | None, tuple[int, ...] | None, dict[str, Any]]:
    """Draw a new block size and (permuted policy) its permutation."""
    size_index = rng.uniform(len(cfg.block_sizes))
    block_size = cfg.block_sizes[size_index]
    plan = cfg.plan_counts(block_size)
    detail: dict[str, Any] = {
        "block_size_draw": {
            "allowed": list(cfg.block_sizes),
            "selected_index": size_index,
            "selected_size": block_size,
        },
        "plan_counts": list(plan),
    }
    if cfg.tail_policy is TailPolicy.PERMUTED:
        multiset: list[int] = []
        for arm_index, count in enumerate(plan):
            multiset.extend([arm_index] * count)
        order = rng.shuffled(list(range(block_size)))
        permutation = tuple(multiset[p] for p in order)
        detail["shuffle_order"] = order
        detail["permutation"] = list(permutation)
        return block_size, permutation, None, detail
    detail["eligible_paths"] = "hamilton_prefix_constraint"
    return block_size, None, tuple(0 for _ in cfg.arms), detail


def draw_next(
    cfg: StudyConfig,
    stream_key: bytes,
    state: StratumRuntimeState,
) -> tuple[BlockDraw, StratumRuntimeState]:
    """Produce the next allocation and the advanced state.

    Pure function: no I/O, no hidden global state. Raises ``ValueError``
    only on a sealed stratum (the service layer maps that to a domain
    error).
    """
    if state.sealed:
        raise ValueError("stratum is sealed")

    rng = CounterStream(stream_key, state.rng)
    block_index = state.block_index
    position = state.position
    block_size = state.block_size
    permutation = state.permutation
    used = state.used
    detail: dict[str, Any] = {}

    starting_block = position == 0 or position >= block_size
    if starting_block:
        block_size, permutation, used, detail = _open_block(cfg, rng, block_index)

    plan_counts = cfg.plan_counts(block_size)
    tail = False

    if cfg.tail_policy is TailPolicy.PERMUTED:
        assert permutation is not None
        arm_index = permutation[position]
        detail.setdefault(
            "block_size_draw",
            {
                "allowed": list(cfg.block_sizes),
                "selected_size": block_size,
            },
        )
        detail["position_arm"] = arm_index
        next_position = position + 1
        next_used = None
    else:
        assert used is not None
        used_list = list(used)
        prefix_targets = hamilton_counts(position + 1, cfg.allocation_ratio)
        eligible = [i for i in range(len(cfg.arms)) if used_list[i] < prefix_targets[i]]
        choice = rng.uniform(len(eligible))
        arm_index = eligible[choice]
        used_list[arm_index] += 1
        detail.setdefault(
            "block_size_draw",
            {"allowed": list(cfg.block_sizes), "selected_size": block_size},
        )
        detail["prefix_n"] = position + 1
        detail["prefix_targets"] = list(prefix_targets)
        detail["eligible_arms"] = eligible
        detail["selected_eligible_index"] = choice
        next_position = position + 1
        next_used = tuple(used_list)

    closes_block = next_position >= block_size
    if closes_block:
        next_block_index = block_index + 1
        next_position = 0
        next_block_size = 0
        next_permutation = None
        next_used = None
    else:
        next_block_index = block_index
        next_block_size = block_size
        next_permutation = permutation

    sequence_index = state.sequence_index + 1
    new_state = StratumRuntimeState(
        block_index=next_block_index,
        position=next_position,
        block_size=next_block_size,
        rng=rng.rng_state(),
        sequence_index=sequence_index,
        permutation=next_permutation,
        used=next_used,
    )
    draw = BlockDraw(
        arm_index=arm_index,
        block_index=block_index,
        position_in_block=position,
        block_size=block_size,
        plan_counts=plan_counts,
        tail=tail,
        policy=cfg.tail_policy,
        stream_id="",
        stream_seed=0,
        sequence_index=sequence_index - 1,
        rng_detail=detail,
    )
    return draw, new_state


def expected_tail_report(
    cfg: StudyConfig, realised_counts: tuple[int, ...], n_allocated: int
) -> dict[str, Any]:
    """Explain a sealed (possibly incomplete) block.

    The reported target is one largest-remainder (Hamilton) apportionment;
    for the permuted policy the symmetric acceptance rule is
    :func:`stratblock.contract.prefix_is_balanced`, which admits every
    tie-break. Deviations outside the feasible set are reported, not
    hidden.
    """
    from .contract import prefix_is_balanced

    target = hamilton_counts(n_allocated, cfg.allocation_ratio)
    deviation = tuple(realised_counts[i] - target[i] for i in range(len(cfg.arms)))
    if cfg.tail_policy is TailPolicy.BALANCED_PREFIX:
        balanced = realised_counts == target
        criterion = "deterministic_hamilton_target"
    else:
        balanced = prefix_is_balanced(
            realised_counts, n_allocated, cfg.allocation_ratio
        )
        criterion = "feasible_apportionment_set"
    return {
        "policy": cfg.tail_policy.value,
        "balance_criterion": criterion,
        "allocated_in_open_block": n_allocated,
        "realised_counts": list(realised_counts),
        "on_ratio_target_for_prefix": list(target),
        "deviation": list(deviation),
        "balanced": balanced,
        "flag": None if balanced else "tail_imbalance",
    }
