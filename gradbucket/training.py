"""训练状态模块：轮次状态机与两阶段提交。

这是"桶完成不能提前更新权重"与"工作者失联该轮拒绝提交"两条要求的落点。

一轮的生命周期::

    BEGIN ──► OPEN ──► （各工作者提交各桶；某桶收齐即 STAGED 归约结果，
                        但世代与权重在此阶段绝不变更）
                  ──► seal():
                        · 欠桶且欠桶者失联    ──► REJECTED（REJECTED_WORKER_LOST）
                        · 欠桶但欠桶者仍新鲜  ──► INDETERMINATE_PENDING（仍开放）
                        · 桶齐但必需槽零证据  ──► REJECTED（REJECTED_NO_EVIDENCE）
                        · 全部桶齐且证据充分  ──► COMMITTED（原子更新权重，世代 +1）

   零证据只可能在桶收齐后确认，而桶齐意味着无法再补提（重复提交被拒），
   因此它是确定性终局：判 REJECTED 并允许 reset 重开，而不是永久卡在
   "暂时无法判定"。

关键不可变量
------------
1. 权重只在 COMMIT 的瞬间变化；STAGED 持有归约结果的*副本*，任何工作者在
   seal 成功前读到的都是本轮开始时的权重快照与同一世代号。
2. 提交必须携带其计算所依据的世代号；世代不符一律 REJECTED_STALE_ROUND，
   杜绝跨轮梯度污染新权重（这正是"后续梯度读取新权重"问题的另一面）。
3. 失联判定同时看*心跳新鲜度*与*缺桶事实*：欠桶工作者心跳超时（或超过
   开工宽限仍未首次接触）才判 REJECTED_WORKER_LOST；欠桶但仍新鲜时给
   INDETERMINATE_PENDING，把"拒绝"与"暂时无法判定"严格区分。已交齐全部
   桶的工作者正常退出不算失联。
4. seal 幂等：已终结的轮重复 seal 返回缓存结局，不二次提交、不抛异常。
"""

from __future__ import annotations

import threading
import time
from dataclasses import dataclass, field
from enum import Enum
from typing import Callable, Sequence

import numpy as np

from .diagnostics import (
    Diagnostic,
    DiagnosticLog,
    Verdict,
    new_request_id,
    tensor_fingerprint,
)
from .reducer import (
    BucketReduction,
    Contribution,
    ReductionError,
    reduce_bucket,
)
from .tensors import BucketLayout, ParamSpec, build_layout, unpack_bucket


class RoundStatus(str, Enum):
    OPEN = "OPEN"
    COMMITTED = "COMMITTED"
    REJECTED = "REJECTED"


@dataclass(frozen=True)
class CommitResult:
    round_index: int
    generation_before: int
    generation_after: int
    weights: dict[str, np.ndarray]
    gradients: dict[str, np.ndarray]
    bucket_bases: tuple[dict, ...]
    request_id: str


@dataclass
class _Round:
    index: int
    generation: int
    layout: BucketLayout
    expected_workers: frozenset[str]
    known_zero_params: frozenset[str]
    weights_snapshot: dict[str, np.ndarray]
    started_at: float
    startup_grace: float
    received: dict[int, dict[str, Contribution]] = field(default_factory=dict)
    staged: dict[int, BucketReduction] = field(default_factory=dict)
    status: RoundStatus = RoundStatus.OPEN
    commit: CommitResult | None = None
    terminal_verdict: Verdict | None = None


def sgd_step(
    weights: dict[str, np.ndarray],
    gradients: dict[str, np.ndarray],
    learning_rate: float,
) -> dict[str, np.ndarray]:
    """纯函数 SGD：返回新权重 dict，不修改入参（不可变更新）。"""
    if learning_rate <= 0:
        raise ValueError("learning_rate 必须为正")
    return {k: weights[k] - learning_rate * gradients[k] for k in weights}


