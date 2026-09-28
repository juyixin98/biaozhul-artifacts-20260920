"""编排模块：在单进程内确定性地模拟多工作者同步训练。

与真实多进程的区别仅在于传输：这里直接调用
:class:`~gradbucket.training.RoundCoordinator`，用可注入的假时钟精确控制
心跳超时，从而能穷尽测试"完成顺序、缺梯度、不等批量、节点中断"四类场景，
且无任何时序抖动。

真实多进程路径见 :mod:`gradbucket.cluster`（uvicorn 子进程 + HTTP +
:mod:`gradbucket.worker_cli`），两条路径共用同一套 coordinator/reducer，
HTTP 集成测试会对语义再复验一次。
"""

from __future__ import annotations

from dataclasses import dataclass, field
from itertools import permutations

import numpy as np

from .diagnostics import DiagnosticLog, Verdict
from .graph import LinearModel, ModelConfig, make_dataset, shard_indices
from .tensors import BucketLayout, build_layout
from .training import RoundCoordinator


class FakeClock:
    """单调假时钟，只有显式 :meth:`advance` 才前进。"""

    def __init__(self, start: float = 1000.0) -> None:
        self._t = start

    def __call__(self) -> float:
        return self._t

    def advance(self, seconds: float) -> None:
        self._t += seconds


@dataclass
class Scenario:
    """一轮训练的完整场景描述（合成夹具）。"""

    n_samples: int = 12
    n_workers: int = 3
    shard_sizes: list[int] | None = None  # None=均匀；否则和必须等于 n_samples
    bucket_capacity: int = 2
    learning_rate: float = 0.1
    liveness_timeout: float = 5.0
    seed: int = 445
    # worker_id -> 该工作者"没算"的参数名集合（提交显式占位 mask=False）
    omit_params: dict[str, set[str]] = field(default_factory=dict)
    # worker 在提交完编号为 die_after[w] 的桶后中断（不再出现）
    die_after: dict[str, int] = field(default_factory=dict)
    # (worker, bucket) 被压下不提交，但该工作者仍保持心跳（无法判定场景）
    withhold: set[tuple[str, int]] = field(default_factory=set)
    # 显式提交顺序；None 表示工作者优先顺序
    order: list[tuple[str, int]] | None = None
    # seal 前静默时长；超过 liveness_timeout 即可观察到中断
    silence_before_seal: float = 0.0
    include_spare: bool = True
    known_zero_params: tuple[str, ...] = ("spare",)


@dataclass
class ScenarioResult:
    verdict: Verdict
    commit: object | None
    coordinator: RoundCoordinator
    layout: BucketLayout
    shards: dict
    omit_params: dict[str, set[str]]
    generations_seen_during_submission: list[int]
    weights_seen_during_submission: list[dict]


def worker_ids(n: int) -> list[str]:
    return [f"w{i}" for i in range(n)]


def default_order(n_workers: int, num_buckets: int) -> list[tuple[str, int]]:
    return [(w, b) for w in worker_ids(n_workers) for b in range(num_buckets)]


def representative_orders(
    n_workers: int, num_buckets: int
) -> list[list[tuple[str, int]]]:
    """有代表性的完成顺序：桶优先、工作者优先、若干交错排列。"""
    ids = worker_ids(n_workers)
    full = {
        (w, b) for w in ids for b in range(num_buckets)
    }
    orders = [
        [(w, b) for b in range(num_buckets) for w in ids],  # 桶优先
        [(w, b) for w in ids for b in range(num_buckets)],  # 工作者优先
    ]
    for perm in list(permutations(ids))[:3]:
        seq = [(w, b) for b in range(num_buckets) for w in perm]
        if seq not in orders:
            orders.append(seq)
    assert all(set(o) == full for o in orders)
    return orders


def build_worker_payload(
    layout: BucketLayout,
    bucket_index: int,
    grads: dict[str, np.ndarray],
    omit: set[str],
) -> tuple[np.ndarray, np.ndarray]:
    """构造 (vec, mask)：未算参数写显式零占位且该槽 mask 全 False。"""
    slots = layout.slots_for_bucket(bucket_index)
    total = sum(s.length for s in slots)
    vec = np.zeros(total, dtype=np.float64)
    mask = np.zeros(total, dtype=bool)
    cursor = 0
    for s in slots:
        if s.param.name not in omit:
            vec[cursor : cursor + s.length] = grads[s.param.name].reshape(-1)
            mask[cursor : cursor + s.length] = True
        # 否则保持显式占位：零向量 + False
        cursor += s.length
    return vec, mask


