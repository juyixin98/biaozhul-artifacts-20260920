"""可复现的合成数据。

包含两套数据：

1. :func:`build_canonical_dataset` —— 小型、确定性、可手算的经典场景，
   显式覆盖三种验收情形：晚到特征、同时间多版本、缺特征；
2. :func:`build_generated_dataset` —— 用固定随机种子生成的较大数据，
   其中"初步值准时入库、修订值三天后入库"，用于演示事后修订泄漏如何
   污染朴素 as-of join，并交由简单模型量化差异。
"""
from __future__ import annotations

from dataclasses import dataclass

import numpy as np

from .engine import Event, FeatureRecord, FeatureStore
from .times import dt, hours

# 规范场景的时间原点
T0 = dt(2026, 1, 1, 0, 0, 0)


@dataclass(frozen=True)
class CanonicalDataset:
    store: FeatureStore
    events: tuple[Event, ...]
    records: tuple[FeatureRecord, ...]


def build_canonical_dataset() -> CanonicalDataset:
    """构造可手算的经典场景（所有入库/生效时刻均为字面量）。

    场景一览（``T0 = 2026-01-01T00:00Z``）::

        实体 cust_A, 特征 f_balance（演示"晚到修订"）
          rA1  effective=T0           value=100.0 ingest=T0+1h
          rA2  effective=T0+1d        value=110.0 ingest=T0+1d+1h   准时
          rA3  effective=T0+1d        value=111.0 ingest=T0+3d      晚到修订(同生效时间)

        实体 cust_B, 特征 f_score（同一生效时刻、同一入库时刻，三个版本）
          rB1 effective=T0 value=1.0 v1 ingest=T0
          rB2 effective=T0 value=2.0 v2 ingest=T0
          rB3 effective=T0 value=3.0 v3 ingest=T0      -> 版本号裁决取 3.0
        实体 cust_B, 特征 f_tier（同生效时间、同版本，入库有先后）
          rB4 effective=T0 value=10.0 v1 ingest=T0+2h
          rB5 effective=T0 value=20.0 v1 ingest=T0+5h  -> 入库最晚裁决取 20.0

        实体 cust_C, 特征 f_balance（生效于未来）
          rC1 effective=T0+10d value=500.0 ingest=T0-1d（早已入库，但业务尚未生效）

        实体 cust_D, 特征 f_balance（生效很早，但入库晚于事件）
          rD1 effective=T0 value=77.0 ingest=T0+5d

        实体 cust_E 不出现在任何特征记录中 -> MISSING_KEY
    """
    d = 24
    records: list[FeatureRecord] = [
        # --- A: 晚到修订 ---
        FeatureRecord("cust_A", "f_balance", 100.0, T0, T0 + hours(1), version=1, record_id="rA1"),
        FeatureRecord("cust_A", "f_balance", 110.0, T0 + hours(d), T0 + hours(d) + hours(1),
                      version=1, record_id="rA2"),
        FeatureRecord("cust_A", "f_balance", 111.0, T0 + hours(d), T0 + hours(3 * d),
                      version=2, record_id="rA3"),
        # --- B: 同时间多版本（版本号裁决）---
        FeatureRecord("cust_B", "f_score", 1.0, T0, T0, version=1, record_id="rB1"),
        FeatureRecord("cust_B", "f_score", 2.0, T0, T0, version=2, record_id="rB2"),
        FeatureRecord("cust_B", "f_score", 3.0, T0, T0, version=3, record_id="rB3"),
        # --- B: 同时间同版本（入库时间裁决）---
        FeatureRecord("cust_B", "f_tier", 10.0, T0, T0 + hours(2), version=1, record_id="rB4"),
        FeatureRecord("cust_B", "f_tier", 20.0, T0, T0 + hours(5), version=1, record_id="rB5"),
        # --- C: 未来生效 ---
        FeatureRecord("cust_C", "f_balance", 500.0, T0 + hours(10 * d), T0 - hours(24),
                      version=1, record_id="rC1"),
        # --- D: 晚入库（ALL_LATE）---
        FeatureRecord("cust_D", "f_balance", 77.0, T0, T0 + hours(5 * d),
                      version=1, record_id="rD1"),
    ]

    events: tuple[Event, ...] = (
        Event("cust_A", T0 + hours(12), label=0),                 # E1
        Event("cust_A", T0 + hours(d) + hours(12), label=1),     # E2 修订未入库
        Event("cust_A", T0 + hours(3 * d) + hours(12), label=1), # E3 修订已入库
        Event("cust_B", T0 + hours(d), label=1),                 # E4
        Event("cust_C", T0 + hours(d), label=0),                 # E5 仅未来记录
        Event("cust_C", T0 + hours(11 * d), label=1),            # E6 记录已生效
        Event("cust_D", T0 + hours(d), label=0),                 # E7 记录晚入库
        Event("cust_D", T0 + hours(6 * d), label=1),             # E8 记录已入库
        Event("cust_E", T0 + hours(d), label=0),                 # E9 实体无特征
    )
    return CanonicalDataset(
        store=FeatureStore(records), events=events, records=tuple(records)
    )


