"""Statistical contract.

This module is the single source of truth for what a valid study is and
what a correct allocation must satisfy. It contains no I/O and no random
number generation, so both the production kernel (:mod:`stratblock.kernel`)
and the independent reference oracle (:mod:`reference.pbr`) can be checked
against the same rules.

Key decisions frozen here
-------------------------
* Stratification factors are categorical; feature values are strings
  (``1`` and ``"1"`` are never silently treated as the same level).
* Within every *complete* block, arm counts follow the declared
  allocation ratio exactly (``count_i = block_size * ratio_i / sum(ratio)``
  must be integral for every allowed block size).
* Two explicit incomplete-tail policies exist
  (:class:`TailPolicy`); neither ever silently reshuffles.
"""
from __future__ import annotations

import json
from dataclasses import dataclass, field
from enum import Enum
from typing import Any

SERVICE_VERSION = "1.0.0"
STREAM_SCHEME = "stratblock-stream-v1"
HKDF_INFO_VERSION = "v1"

MAX_ARMS = 8
MAX_FACTORS = 6
MAX_LEVEL_LEN = 64
MAX_ID_LEN = 128


class TailPolicy(str, Enum):
    """How an open block behaves if the stratum is sealed before it fills.

    PERMUTED
        Classic permuted blocks: the whole block is shuffled up front and
        consumed in order. Any sealing point is a *realised random prefix*.
        The seal report states the realised counts and explicitly flags a
        proportional deviation as ``tail_imbalance`` — it is never hidden.

    BALANCED_PREFIX
        Constrained random allocation inside the block: at position ``n``
        only arms that have not already reached their Hamilton-apportioned
        target for ``n`` allocations are eligible. Every possible sealing
        point is therefore exactly on-ratio, at the documented cost of
        reduced randomness near block boundaries.
    """

    PERMUTED = "permuted"
    BALANCED_PREFIX = "balanced_prefix"


class ErrorCategory(str, Enum):
    """Stable failure categories. Callers may branch on these strings."""

    VALIDATION_ERROR = "VALIDATION_ERROR"
    UNKNOWN_STUDY = "UNKNOWN_STUDY"
    UNKNOWN_SUBJECT = "UNKNOWN_SUBJECT"
    DUPLICATE_CONFLICT = "DUPLICATE_CONFLICT"
    STRATUM_CLOSED = "STRATUM_CLOSED"
    FORBIDDEN = "FORBIDDEN"
    UNAUTHENTICATED = "UNAUTHENTICATED"
    REPLAY_MISMATCH = "REPLAY_MISMATCH"
    INDETERMINATE = "INDETERMINATE"
    CONFLICT = "CONFLICT"


# Mismatch sub-categories emitted by the replay / diagnostics layer.
class MismatchKind(str, Enum):
    STREAM_SEED_MISMATCH = "STREAM_SEED_MISMATCH"
    BLOCK_SIZE_MISMATCH = "BLOCK_SIZE_MISMATCH"
    SEQUENCE_MISMATCH = "SEQUENCE_MISMATCH"
    POSITION_GAP = "POSITION_GAP"
    COUNT_PLAN_VIOLATION = "COUNT_PLAN_VIOLATION"
    TAIL_BALANCE_VIOLATION = "TAIL_BALANCE_VIOLATION"
    EVENT_REORDER = "EVENT_REORDER"


class AllocationError(Exception):
    """Domain error carrying a stable :class:`ErrorCategory`."""

    def __init__(
        self,
        category: ErrorCategory,
        message: str,
        details: dict[str, Any] | None = None,
        http_status: int = 400,
    ) -> None:
        super().__init__(message)
        self.category = category
        self.message = message
        self.details = details or {}
        self.http_status = http_status


