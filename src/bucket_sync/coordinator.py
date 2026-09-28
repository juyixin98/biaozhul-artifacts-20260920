"""Coordinator: the synchronous round state machine and commit barrier.

The weighted math lives in :mod:`bucket_sync.reduction`; value types and
failure categories live in :mod:`bucket_sync.round_types`.  This module
enforces the four edge-case guarantees:

1. **Fixed order and bucket layout, explicit placeholders.**  Layout comes
   from the immutable :class:`BucketLayout`; non-trainable slots are masked,
   never shifted away.
2. **True sample-count weighting.**  Workers send gradient sums plus true
   sample counts; :mod:`bucket_sync.reduction` divides by their sum, giving
   exactly the joint-batch mean for unequal batches.
3. **No early updates.**  Bucket completion only records data; model
   parameters are swapped exactly once, at commit.  Every submission must
   carry the round's base-generation token, so a gradient computed against
   newer weights is rejected.
4. **Lost participants reject the round.**  A participant that goes dead
   aborts the round; commit is refused and late submissions are rejected.
"""

from __future__ import annotations

import threading
import time
from typing import Callable, Dict, List, Optional, Sequence, Set, Tuple

import numpy as np

from bucket_sync.bucketing import BucketLayout
from bucket_sync.diagnostics import Diagnostics
from bucket_sync.graph import ParameterGraph
from bucket_sync.reduction import active_participants, reduce_buckets
from bucket_sync.round_types import (
    BucketEvidence,
    CommitReport,
    Participant,
    RejectReason,
    Round,
    RoundStatus,
    SubmissionResult,
    Worker,
    params_token,
)
from bucket_sync.training import ModelState

__all__ = [
    "Coordinator",
    "RejectReason",
    "RoundStatus",
    "SubmissionResult",
    "CommitReport",
    "BucketEvidence",
]