def run_scenario(scenario: Scenario) -> ScenarioResult:
    """执行场景，返回判定、commit 证据及提交期间观察到的世代/权重。"""
    cfg = ModelConfig(include_spare=scenario.include_spare)
    ids = worker_ids(scenario.n_workers)
    if scenario.shard_sizes is not None and (
        len(scenario.shard_sizes) != scenario.n_workers
        or sum(scenario.shard_sizes) != scenario.n_samples
    ):
        raise ValueError("shard_sizes 长度须等于工作者数且总和等于样本总数")

    x_all, y_all = make_dataset(
        scenario.n_samples, seed=scenario.seed, n_in=cfg.n_in, n_out=cfg.n_out
    )
    shards: dict[str, tuple[np.ndarray, np.ndarray, int]] = {}
    for rank, w in enumerate(ids):
        idx = shard_indices(
            scenario.n_samples, rank, scenario.n_workers,
            shard_sizes=scenario.shard_sizes, seed=scenario.seed,
        )
        shards[w] = (x_all[idx], y_all[idx], len(idx))

    model = LinearModel(cfg)
    params = model.param_specs()
    layout = build_layout(0, params, scenario.bucket_capacity)
    initial = {spec.name: spec.zeros() for spec in params}

    clock = FakeClock()
    coord = RoundCoordinator(
        liveness_timeout=scenario.liveness_timeout,
        clock=clock,
        diaglog=DiagnosticLog(),
    )
    coord.begin_round(
        round_index=0,
        params=params,
        initial_weights=initial,
        expected_workers=ids,
        bucket_capacity=scenario.bucket_capacity,
        known_zero_params=scenario.known_zero_params,
    )

    # 各工作者基于轮初同一快照计算本地梯度（同步训练契约）。
    snap0 = coord.snapshot_weights()
    worker_grads: dict[str, tuple[dict, int]] = {}
    for w, (xs, ys, n) in shards.items():
        local = LinearModel(cfg)
        local.set_weights(snap0["w"], snap0["b"])
        worker_grads[w] = local.gradients(xs, ys)

    for w in ids:  # 开工报到；开工前即中断者（die_after < 0）不报心跳
        if scenario.die_after.get(w, 0) >= 0:
            coord.heartbeat(w)

    order = scenario.order or default_order(scenario.n_workers, layout.num_buckets)
    gens_seen: list[int] = []
    weights_seen: list[dict] = []
    dead: set[str] = {w for w, after in scenario.die_after.items() if after < 0}
    for w, b in order:
        if (w, b) in scenario.withhold:
            continue
        if w in dead:
            continue
        grads, n = worker_grads[w]
        omit = scenario.omit_params.get(w, set())
        vec, mask = build_worker_payload(layout, b, grads, omit)
        verdict = coord.submit_bucket(
            round_index=0, generation=0, worker_id=w, bucket_index=b,
            vec=vec, mask=mask, sample_count=n,
        )
        assert verdict == Verdict.ACCEPTED, f"场景夹具提交被拒: {verdict}"
        clock.advance(0.1)
        gens_seen.append(coord.generation)
        weights_seen.append(coord.snapshot_weights())
        if scenario.die_after.get(w) == b:
            dead.add(w)  # 此后不再提交、不再心跳

    # 静默期后：存活者继续续约心跳，中断者停留在最后一次提交时间。
    clock.advance(scenario.silence_before_seal)
    for w in ids:
        if w not in dead:
            coord.heartbeat(w)

    verdict, commit = coord.seal_round(learning_rate=scenario.learning_rate)
    return ScenarioResult(
        verdict=verdict,
        commit=commit,
        coordinator=coord,
        layout=layout,
        shards=shards,
        omit_params=scenario.omit_params,
        generations_seen_during_submission=gens_seen,
        weights_seen_during_submission=weights_seen,
    )