@dataclass(frozen=True)
class StudyConfig:
    """Frozen study specification.

    Stored as JSON when a study is registered; re-registration is refused,
    so the contract under which subjects were allocated can never change.
    """

    study_id: str
    arms: tuple[str, ...]
    stratification_factors: tuple[str, ...]
    block_sizes: tuple[int, ...]
    allocation_ratio: tuple[int, ...]
    tail_policy: TailPolicy = TailPolicy.PERMUTED

    def ratio_sum(self) -> int:
        return sum(self.allocation_ratio)

    def arm_index(self, arm: str) -> int:
        return self.arms.index(arm)

    def plan_counts(self, block_size: int) -> tuple[int, ...]:
        """Exact per-arm counts for a complete block of this size."""
        rsum = self.ratio_sum()
        return tuple(block_size * r // rsum for r in self.allocation_ratio)

    def to_json(self) -> str:
        return json.dumps(
            {
                "study_id": self.study_id,
                "arms": list(self.arms),
                "stratification_factors": list(self.stratification_factors),
                "block_sizes": list(self.block_sizes),
                "allocation_ratio": list(self.allocation_ratio),
                "tail_policy": self.tail_policy.value,
                "stream_scheme": STREAM_SCHEME,
                "contract_version": SERVICE_VERSION,
            },
            sort_keys=True,
        )

    @staticmethod
    def from_json(raw: str) -> "StudyConfig":
        data = json.loads(raw)
        return StudyConfig(
            study_id=data["study_id"],
            arms=tuple(data["arms"]),
            stratification_factors=tuple(data["stratification_factors"]),
            block_sizes=tuple(data["block_sizes"]),
            allocation_ratio=tuple(data["allocation_ratio"]),
            tail_policy=TailPolicy(data["tail_policy"]),
        )


@dataclass(frozen=True)
class BlockDraw:
    """One allocation decision, fully explained."""

    arm_index: int
    block_index: int
    position_in_block: int  # 0-based
    block_size: int
    plan_counts: tuple[int, ...]
    tail: bool
    policy: TailPolicy
    # Provenance of the randomness consumed for this draw.
    stream_id: str
    stream_seed: int
    sequence_index: int  # global draw index within the stratum
    # RNG transparency: block-size draw and full permutation are recorded
    # when a new (permuted) block opens, so replay can re-derive them.
    rng_detail: dict[str, Any] = field(default_factory=dict)


def hamilton_counts(n: int, ratio: tuple[int, ...]) -> tuple[int, ...]:
    """Largest-remainder apportionment of ``n`` across arms.

    Deterministic tie-break: leftover seats go to the lowest arm index.
    Returns a tuple that always sums to ``n`` and stays as close as
    possible to the exact ratio shares.

    Note: at exact ``x.5`` ties this is one arbitrary (but fixed) ordering
    convention. Diagnostics therefore use the symmetric
    :func:`prefix_is_balanced`, which accepts *every* largest-remainder
    apportionment, not just the lowest-index tie-break.
    """
    if n < 0:
        raise ValueError("n must be non-negative")
    total = sum(ratio)
    exact = [n * r / total for r in ratio]
    floors = [int(x) for x in exact]  # truncation == floor for non-negative
    remainder = n - sum(floors)
    order = sorted(
        range(len(ratio)),
        key=lambda i: (-(exact[i] - floors[i]), i),
    )
    for k in range(remainder):
        floors[order[k]] += 1
    return tuple(floors)


def prefix_is_balanced(
    realised: tuple[int, ...], n: int, ratio: tuple[int, ...]
) -> bool:
    """Symmetric on-ratio test for an incomplete prefix.

    A count vector is balanced iff every arm count is one of
    ``floor(n*r_i/R)`` or ``ceil(n*r_i/R)`` and the vector sums to ``n``.
    That set is exactly the collection of valid largest-remainder
    apportionments, so for a 1:1 prefix of length 3 both (2,1) and (1,2)
    are accepted, while (3,0) and (0,3) are rejected.
    """
    if sum(realised) != n:
        return False
    import math

    total = sum(ratio)
    for count, r in zip(realised, ratio):
        share = n * r / total
        if count not in (math.floor(share), math.ceil(share)):
            return False
    return True


def canonical_stratum_key(
    factors: tuple[str, ...], features: dict[str, str]
) -> str:
    """Deterministic stratum identity, e.g. ``age=old|site=A``."""
    return "|".join(f"{f}={features[f]}" for f in factors)


def validate_features(
    cfg: StudyConfig, features: dict[str, Any]
) -> dict[str, str]:
    """Validate enrollment features against the frozen factors.

    Only string levels are accepted; missing/extra factors and non-string
    values are rejected up front with explicit details.
    """
    if not isinstance(features, dict):
        raise AllocationError(
            ErrorCategory.VALIDATION_ERROR,
            "features must be an object mapping factor names to string levels",
            {"got_type": type(features).__name__},
            http_status=422,
        )
    got = set(features)
    expected = set(cfg.stratification_factors)
    if got != expected:
        raise AllocationError(
            ErrorCategory.VALIDATION_ERROR,
            "feature keys must match the frozen stratification factors exactly",
            {
                "missing": sorted(expected - got),
                "unexpected": sorted(got - expected),
                "expected": list(cfg.stratification_factors),
            },
            http_status=422,
        )
    normalized: dict[str, str] = {}
    for f in cfg.stratification_factors:
        v = features[f]
        if not isinstance(v, str) or not v:
            raise AllocationError(
                ErrorCategory.VALIDATION_ERROR,
                f"level for factor {f!r} must be a non-empty string",
                {"factor": f, "got": v},
                http_status=422,
            )
        if len(v) > MAX_LEVEL_LEN:
            raise AllocationError(
                ErrorCategory.VALIDATION_ERROR,
                f"level for factor {f!r} exceeds {MAX_LEVEL_LEN} characters",
                {"factor": f, "length": len(v)},
                http_status=422,
            )
        normalized[f] = v
    return normalized


def build_study_config(
    study_id: str,
    arms: list[str],
    stratification_factors: list[str],
    block_sizes: list[int],
    allocation_ratio: list[int] | None = None,
    tail_policy: TailPolicy | str = TailPolicy.PERMUTED,
) -> StudyConfig:
    """Validate raw registration input and produce a frozen config.

    All rejection reasons are raised as :class:`AllocationError` with
    category :data:`ErrorCategory.VALIDATION_ERROR` and explicit details.
    """
    problems: list[str] = []

    if not isinstance(study_id, str) or not study_id.strip():
        problems.append("study_id must be a non-empty string")
    elif len(study_id) > MAX_ID_LEN:
        problems.append(f"study_id exceeds {MAX_ID_LEN} characters")

    if not isinstance(arms, list) or not (2 <= len(arms) <= MAX_ARMS):
        problems.append(f"arms must be a list of 2..{MAX_ARMS} entries")
        arms = []
    if len(set(arms)) != len(arms):
        problems.append("arm labels must be unique")
    if any((not isinstance(a, str)) or not a.strip() for a in arms):
        problems.append("arm labels must be non-empty strings")

    if (
        not isinstance(stratification_factors, list)
        or not (1 <= len(stratification_factors) <= MAX_FACTORS)
    ):
        problems.append(
            f"stratification_factors must list 1..{MAX_FACTORS} entries"
        )
        stratification_factors = []
    if len(set(stratification_factors)) != len(stratification_factors):
        problems.append("stratification factor names must be unique")
    if any(not isinstance(f, str) or not f.strip() for f in stratification_factors):
        problems.append("stratification factor names must be non-empty strings")

    ratio = allocation_ratio or [1] * len(arms)
    if (
        not isinstance(ratio, list)
        or len(ratio) != len(arms)
        or any(not isinstance(r, int) or isinstance(r, bool) or r < 1 for r in ratio)
    ):
        problems.append(
            "allocation_ratio must be positive integers, one entry per arm"
        )
        ratio = [1] * len(arms)

    if not isinstance(block_sizes, list) or not block_sizes:
        problems.append("block_sizes must be a non-empty list")
        block_sizes = []
    if any(not isinstance(b, int) or isinstance(b, bool) or b < 2 for b in block_sizes):
        problems.append("block sizes must be integers >= 2")
    if len(set(block_sizes)) != len(block_sizes):
        problems.append("block sizes must not repeat")

    rsum = sum(ratio)
    for b in sorted(set(block_sizes)):
        if isinstance(b, int) and b >= 2:
            if any(b * r_arm % rsum != 0 for r_arm in ratio):
                problems.append(
                    f"block size {b} cannot realise ratio {ratio} exactly: "
                    f"{b}*ratio_i/{rsum} is not integral for some arm"
                )

    if isinstance(tail_policy, str):
        try:
            policy = TailPolicy(tail_policy)
        except ValueError:
            problems.append(
                f"tail_policy must be one of {[p.value for p in TailPolicy]}"
            )
            policy = TailPolicy.PERMUTED
    else:
        policy = tail_policy

    if problems:
        raise AllocationError(
            ErrorCategory.VALIDATION_ERROR,
            "study specification is invalid",
            {"problems": problems},
            http_status=422,
        )

    return StudyConfig(
        study_id=study_id.strip(),
        arms=tuple(arms),
        stratification_factors=tuple(stratification_factors),
        block_sizes=tuple(sorted(block_sizes)),
        allocation_ratio=tuple(ratio),
        tail_policy=policy,
    )