class RoundCoordinator:
    """线程安全的同步训练轮次协调器（API 层之下唯一持有状态的对象）。"""

    def __init__(
        self,
        *,
        liveness_timeout: float = 5.0,
        startup_grace: float | None = None,
        clock: Callable[[], float] = time.monotonic,
        diaglog: DiagnosticLog | None = None,
    ) -> None:
        self._lock = threading.RLock()
        self._timeout = float(liveness_timeout)
        # 开工宽限：工作者首次接触（心跳/提交）之前，不计心跳超时，
        # 避免把"启动慢"永久误判成"节点失联"。默认与失联阈值相同。
        self._default_grace = float(
            startup_grace if startup_grace is not None else liveness_timeout
        )
        self._clock = clock
        self._diag = diaglog or DiagnosticLog()
        self._generation = 0
        self._weights: dict[str, np.ndarray] = {}
        self._round: _Round | None = None
        self._heartbeats: dict[str, float] = {}

    # ------------------------------------------------------------------
    # 只读视图
    # ------------------------------------------------------------------

    @property
    def generation(self) -> int:
        with self._lock:
            return self._generation

    def snapshot_weights(self) -> dict[str, np.ndarray]:
        with self._lock:
            return {k: v.copy() for k, v in self._weights.items()}

    def current_round_index(self) -> int | None:
        with self._lock:
            return None if self._round is None else self._round.index

    def diagnostics(self) -> DiagnosticLog:
        return self._diag

    def round_view(self) -> dict | None:
        """供 /rounds/current 返回的状态视图（不含张量内容）。"""
        with self._lock:
            r = self._round
            if r is None:
                return None
            now = self._clock()
            return {
                "round_index": r.index,
                "generation": r.generation,
                "status": r.status.value,
                "expected_workers": sorted(r.expected_workers),
                "bucket_sizes": list(r.layout.bucket_sizes),
                "slots": r.layout.describe(),
                "staged_buckets": sorted(r.staged),
                "missing_buckets": self._missing_buckets_locked(r),
                "worker_last_heartbeat_age": {
                    w: round(now - self._heartbeats.get(w, -np.inf), 6)
                    for w in sorted(r.expected_workers)
                },
            }

    # ------------------------------------------------------------------
    # 轮次开始 / 工作者注册
    # ------------------------------------------------------------------

    def begin_round(
        self,
        round_index: int,
        params: Sequence[ParamSpec],
        initial_weights: dict[str, np.ndarray],
        expected_workers: Sequence[str],
        *,
        bucket_capacity: int = 2,
        known_zero_params: Sequence[str] = (),
        request_id: str | None = None,
    ) -> dict:
        request_id = request_id or new_request_id("begin")
        with self._lock:
            if self._round is not None and self._round.status == RoundStatus.OPEN:
                raise RuntimeError(
                    f"第 {self._round.index} 轮仍开放，须先 commit 或 reject"
                )
            workers = tuple(expected_workers)
            if len(set(workers)) != len(workers):
                raise ValueError("工作者 id 重复")
            if not workers:
                raise ValueError("至少需要一个工作者")
            layout = build_layout(round_index, list(params), bucket_capacity)
            missing = {s.param.name for s in layout.slots} - set(initial_weights)
            if missing:
                raise ValueError(f"初始权重缺少参数: {sorted(missing)}")
            snapshot = {
                s.param.name: np.array(
                    initial_weights[s.param.name], dtype=np.float64, copy=True
                ).reshape(s.param.shape)
                for s in layout.slots
            }
            self._weights = {k: v.copy() for k, v in snapshot.items()}
            self._round = _Round(
                index=round_index,
                generation=self._generation,
                layout=layout,
                expected_workers=frozenset(workers),
                known_zero_params=frozenset(known_zero_params),
                weights_snapshot=snapshot,
                started_at=self._clock(),
                startup_grace=self._default_grace,
                received={b: {} for b in range(layout.num_buckets)},
            )
            self._heartbeats = {}
            return self.round_view()

    def heartbeat(self, worker_id: str, *, request_id: str | None = None) -> str:
        request_id = request_id or new_request_id("hb")
        with self._lock:
            r = self._require_open_round_locked()
            if worker_id not in r.expected_workers:
                self._record(
                    request_id,
                    Verdict.REJECTED_STALE_ROUND,
                    f"未知工作者 {worker_id}",
                    r,
                    worker_id=worker_id,
                )
                raise KeyError(worker_id)
            self._heartbeats[worker_id] = self._clock()
            return request_id

    # ------------------------------------------------------------------
    # 桶提交（第一阶段：只暂存，不更新权重）
    # ------------------------------------------------------------------

    def submit_bucket(
        self,
        round_index: int,
        generation: int,
        worker_id: str,
        bucket_index: int,
        vec: np.ndarray,
        mask: np.ndarray,
        sample_count: int,
        *,
        request_id: str | None = None,
    ) -> Verdict:
        request_id = request_id or new_request_id("sub")
        with self._lock:
            r = self._round
            if r is None:
                self._record(
                    request_id, Verdict.REJECTED_STALE_ROUND, "当前没有开放轮次",
                    None, worker_id=worker_id, bucket=bucket_index,
                )
                return Verdict.REJECTED_STALE_ROUND
            if r.status != RoundStatus.OPEN:
                self._record(
                    request_id, Verdict.REJECTED_SEALED,
                    f"第 {r.index} 轮已 {r.status.value}，拒绝迟到提交",
                    r, worker_id=worker_id, bucket=bucket_index,
                )
                return Verdict.REJECTED_SEALED
            if round_index != r.index or generation != r.generation:
                self._record(
                    request_id, Verdict.REJECTED_STALE_ROUND,
                    f"提交轮次/世代 (r{round_index},g{generation}) 与当前 "
                    f"(r{r.index},g{r.generation}) 不符，拒绝跨轮梯度",
                    r, worker_id=worker_id, bucket=bucket_index,
                )
                return Verdict.REJECTED_STALE_ROUND
            if worker_id not in r.expected_workers:
                self._record(
                    request_id, Verdict.REJECTED_STALE_ROUND,
                    f"未知工作者 {worker_id}",
                    r, worker_id=worker_id, bucket=bucket_index,
                )
                return Verdict.REJECTED_STALE_ROUND
            if not (0 <= bucket_index < r.layout.num_buckets):
                self._record(
                    request_id, Verdict.REJECTED_BUCKET_SHAPE,
                    f"桶号 {bucket_index} 越界（共 {r.layout.num_buckets} 桶）",
                    r, worker_id=worker_id, bucket=bucket_index,
                )
                return Verdict.REJECTED_BUCKET_SHAPE

            # 重复提交优先判定：无论本次载荷是否合法，同一 (worker, bucket)
            # 的第二次提交都归为 DUPLICATE，类别不被载荷形状干扰。
            if worker_id in r.received[bucket_index]:
                self._record(
                    request_id, Verdict.REJECTED_DUPLICATE,
                    f"工作者 {worker_id} 已提交过桶 {bucket_index}，拒绝重复",
                    r, worker_id=worker_id, bucket=bucket_index,
                )
                return Verdict.REJECTED_DUPLICATE

            contrib = Contribution(
                worker_id=worker_id,
                vec=np.asarray(vec, dtype=np.float64),
                mask=np.asarray(mask, dtype=bool),
                sample_count=int(sample_count),
                request_id=request_id,
            )
            # 复用 reducer 的权威校验，非法提交不进入暂存区。
            try:
                self._validate_contribution_locked(r, bucket_index, contrib)
            except ReductionError as exc:
                verdict_map = {
                    "REJECTED_BUCKET_SHAPE": Verdict.REJECTED_BUCKET_SHAPE,
                    "REJECTED_PARTIAL_SLOT_MASK":
                        Verdict.REJECTED_PARTIAL_SLOT_MASK,
                    "REJECTED_SAMPLE_COUNT": Verdict.REJECTED_SAMPLE_COUNT,
                    "REJECTED_NON_FINITE": Verdict.REJECTED_NON_FINITE,
                }
                self._record(
                    request_id, verdict_map[exc.code], f"拒绝提交: {exc}",
                    r, worker_id=worker_id, bucket=bucket_index,
                    state={"fingerprint": tensor_fingerprint(contrib.vec),
                           "sample_count": sample_count},
                )
                return verdict_map[exc.code]

            # 提交本身就是存活证据。
            self._heartbeats[worker_id] = self._clock()
            r.received[bucket_index][worker_id] = contrib

            if len(r.received[bucket_index]) == len(r.expected_workers):
                # 桶收齐：立即归约并 STAGE，但绝不触碰权重与世代。
                # 输入合法不代表归约结果有限（极端大梯度×样本数可能溢出）；
                # 一旦发生，本轮终结为 REJECTED_NON_FINITE，绝不提交 NaN 权重。
                try:
                    reduction = reduce_bucket(
                        r.layout, bucket_index,
                        list(r.received[bucket_index].values()),
                    )
                except ReductionError as exc:
                    verdict_map = {
                        "REJECTED_NON_FINITE": Verdict.REJECTED_NON_FINITE,
                        "REJECTED_BUCKET_SHAPE": Verdict.REJECTED_BUCKET_SHAPE,
                    }
                    verdict = verdict_map.get(exc.code,
                                              Verdict.REJECTED_BUCKET_SHAPE)
                    self._record(
                        request_id, verdict,
                        f"桶 {bucket_index} 归约失败，终结本轮: {exc}",
                        r, worker_id=worker_id, bucket=bucket_index,
                    )
                    r.status = RoundStatus.REJECTED
                    r.terminal_verdict = verdict
                    return verdict
                r.staged[bucket_index] = reduction
                self._record(
                    request_id, Verdict.ACCEPTED,
                    f"桶 {bucket_index} 收齐并暂存（STAGED）；世代 {r.generation} "
                    "保持不变，权重未更新，等待其余桶封存",
                    r, worker_id=worker_id, bucket=bucket_index,
                    state={"staged_buckets": sorted(r.staged),
                           "basis": reduction.basis()},
                )
            else:
                self._record(
                    request_id, Verdict.ACCEPTED,
                    f"桶 {bucket_index} 已记录 {worker_id} 的提交"
                    f"（{len(r.received[bucket_index])}/"
                    f"{len(r.expected_workers)}），等待其余工作者",
                    r, worker_id=worker_id, bucket=bucket_index,
                )
            return Verdict.ACCEPTED

    # ------------------------------------------------------------------
    # 封存（第二阶段：判定 + 原子提交）
    # ------------------------------------------------------------------

    def seal_round(
        self,
        *,
        learning_rate: float = 0.1,
        request_id: str | None = None,
    ) -> tuple[Verdict, CommitResult | None]:
        request_id = request_id or new_request_id("seal")
        with self._lock:
            if self._round is None:
                self._record(
                    request_id, Verdict.REJECTED_SEALED, "当前没有轮次可封存",
                    None,
                )
                raise RuntimeError("当前没有轮次")
            r = self._round

            # 幂等：已终结的轮重复 seal 直接返回缓存结局，不抛异常、不重复提交。
            if r.status != RoundStatus.OPEN:
                if r.status == RoundStatus.COMMITTED and r.commit is not None:
                    return Verdict.ACCEPTED, r.commit
                terminal = r.terminal_verdict or Verdict.REJECTED_SEALED
                self._record(
                    request_id, terminal,
                    f"第 {r.index} 轮已终结为 {r.status.value}，重复 seal 幂等返回",
                    r,
                )
                return terminal, None

            now = self._clock()
            missing = self._missing_buckets_locked(r)

            # 只有"仍欠桶"的工作者才需要判活；已交齐全部桶的工作者退出是
            # 正常完成，不应因其停止心跳而把本轮判成失联。
            outstanding = {
                w for w in r.expected_workers
                if any(w not in r.received[b] for b in missing)
            }

            def _is_lost(w: str) -> bool:
                hb = self._heartbeats.get(w)
                if hb is None:
                    # 从未接触：在开工宽限期内视为"可能只是启动慢"，不判死。
                    return now - r.started_at > r.startup_grace
                return now - hb > self._timeout

            lost = {w for w in outstanding if _is_lost(w)}

            if missing:
                if lost:
                    self._record(
                        request_id, Verdict.REJECTED_WORKER_LOST,
                        f"工作者 {sorted(lost)} 心跳超时（或超过开工宽限仍未报到）"
                        f"且缺失桶 {missing}，本轮拒绝提交"
                        "（宁可不更新，不用残缺桶更新）",
                        r, state={"lost_workers": sorted(lost),
                                  "outstanding_workers": sorted(outstanding),
                                  "missing_buckets": missing,
                                  "staged_buckets": sorted(r.staged)},
                    )
                    r.status = RoundStatus.REJECTED
                    r.terminal_verdict = Verdict.REJECTED_WORKER_LOST
                    return Verdict.REJECTED_WORKER_LOST, None
                never_contacted = sorted(
                    w for w in outstanding if w not in self._heartbeats
                )
                self._record(
                    request_id, Verdict.INDETERMINATE_PENDING,
                    f"桶 {missing} 尚未到齐，欠桶工作者 {sorted(outstanding)} "
                    "尚在开工宽限/心跳新鲜，无法判定成败：本轮保持开放，稍后重试",
                    r, state={"outstanding_workers": sorted(outstanding),
                              "never_contacted": never_contacted,
                              "missing_buckets": missing,
                              "staged_buckets": sorted(r.staged)},
                )
                return Verdict.INDETERMINATE_PENDING, None

            # 桶齐：逐槽检查证据。零证据槽若属于已知零参数则放行。
            # 桶已齐则无法再补提（重复提交被拒），零证据是确定性终局——
            # 直接判 REJECTED_NO_EVIDENCE（可 reset 后重开），而非永久卡死。
            no_evidence_slots: list[str] = []
            for b, red in sorted(r.staged.items()):
                for srep in red.slots:
                    if not srep.covered and srep.param not in r.known_zero_params:
                        no_evidence_slots.append(f"bucket{b}:{srep.param}")
            if no_evidence_slots:
                self._record(
                    request_id, Verdict.REJECTED_NO_EVIDENCE,
                    f"槽位 {no_evidence_slots} 没有任何真实梯度贡献者，且桶已齐"
                    "无法补提；拒绝把零猜测成平均梯度，本轮拒绝提交",
                    r, state={"no_evidence_slots": no_evidence_slots},
                )
                r.status = RoundStatus.REJECTED
                r.terminal_verdict = Verdict.REJECTED_NO_EVIDENCE
                return Verdict.REJECTED_NO_EVIDENCE, None

            gradients = self._assemble_gradients_locked(r)
            gen_before = self._generation
            new_weights = sgd_step(self._weights, gradients, learning_rate)
            # 原子点：上面全部为纯计算，此处一次性替换。
            self._weights = new_weights
            self._generation += 1
            result = CommitResult(
                round_index=r.index,
                generation_before=gen_before,
                generation_after=self._generation,
                weights={k: v.copy() for k, v in new_weights.items()},
                gradients=gradients,
                bucket_bases=tuple(
                    r.staged[b].basis() for b in sorted(r.staged)
                ),
                request_id=request_id,
            )
            r.status = RoundStatus.COMMITTED
            r.commit = result
            r.terminal_verdict = Verdict.ACCEPTED
            self._record(
                request_id, Verdict.ACCEPTED,
                f"全部桶封存成功：世代 {gen_before} -> {self._generation}，"
                "权重已按各槽真实样本数加权平均后原子更新",
                r,
                state={
                    "generation_before": gen_before,
                    "generation_after": self._generation,
                    "bucket_bases": list(result.bucket_bases),
                    "gradient_fingerprints": {
                        k: tensor_fingerprint(v) for k, v in gradients.items()
                    },
                },
            )
            return Verdict.ACCEPTED, result

    def reset_rejected_round(self) -> None:
        """终结被拒绝的轮次，以便开始新一轮（失败的轮不允许复活）。"""
        with self._lock:
            r = self._round
            if r is None:
                return
            if r.status == RoundStatus.OPEN:
                raise RuntimeError("开放轮不能 reset")
            self._round = None
            self._heartbeats = {}

    # ------------------------------------------------------------------
    # 内部
    # ------------------------------------------------------------------

    def _require_open_round_locked(self) -> _Round:
        if self._round is None:
            raise RuntimeError("当前没有轮次")
        if self._round.status != RoundStatus.OPEN:
            raise RuntimeError(f"轮次状态为 {self._round.status.value}")
        return self._round

    def _missing_buckets_locked(self, r: _Round) -> list[int]:
        return [
            b for b in range(r.layout.num_buckets)
            if len(r.received[b]) < len(r.expected_workers)
        ]

    @staticmethod
    def _validate_contribution_locked(
        r: _Round, bucket_index: int, contrib: Contribution
    ) -> None:
        """长度/dtype/样本数/非有限 + 整槽遮罩校验（复用 reducer 同一份规则）。"""
        from .reducer import _local_bounds, validate_contribution_for_slots

        slots = r.layout.slots_for_bucket(bucket_index)
        validate_contribution_for_slots(contrib, _local_bounds(slots))

    def _assemble_gradients_locked(self, r: _Round) -> dict[str, np.ndarray]:
        grads: dict[str, np.ndarray] = {}
        for b in range(r.layout.num_buckets):
            grads.update(unpack_bucket(r.layout, b, r.staged[b].vec))
        return grads

    def _record(
        self,
        request_id: str,
        verdict: Verdict,
        reason: str,
        round_obj: _Round | None,
        *,
        worker_id: str | None = None,
        bucket: int | None = None,
        state: dict | None = None,
    ) -> None:
        self._diag.record(
            Diagnostic(
                request_id=request_id,
                verdict=verdict,
                reason=reason,
                round_index=None if round_obj is None else round_obj.index,
                bucket_index=bucket,
                generation=None if round_obj is None else round_obj.generation,
                worker_id=worker_id,
                key_state=state or {},
            )
        )
