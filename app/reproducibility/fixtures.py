"""本地合成夹具。

全部数据都是合成的：研究定义、固定种子、登记对象队列（含分层特征）、
并发批次与"特征篡改"用例。夹具随代码版本固定，任何机器上跑出的
*期望序列*应完全一致。
"""
from __future__ import annotations

import base64
from dataclasses import dataclass, field

# 32 字节固定主种子（公开测试种子，非真实凭证）
FIXED_SEED_BYTES = b"rct-fixed-seed-v1-A".ljust(32, b"0")
assert len(FIXED_SEED_BYTES) == 32
FIXED_SEED_B64 = base64.b64encode(FIXED_SEED_BYTES).decode("ascii")

# 第二颗种子用于多种子扫描
FIXED_SEED2_BYTES = b"rct-fixed-seed-v1-B".ljust(32, b"0")
assert len(FIXED_SEED2_BYTES) == 32
FIXED_SEED2_B64 = base64.b64encode(FIXED_SEED2_BYTES).decode("ascii")


@dataclass(frozen=True)
class SubjectCase:
    subject_id: str
    features: dict[str, str]


@dataclass(frozen=True)
class StudyFixture:
    study_id: str
    arms: tuple[tuple[str, int], ...]
    factors: tuple[tuple[str, tuple[str, ...]], ...]
    block_multiple: int
    tail_policy: str
    seed_b64: str
    enrollments: tuple[SubjectCase, ...]
    # 并发批次：同一批内对象在不同线程同时登记
    concurrent_batch: tuple[str, ...] = ()
    # 特征篡改回放：subject_id -> 被篡改的特征
    tampered_replays: dict[str, dict[str, str]] = field(default_factory=dict)


def two_arm_two_strata_fixture(tail_policy: str = "keep_open") -> StudyFixture:
    """2 臂 1:1，2 个分层因子，区组长 4，共 23 个对象 → 5 整组 + 3 尾组。"""
    centers = ("C1", "C2", "C3")
    stages = ("early", "late")
    cases = []
    for i in range(23):
        cases.append(SubjectCase(
            subject_id=f"S{i:03d}",
            features={"center": centers[i % 3], "stage": stages[i % 2]},
        ))
    return StudyFixture(
        study_id="FIXT-2ARM-2STRATA",
        arms=(("control", 1), ("treatment", 1)),
        factors=(("center", centers), ("stage", stages)),
        block_multiple=2,
        tail_policy=tail_policy,
        seed_b64=FIXED_SEED_B64,
        enrollments=tuple(cases),
        concurrent_batch=tuple(f"S{i:03d}" for i in range(8)),
        tampered_replays={
            "S000": {"center": "C2", "stage": "early"},  # 改 center
            "S001": {"center": "C1", "stage": "late"},   # 改 stage
        },
    )


def ratio_fixture() -> StudyFixture:
    """不等比 1:2，单因子 2 层，区组长 6，共 13 个对象 → 2 整组 + 1 尾组。"""
    cases = [
        SubjectCase(f"R{i:03d}", {"region": ("N", "S")[i % 2]})
        for i in range(13)
    ]
    return StudyFixture(
        study_id="FIXT-RATIO-1-2",
        arms=(("A", 1), ("B", 2)),
        factors=(("region", ("N", "S")),),
        block_multiple=2,
        tail_policy="keep_open",
        seed_b64=FIXED_SEED2_B64,
        enrollments=tuple(cases),
        concurrent_batch=tuple(f"R{i:03d}" for i in range(6)),
    )
