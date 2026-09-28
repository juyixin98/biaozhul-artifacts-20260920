"""Evidence tests: independent oracle agreement + multi-seed distribution.

The reference oracle (``reference/pbr.py``) shares no code with the
production kernel. These tests therefore cross-validate two independent
implementations rather than letting the system under test grade itself.
"""
from __future__ import annotations

import pytest

from reference import pbr
from stratblock.contract import TailPolicy, build_study_config
from stratblock.rng import derive_stream_key, draw_next, initial_state

FIXED_SEEDS = (1, 2, 3, 7, 42, 99, 1234, 20260927)


def _kernel_sequence(cfg, master_seed: int, study_id: str, key: str, n: int) -> list[int]:
    state = initial_state()
    stream_key = derive_stream_key(master_seed, study_id, key)
    out: list[int] = []
    for _ in range(n):
        draw, state = draw_next(cfg, stream_key, state)
        out.append(draw.arm_index)
    return out


def _as_dict(cfg, master_seed: int) -> dict:
    return {
        "arms": list(cfg.arms),
        "block_sizes": list(cfg.block_sizes),
        "allocation_ratio": list(cfg.allocation_ratio),
        "tail_policy": cfg.tail_policy.value,
        "master_seed": master_seed,
        "study_id": cfg.study_id,
    }


@pytest.mark.evidence
@pytest.mark.parametrize("seed", FIXED_SEEDS)
@pytest.mark.parametrize("policy", [TailPolicy.PERMUTED, TailPolicy.BALANCED_PREFIX])
def test_kernel_agrees_with_reference_oracle(seed: int, policy: TailPolicy) -> None:
    cfg = build_study_config(
        f"oracle-{seed}-{policy.value}",
        ["C", "T1", "T2"], ["site", "wave"],
        block_sizes=[3, 6], allocation_ratio=[1, 1, 1], tail_policy=policy,
    )
    key = "site=Berlin|wave=2"
    n = 40
    kernel = _kernel_sequence(cfg, seed, cfg.study_id, key, n)
    oracle = pbr.arm_sequence(_as_dict(cfg, seed), key, n)
    assert kernel == oracle


@pytest.mark.evidence
def test_reference_diary_block_geometry_matches_kernel() -> None:
    cfg = build_study_config(
        "diary", ["C", "T"], ["s"], [2, 4, 6], [1, 1], TailPolicy.PERMUTED
    )
    seed, key = 555, "s=Z"
    state = initial_state()
    stream_key = derive_stream_key(seed, cfg.study_id, key)
    kernel_rows = []
    for i in range(20):
        draw, state = draw_next(cfg, stream_key, state)
        kernel_rows.append(
            {
                "block_index": draw.block_index,
                "position_in_block": draw.position_in_block,
                "block_size": draw.block_size,
                "arm_index": draw.arm_index,
                "sequence_index": i,
            }
        )
    oracle_rows = pbr.block_diary(_as_dict(cfg, seed), key, 20)
    assert kernel_rows == oracle_rows


@pytest.mark.evidence
def test_small_blocks_exact_ratio_enumeration() -> None:
    """Enumerate complete blocks over many seeds; counts must be exact."""
    cfg = build_study_config(
        "enum", ["C", "T"], ["s"], [2, 4], [1, 1], TailPolicy.PERMUTED
    )
    violations = []
    for seed in range(1, 121):
        seq = pbr.arm_sequence(_as_dict(cfg, seed), "s=Q", 40)
        # Walk blocks via the diary so the random block sizes are honoured.
        diary = pbr.block_diary(_as_dict(cfg, seed), "s=Q", 40)
        blocks: dict[int, list[int]] = {}
        for row, arm in zip(diary, seq):
            blocks.setdefault(row["block_index"], []).append(arm)
        for block_index, arms in blocks.items():
            size = next(
                r["block_size"] for r in diary if r["block_index"] == block_index
            )
            if len(arms) == size:  # complete blocks only
                counts = [arms.count(i) for i in range(2)]
                plan = [size // 2, size // 2]
                if counts != plan:
                    violations.append((seed, block_index, counts, plan))
    assert violations == []


@pytest.mark.evidence
def test_multi_seed_distribution_is_fair() -> None:
    """Pool many fixed-seed streams and test arm-frequency uniformity.

    Uses multiple *fixed* seeds (not an unreproducible random seed) and a
    chi-square goodness-of-fit. This checks marginal fairness; it is not
    used as proof of allocation correctness (mechanism agreement is).
    """
    cfg = build_study_config(
        "dist", ["C", "T"], ["s"], [4], [1, 1], TailPolicy.PERMUTED
    )
    pooled = [0, 0]
    per_stream_first_arm = []
    draws_per_stratum = 8
    for seed in FIXED_SEEDS:
        for level in range(20):
            seq = pbr.arm_sequence(_as_dict(cfg, seed), f"s={level}", draws_per_stratum)
            for arm in seq:
                pooled[arm] += 1
            per_stream_first_arm.append(seq[0])
    total = sum(pooled)
    expected = total / 2
    chi2 = sum((c - expected) ** 2 / expected for c in pooled)
    # df=1, 0.1% critical value ~10.83.
    assert chi2 < 10.83, pooled
    # First draws across streams should not be constant.
    assert set(per_stream_first_arm) == {0, 1}
    assert abs(pooled[0] - pooled[1]) / total < 0.05


@pytest.mark.evidence
def test_balanced_prefix_never_deviates_at_any_sealing_point() -> None:
    cfg = build_study_config(
        "bp", ["C", "T"], ["s"], [4, 8], [1, 1], TailPolicy.BALANCED_PREFIX
    )
    for seed in FIXED_SEEDS:
        seq = pbr.arm_sequence(_as_dict(cfg, seed), "s=1", 16)
        for n in range(1, 17):
            counts = [seq[:n].count(i) for i in range(2)]
            target = list(pbr._hamilton(n, (1, 1)))
            assert counts == target, (seed, n, counts, target)


@pytest.mark.evidence
def test_permuted_policy_can_show_tail_imbalance_and_it_is_bounded() -> None:
    """The permuted policy is free to deviate in an open tail, boundedly.

    Sealing a size-4 1:1 block after 2 allocations can only be balanced
    as (1,1); a realised (2,0)/(0,2) prefix cannot realise the ratio and
    must be disclosed. Across fixed seeds we must find such a case (the
    policy does not secretly constrain itself), and the deviation can
    never exceed one seat at that prefix length.
    """
    cfg = build_study_config(
        "tail", ["C", "T"], ["s"], [4], [1, 1], TailPolicy.PERMUTED
    )
    deviations = []
    balanced_cases = 0
    for seed in range(1, 300):
        seq = pbr.arm_sequence(_as_dict(cfg, seed), "s=1", 2)
        realised = (seq.count(0), seq.count(1))
        if realised == (1, 1):
            balanced_cases += 1
            continue
        gap = tuple(a - b for a, b in zip(realised, (1, 1)))
        deviations.append((seed, realised, gap))
    assert deviations, "permuted tails must be capable of (2,0)/(0,2)"
    assert balanced_cases > 0, "balanced prefixes must also occur"
    for seed, realised, gap in deviations:
        assert realised in {(2, 0), (0, 2)}
        assert tuple(abs(g) for g in gap) == (1, 1)