# ---------------------------------------------------------------------------
# 较大规模的可复现合成数据：演示修订泄漏
# ---------------------------------------------------------------------------
@dataclass(frozen=True)
class GeneratedDataset:
    store: FeatureStore
    events: tuple[Event, ...]
    preliminary_records: int
    revision_records: int


def build_generated_dataset(
    seed: int = 42,
    n_entities: int = 40,
    n_days: int = 30,
    n_events: int = 320,
    revision_lag_hours: int = 72,
) -> GeneratedDataset:
    """生成含"准时初步值 + 晚到修订值"的合成数据。

    每个实体每天 00:00 有一条 f_risk 快照：

    * ``preliminary``：业务生效后 1 小时入库，带噪声（T+1 才能看到的粗值）；
    * ``revision``：同一生效时间、72 小时后入库的"定稿值"，恰好等于
      生成标签所用的潜在变量 —— 模拟现实中事后回填/修订泄漏未来信息。

    标签仅依赖事件发生时的潜在风险。PIT join 在 72 小时窗口内只能看到
    带噪声的初步值；忽略入库时间的朴素 join 会直接拿到定稿值而"泄漏"。
    """
    rng = np.random.default_rng(seed)
    hour_ms = hours(1)
    day_ms = hours(24)

    entity_baseline = rng.normal(0.0, 1.0, size=n_entities)
    # 潜在"真值"：实体基线 + 每日独立的平稳噪声（无全局趋势，保证标签不随时间失衡）
    latent = entity_baseline[:, None] + rng.normal(
        0.0, 0.25, size=(n_entities, n_days)
    )

    records: list[FeatureRecord] = []
    n_prelim = n_revision = 0

    for e in range(n_entities):
        entity_id = f"ent_{e:03d}"
        for day in range(n_days):
            effective = T0 + day * day_ms
            true_value = float(latent[e, day])
            preliminary = true_value + rng.normal(0.0, 2.0)  # 明显噪声的粗值
            records.append(FeatureRecord(
                entity_id, "f_risk", preliminary,
                effective, effective + hour_ms, version=1,
                record_id=f"{entity_id}-d{day:02d}-prelim",
            ))
            records.append(FeatureRecord(
                entity_id, "f_risk", true_value,
                effective, effective + revision_lag_hours * hour_ms, version=2,
                record_id=f"{entity_id}-d{day:02d}-final",
            ))
            n_prelim += 1
            n_revision += 1

    # 事件时刻均匀撒在 [第1天, 第28天)；标签由当日潜在真值决定
    event_entities = rng.integers(0, n_entities, size=n_events)
    event_hours = rng.integers(24, 28 * 24, size=n_events)
    events: list[Event] = []
    for i in range(n_events):
        e = int(event_entities[i])
        h = int(event_hours[i])
        day = h // 24
        t = T0 + h * hour_ms
        label = 1 if latent[e, day] + rng.normal(0.0, 0.5) > 0 else 0
        events.append(Event(f"ent_{e:03d}", t, label=float(label)))

    return GeneratedDataset(
        store=FeatureStore(records),
        events=tuple(events),
        preliminary_records=n_prelim,
        revision_records=n_revision,
    )