class Coordinator:
    """Thread-safe coordinator owning graph, layout, model state, and rounds."""

    def __init__(
        self,
        graph: ParameterGraph,
        layout: BucketLayout,
        model: ModelState,
        lr: float,
        diagnostics: Optional[Diagnostics] = None,
        *,
        time_func: Callable[[], float] = time.monotonic,
        heartbeat_timeout: float = 5.0,
        allow_partial_coverage: bool = False,
    ) -> None:
        if lr <= 0:
            raise ValueError(f"lr must be positive, got {lr!r}")
        if layout.graph is not graph:
            raise ValueError("layout must be built from the given graph")
        self._lock = threading.RLock()
        self._graph = graph
        self._layout = layout
        self._model = model
        self._lr = float(lr)
        self._diag = diagnostics or Diagnostics()
        self._time = time_func
        self._heartbeat_timeout = float(heartbeat_timeout)
        # Strict synchronous semantics by default: every active worker must
        # cover every trainable slot or the commit is refused.
        self._allow_partial_coverage = bool(allow_partial_coverage)
        self._workers: Dict[str, Worker] = {}
        self._round: Optional[Round] = None
        self._next_round_id = 1
        self._history: List[Round] = []

    # ---- properties & introspection --------------------------------

    @property
    def diagnostics(self) -> Diagnostics:
        return self._diag

    @property
    def layout(self) -> BucketLayout:
        return self._layout

    def current_generation(self) -> int:
        with self._lock:
            return self._model.step

    def base_token(self) -> str:
        with self._lock:
            return params_token(self._model.step, self._model.params)

    def snapshot_params(self) -> Dict[str, np.ndarray]:
        """Deep-copy snapshot of current parameters (immutable to callers)."""
        with self._lock:
            return {k: v.copy() for k, v in self._model.params.items()}

    def alive_worker_ids(self) -> List[str]:
        with self._lock:
            return [w.worker_id for w in self._workers.values() if w.alive]

    def is_alive(self, worker_id: str) -> bool:
        with self._lock:
            worker = self._workers.get(worker_id)
            return bool(worker and worker.alive)

    # ---- worker registry & liveness --------------------------------

    def register_worker(self, worker_id: str) -> None:
        with self._lock:
            if worker_id in self._workers:
                existing = self._workers[worker_id]
                if existing.alive:
                    # Idempotent: a live re-register (e.g. client retry) is a no-op.
                    existing.last_heartbeat = self._time()
                    return
                # A dead worker may be re-registered only between rounds; an
                # aborted round referencing it has already been retired.
                if self._round is not None and self._round.status == RoundStatus.OPEN:
                    raise ValueError(
                        f"cannot revive worker {worker_id!r} while a round is open"
                    )
                existing.alive = True
                existing.lost_reason = None
                existing.last_heartbeat = self._time()
                self._diag.emit(
                    round_id=None,
                    worker_id=worker_id,
                    outcome="info",
                    reason="worker_revived",
                    detail={
                        "alive_workers": sorted(
                            w.worker_id for w in self._workers.values() if w.alive
                        )
                    },
                )
                return
            self._workers[worker_id] = Worker(
                worker_id=worker_id, last_heartbeat=self._time()
            )
            self._diag.emit(
                round_id=self._round.round_id if self._round else None,
                worker_id=worker_id,
                outcome="info",
                reason="worker_registered",
                detail={"alive_workers": sorted(self._workers)},
            )

    def heartbeat(self, worker_id: str) -> None:
        with self._lock:
            self._require_worker(worker_id).last_heartbeat = self._time()

    def mark_worker_lost(self, worker_id: str, reason: str = "reported_dead") -> None:
        """Mark a worker dead; if it participates in the open round, abort it."""
        with self._lock:
            worker = self._require_worker(worker_id)
            if not worker.alive:
                return
            worker.alive = False
            worker.lost_reason = reason
            self._diag.emit(
                round_id=self._round.round_id if self._round else None,
                worker_id=worker_id,
                outcome="rejected",
                reason=RejectReason.WORKER_LOST.value,
                detail={"why": reason},
            )
            if self._round is not None and self._round.status == RoundStatus.OPEN:
                if worker_id in self._round.participants:
                    self._abort_round_locked(
                        RejectReason.WORKER_LOST.value,
                        {"lost_worker": worker_id, "why": reason},
                    )

    def check_liveness(self) -> Set[str]:
        """Expire workers whose heartbeat is stale; abort round if needed."""
        with self._lock:
            now = self._time()
            lost = {
                w.worker_id
                for w in self._workers.values()
                if w.alive and now - w.last_heartbeat > self._heartbeat_timeout
            }
            for worker_id in lost:
                self.mark_worker_lost(worker_id, reason="heartbeat_timeout")
            return lost

    # ---- round lifecycle -------------------------------------------

    def begin_round(
        self,
        participant_ids: Sequence[str],
        *,
        skipped_ids: Optional[Sequence[str]] = None,
    ) -> Dict[str, object]:
        """Open a new round over currently-alive participants."""
        with self._lock:
            if self._round is not None and self._round.status == RoundStatus.OPEN:
                raise RuntimeError(
                    f"round {self._round.round_id} is still open; commit or abort first"
                )
            skipped = set(skipped_ids or ())
            ids = list(participant_ids)
            if not ids:
                raise ValueError("begin_round requires at least one participant")
            if len(set(ids)) != len(ids):
                raise ValueError(f"duplicate participants: {ids!r}")
            unknown = [w for w in ids if w not in self._workers]
            if unknown:
                raise ValueError(f"unregistered workers: {unknown!r}")
            dead = [w for w in ids if not self._workers[w].alive]
            if dead:
                raise ValueError(f"dead workers cannot participate: {dead!r}")
            unknown_skip = [w for w in skipped if w not in ids]
            if unknown_skip:
                raise ValueError(
                    f"skipped workers must be participants: {unknown_skip}"
                )

            round_id = self._next_round_id
            self._next_round_id += 1
            generation = self._model.step
            base = {k: v.copy() for k, v in self._model.params.items()}
            token = params_token(generation, base)
            participants = {
                wid: Participant(worker_id=wid, skipped=wid in skipped) for wid in ids
            }
            self._round = Round(
                round_id=round_id,
                generation=generation,
                base_params=base,
                base_token=token,
                participants=participants,
            )
            for worker in self._workers.values():
                worker.last_heartbeat = self._time()
            self._diag.emit(
                round_id=round_id,
                worker_id=None,
                outcome="info",
                reason="round_began",
                detail={
                    "generation": generation,
                    "base_token": token,
                    "participants": ids,
                    "skipped": sorted(skipped),
                    "bucket_count": self._layout.bucket_count(),
                },
            )
            return {
                "round_id": round_id,
                "generation": generation,
                "base_token": token,
                "participants": ids,
                "skipped": sorted(skipped),
                "bucket_count": self._layout.bucket_count(),
                "base_params": base,
            }

    def abort_round(self, reason: str = "explicit_abort") -> None:
        with self._lock:
            self._abort_round_locked(reason, {"requested_by": "driver"})

    def round_descriptor(self) -> Optional[Dict[str, object]]:
        with self._lock:
            if self._round is None:
                return None
            return {
                "round_id": self._round.round_id,
                "generation": self._round.generation,
                "base_token": self._round.base_token,
                "status": self._round.status.value,
                "abort_reason": self._round.abort_reason,
                "base_params": {
                    k: v.copy() for k, v in self._round.base_params.items()
                },
                "participants": sorted(self._round.participants),
                "skipped": sorted(
                    p.worker_id
                    for p in self._round.participants.values()
                    if p.skipped
                ),
            }

    # ---- bucket submission -----------------------------------------

    def submit_bucket(
        self,
        round_id: int,
        worker_id: str,
        bucket_index: int,
        segment: np.ndarray,
        n_samples: int,
        base_token: str,
        present_mask: np.ndarray,
    ) -> SubmissionResult:
        """Submit one gradient-sum bucket; validate before any state change.

        ``present_mask`` is explicit: True says "this trainable slot carries
        a gradient I computed"; a missing gradient therefore cannot
        masquerade as a zero, and a placeholder cannot claim coverage.
        """
        with self._lock:
            rnd = self._round
            failure = self._validate_submission(
                rnd,
                round_id,
                worker_id,
                bucket_index,
                segment,
                n_samples,
                base_token,
                present_mask,
            )
            if failure is not None:
                code, why, detail = failure
                event = self._diag.emit(
                    round_id=round_id if rnd else None,
                    worker_id=worker_id,
                    outcome="rejected",
                    reason=code,
                    detail=detail,
                )
                return SubmissionResult(
                    accepted=False,
                    reason=code,
                    record_id=event.record_id,
                    bucket_index=bucket_index,
                    bucket_complete=False,
                    all_buckets_received=False,
                )
            assert rnd is not None
            part = rnd.participants[worker_id]
            bucket = self._layout.buckets[bucket_index]
            values = np.asarray(segment, dtype=np.float64).reshape(-1).copy()
            mask = np.asarray(present_mask, dtype=bool).reshape(-1).copy()
            ph = self._layout.placeholder_mask()[bucket.start : bucket.end]
            values[ph] = 0.0
            mask[ph] = False
            part.buckets[bucket_index] = values
            part.coverage[bucket_index] = mask
            if part.n_samples is None:
                part.n_samples = int(n_samples)
            complete = self._bucket_complete_locked(rnd, bucket_index)
            all_in = not self._missing_buckets_locked(rnd)
            params_unchanged = self._model.step == rnd.generation
            event = self._diag.emit(
                round_id=round_id,
                worker_id=worker_id,
                outcome="accepted",
                reason="bucket_received",
                detail={
                    "bucket_index": bucket_index,
                    "n_samples": int(n_samples),
                    "bucket_complete": complete,
                    "all_buckets_received": all_in,
                    "model_generation": self._model.step,
                    "round_generation": rnd.generation,
                    "weights_swapped": not params_unchanged,
                    "segment_norm": round(float(np.linalg.norm(values)), 6),
                },
            )
            return SubmissionResult(
                accepted=True,
                reason=None,
                record_id=event.record_id,
                bucket_index=bucket_index,
                bucket_complete=complete,
                all_buckets_received=all_in,
            )

    def skip_round(self, worker_id: str) -> None:
        """An explicitly idle participant contributes no samples this round."""
        with self._lock:
            rnd = self._require_open_round()
            if worker_id not in rnd.participants:
                raise KeyError(worker_id)
            rnd.participants[worker_id].skipped = True
            self._diag.emit(
                round_id=rnd.round_id,
                worker_id=worker_id,
                outcome="info",
                reason="worker_skipped_round",
                detail={"participants": sorted(rnd.participants)},
            )

    # ---- commit / reduction ----------------------------------------

    def commit_round(self) -> CommitReport:
        """Barrier check, weighted reduction, and atomic weight swap."""
        with self._lock:
            rnd = self._round
            if rnd is None:
                return self._commit_failure(
                    None, "rejected", RejectReason.ROUND_NOT_OPEN.value, {}
                )
            if rnd.status == RoundStatus.ABORTED:
                return self._commit_failure(
                    rnd,
                    "rejected",
                    RejectReason.ROUND_ABORTED.value,
                    {"abort_reason": rnd.abort_reason},
                )
            if rnd.status == RoundStatus.COMMITTED:
                return self._commit_failure(
                    rnd, "rejected", RejectReason.ROUND_COMMITTED.value, {}
                )

            missing = self._missing_buckets_locked(rnd)
            if missing:
                return self._commit_failure(
                    rnd,
                    "undecided",
                    RejectReason.INCOMPLETE_SUBMISSION.value,
                    {
                        "missing_buckets": missing,
                        "received": self._received_summary(rnd),
                    },
                    dedupe=True,
                )

            gaps = self._coverage_gaps_locked(rnd)
            if gaps and not self._allow_partial_coverage:
                return self._commit_failure(
                    rnd,
                    "undecided",
                    RejectReason.MISSING_GRADIENT.value,
                    {
                        "slots_without_full_coverage": gaps,
                        "policy": "strict: every active worker must cover every "
                        "trainable slot",
                    },
                    dedupe=True,
                )

            return self._reduce_and_commit_locked(rnd, gaps)

    # ---- validation -------------------------------------------------

    def _validate_submission(
        self,
        rnd: Optional[Round],
        round_id: int,
        worker_id: str,
        bucket_index: int,
        segment: np.ndarray,
        n_samples: int,
        base_token: str,
        present_mask: np.ndarray,
    ) -> Optional[Tuple[str, str, Dict[str, object]]]:
        if worker_id not in self._workers:
            return RejectReason.UNKNOWN_WORKER.value, "worker never registered", {}
        if not self._workers[worker_id].alive:
            return (
                RejectReason.WORKER_LOST.value,
                "worker marked dead; round rejects its submissions",
                {"lost_why": self._workers[worker_id].lost_reason},
            )
        if rnd is None:
            return RejectReason.ROUND_NOT_OPEN.value, "no round open", {}
        if rnd.status == RoundStatus.ABORTED:
            return (
                RejectReason.ROUND_ABORTED.value,
                "round aborted; submissions are refused",
                {"abort_reason": rnd.abort_reason},
            )
        if rnd.status == RoundStatus.COMMITTED:
            return RejectReason.ROUND_COMMITTED.value, "round already committed", {}
        if round_id != rnd.round_id:
            return (
                RejectReason.WRONG_ROUND.value,
                f"submission targets round {round_id}, open round is {rnd.round_id}",
                {"open_round": rnd.round_id},
            )
        if worker_id not in rnd.participants:
            return RejectReason.NOT_PARTICIPANT.value, "not a participant", {}
        part = rnd.participants[worker_id]
        if part.skipped:
            return RejectReason.WORKER_SKIPPED_ROUND.value, "worker declared idle", {}
        if base_token != rnd.base_token:
            return (
                RejectReason.WRONG_BASE_GENERATION.value,
                "gradient computed against different base weights than the round",
                {"expected_token": rnd.base_token, "got_token": base_token},
            )
        if not 0 <= bucket_index < self._layout.bucket_count():
            return (
                RejectReason.UNKNOWN_BUCKET.value,
                f"bucket {bucket_index} not in layout",
                {"bucket_count": self._layout.bucket_count()},
            )
        if bucket_index in part.buckets:
            return (
                RejectReason.DUPLICATE_BUCKET.value,
                "bucket already submitted by this worker",
                {"bucket_index": bucket_index},
            )
        arr = np.asarray(segment)
        bucket = self._layout.buckets[bucket_index]
        if arr.shape != (bucket.size,):
            return (
                RejectReason.BAD_SHAPE.value,
                f"expected segment shape ({bucket.size},), got {arr.shape}",
                {"bucket_index": bucket_index},
            )
        if not (
            np.issubdtype(arr.dtype, np.floating)
            or np.issubdtype(arr.dtype, np.integer)
        ):
            return RejectReason.BAD_SHAPE.value, "segment must be numeric", {}
        seg64 = np.asarray(arr, dtype=np.float64).reshape(-1)
        if not bool(np.all(np.isfinite(seg64))):
            return RejectReason.NON_FINITE.value, "NaN/Inf in gradient segment", {}
        if not isinstance(n_samples, int) or n_samples <= 0:
            return (
                RejectReason.INVALID_SAMPLE_COUNT.value,
                f"n_samples must be positive int, got {n_samples!r}",
                {},
            )
        if part.n_samples is not None and part.n_samples != n_samples:
            return (
                RejectReason.SAMPLE_COUNT_MISMATCH.value,
                "a worker's sample count must be identical across all buckets",
                {"first": part.n_samples, "got": n_samples},
            )
        ph_mask = self._layout.placeholder_mask()[bucket.start : bucket.end]
        if np.any(ph_mask) and np.any(seg64[ph_mask] != 0.0):
            return (
                RejectReason.PLACEHOLDER_NONZERO.value,
                "non-trainable placeholder slots must be submitted as zeros",
                {"bucket_index": bucket_index},
            )
        mask = np.asarray(present_mask)
        if mask.shape != (bucket.size,):
            return (
                RejectReason.BAD_MASK.value,
                f"present_mask shape {mask.shape} != ({bucket.size},)",
                {"bucket_index": bucket_index},
            )
        if mask.dtype != bool:
            return RejectReason.BAD_MASK.value, "present_mask must be boolean", {}
        mask = mask.reshape(-1)
        if np.any(mask & ph_mask):
            return (
                RejectReason.BAD_MASK.value,
                "present_mask claims coverage on a non-trainable placeholder",
                {"bucket_index": bucket_index},
            )
        trainable = ~ph_mask
        if np.any((~mask) & trainable & (seg64 != 0.0)):
            return (
                RejectReason.BAD_MASK.value,
                "nonzero gradient on a slot the worker did not mark present; "
                "missing gradients must be declared, not sent as zeros",
                {"bucket_index": bucket_index},
            )
        if not np.any(mask):
            return (
                RejectReason.MISSING_GRADIENT.value,
                "bucket covers no trainable slots for this worker",
                {"bucket_index": bucket_index},
            )
        return None

    # ---- coverage helpers ------------------------------------------

    def _bucket_complete_locked(self, rnd: Round, bucket_index: int) -> bool:
        return all(
            bucket_index in p.buckets
            for p in active_participants(rnd.participants)
        )

    def _missing_buckets_locked(self, rnd: Round) -> Dict[str, List[int]]:
        missing: Dict[str, List[int]] = {}
        for part in active_participants(rnd.participants):
            gaps = [b.index for b in self._layout.buckets if b.index not in part.buckets]
            if gaps:
                missing[part.worker_id] = gaps
        return missing

    def _received_summary(self, rnd: Round) -> Dict[str, int]:
        return {
            p.worker_id: len(p.buckets) for p in active_participants(rnd.participants)
        }

    def _coverage_gaps_locked(self, rnd: Round) -> Dict[str, object]:
        """Per-bucket slots where active-worker coverage is not complete."""
        active = active_participants(rnd.participants)
        n_active = len(active)
        gaps: Dict[str, object] = {}
        for bucket in self._layout.buckets:
            counts = np.zeros(bucket.size, dtype=np.int64)
            for part in active:
                counts += part.coverage[bucket.index].astype(np.int64)
            ph = self._layout.placeholder_mask()[bucket.start : bucket.end]
            trainable_idx = np.flatnonzero(~ph)
            partial = [int(j) for j in trainable_idx if 0 < counts[j] < n_active]
            uncovered = [int(j) for j in trainable_idx if counts[j] == 0]
            if partial or uncovered:
                gaps[f"bucket_{bucket.index}"] = {
                    "partial_slots": partial,
                    "uncovered_slots": uncovered,
                    "active_workers": n_active,
                    "min_cover_count": (
                        int(counts[~ph].min()) if len(trainable_idx) else n_active
                    ),
                }
        return gaps

    # ---- commit internals -------------------------------------------

    def _reduce_and_commit_locked(
        self, rnd: Round, coverage_gaps: Dict[str, object]
    ) -> CommitReport:
        active = active_participants(rnd.participants)
        reduced, evidence = reduce_buckets(
            self._layout, active, rnd.generation, coverage_gaps
        )
        rnd.reduced = reduced
        rnd.evidence = evidence

        flat = self._layout.zeros_flat()
        for bucket in self._layout.buckets:
            flat[bucket.start : bucket.end] = reduced[bucket.index]
        grads = self._layout.unpack_flat(flat)
        new_params = {
            name: (
                self._model.params[name] - self._lr * grads[name]
                if self._graph.node(name).trainable
                else self._model.params[name].copy()
            )
            for name in self._graph.param_names()
        }
        self._model = self._model.copy_with(new_params)
        rnd.status = RoundStatus.COMMITTED
        self._history.append(rnd)

        total_samples = int(sum(p.n_samples or 0 for p in active))
        event = self._diag.emit(
            round_id=rnd.round_id,
            worker_id=None,
            outcome="accepted",
            reason="round_committed",
            detail={
                "generation_before": rnd.generation,
                "generation_after": self._model.step,
                "total_samples": total_samples,
                "samples_per_worker": {p.worker_id: p.n_samples for p in active},
                "bucket_generations": [
                    {"bucket": e.bucket_index, "generation": e.generation}
                    for e in evidence
                ],
            },
        )
        return CommitReport(
            outcome="committed",
            reason=None,
            record_id=event.record_id,
            round_id=rnd.round_id,
            generation_before=rnd.generation,
            generation_after=self._model.step,
            bucket_evidence=evidence,
            detail={"total_samples": total_samples},
        )

    def _commit_failure(
        self,
        rnd: Optional[Round],
        outcome: str,
        reason: str,
        detail: Dict[str, object],
        *,
        dedupe: bool = False,
    ) -> CommitReport:
        round_id = rnd.round_id if rnd else None
        # While a round is undecided the driver polls commit repeatedly; log
        # the reason once and hand back the same record id on later probes.
        if dedupe and rnd is not None and reason in rnd.undecided_logged:
            record_id = rnd.undecided_logged[reason]
        else:
            event = self._diag.emit(
                round_id=round_id,
                worker_id=None,
                outcome=outcome,
                reason=reason,
                detail=detail,
            )
            record_id = event.record_id
            if dedupe and rnd is not None:
                rnd.undecided_logged[reason] = record_id
        return CommitReport(
            outcome=outcome,
            reason=reason,
            record_id=record_id,
            round_id=round_id if round_id is not None else -1,
            generation_before=rnd.generation if rnd else self._model.step,
            generation_after=None,
            bucket_evidence=(),
            detail=dict(detail),
        )

    def _abort_round_locked(self, reason: str, detail: Dict[str, object]) -> None:
        if self._round is None:
            return
        self._round.status = RoundStatus.ABORTED
        self._round.abort_reason = reason
        self._diag.emit(
            round_id=self._round.round_id,
            worker_id=None,
            outcome="rejected",
            reason="round_aborted",
            detail={"abort_reason": reason, **detail},
        )
        self._history.append(self._round)
        # Keep the aborted round as the current round (not None): late
        # submissions must be told ROUND_ABORTED, and its status stays
        # observable.  begin_round overwrites any non-open round.

    def _require_open_round(self) -> Round:
        if self._round is None:
            raise RuntimeError("no round open")
        if self._round.status != RoundStatus.OPEN:
            raise RuntimeError(f"round is {self._round.status.value}")
        return self._round

    def _require_worker(self, worker_id: str) -> Worker:
        try:
            return self._workers[worker_id]
        except KeyError:
            raise KeyError(f"unknown worker {worker_id!r}") from None
