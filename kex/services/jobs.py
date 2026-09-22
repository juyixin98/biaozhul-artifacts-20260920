"""Extraction job orchestration and the local worker loop.

Durability & concurrency model
------------------------------
* Work is persisted as ``extraction_jobs`` + ``job_items``. An item is the
  durable checkpoint: its ``stage``/``status`` columns survive crashes and
  restarts; a worker resumes whatever the previous attempt did not finish.
* At most ``KEX_MAX_WORKERS`` (=2) workers cluster-wide can hold leases.
  Membership is a ``worker_heartbeats`` table. Leases on items are short
  (``KEX_WORKER_LEASE_SECONDS``); an expired lease makes an item claimable
  again, which implements restart recovery.
* Claims are a single conditional UPDATE, atomic under SQLite's writer lock,
  so two workers never process the same item.
* Each item binds the document SHA-256 and rule version. Workers re-verify
  digest and document existence before writing: a deleted document never
  gets re-extracted (delete race), and an old job's late result can never
  land against newer rules.
"""
from __future__ import annotations

import datetime as _dt
import os
import random
import threading
import time
import uuid
from dataclasses import dataclass

from sqlalchemy import and_, delete, func, or_, select, update
from sqlalchemy.exc import IntegrityError, OperationalError
from sqlalchemy.orm import Session

from ..extraction import Mention, compile_snapshot
from ..models import (
    Document,
    Entity,
    ExtractionJob,
    IndexGeneration,
    JobItem,
    RuleVersion,
    WorkspaceDocument,
    WorkspaceState,
    WorkerHeartbeat,
)
from . import search_index


def _utcnow() -> _dt.datetime:
    # Naive UTC to match SQLite DATETIME storage (see models._utcnow).
    return _dt.datetime.now(_dt.timezone.utc).replace(tzinfo=None)


# --------------------------------------------------------------------------- #
# Job creation
# --------------------------------------------------------------------------- #
def create_incremental_job(
    db: Session,
    *,
    workspace_id: int,
    rule_version_id: int,
    ws_docs: list[WorkspaceDocument],
    active_generation_id: int | None,
    building_generation_ids: list[int] | None,
    digest: str,
) -> ExtractionJob:
    building_generation_ids = [
        gid for gid in (building_generation_ids or []) if gid != active_generation_id
    ]
    job = ExtractionJob(
        workspace_id=workspace_id,
        kind="incremental",
        target_rule_version_id=rule_version_id,
        target_index_generation_id=active_generation_id,
        status="queued",
    )
    db.add(job)
    db.flush()
    for ws_doc in ws_docs:
        target_gens = [active_generation_id, *building_generation_ids]
        for gen_id in target_gens:
            db.add(
                JobItem(
                    job_id=job.id,
                    workspace_id=workspace_id,
                    workspace_document_id=ws_doc.id,
                    document_sha256=digest,
                    rule_version_id=rule_version_id,
                    index_generation_id=gen_id,
                    stage="queued",
                    status="queued",
                )
            )
    return job


def create_rebuild_job(
    db: Session, workspace_id: int, rule_version_id: int
) -> tuple[ExtractionJob, IndexGeneration]:
    """Publish new rules -> build a fresh generation for them.

    The active rule pointer moves immediately (new API reads use new
    immutable rules); search keeps reading the previous active generation
    until every rebuild item completes, then generations swap atomically.
    """
    state = db.get(WorkspaceState, workspace_id)
    gen = IndexGeneration(
        workspace_id=workspace_id,
        generation=state.next_index_generation,
        rule_version_id=rule_version_id,
        status="building",
    )
    db.add(gen)
    db.flush()
    state.next_index_generation += 1

    job = ExtractionJob(
        workspace_id=workspace_id,
        kind="rebuild",
        target_rule_version_id=rule_version_id,
        target_index_generation_id=gen.id,
        status="queued",
    )
    db.add(job)
    db.flush()

    ws_docs = db.scalars(
        select(WorkspaceDocument).where(
            WorkspaceDocument.workspace_id == workspace_id
        )
    ).all()
    for ws_doc in ws_docs:
        doc = db.get(Document, ws_doc.document_id)
        db.add(
            JobItem(
                job_id=job.id,
                workspace_id=workspace_id,
                workspace_document_id=ws_doc.id,
                document_sha256=doc.sha256,
                rule_version_id=rule_version_id,
                index_generation_id=gen.id,
                stage="queued",
                status="queued",
            )
        )
    return job, gen


def requeue_failed(db: Session, workspace_id: int, job_id: int) -> ExtractionJob:
    job = db.get(ExtractionJob, job_id)
    if job is None or job.workspace_id != workspace_id:
        raise LookupError("job not found")
    if job.status not in ("failed", "partial"):
        return job
    items = db.scalars(
        select(JobItem).where(JobItem.job_id == job.id, JobItem.status == "failed")
    ).all()
    for item in items:
        item.status = "queued"
        item.stage = "queued"
        item.leased_by = None
        item.leased_until = None
    job.status = "queued"
    job.error = None
    job.finished_at = None
    db.flush()
    return job


# --------------------------------------------------------------------------- #
# Worker registry (cluster-wide cap)
# --------------------------------------------------------------------------- #
def register_worker(
    db: Session, worker_id: str, *, max_workers: int, lease_seconds: int
) -> bool:
    """Register heartbeat row if the cap allows. True when this worker may run."""
    cutoff = _utcnow() - _dt.timedelta(seconds=lease_seconds * 2)
    db.execute(delete(WorkerHeartbeat).where(WorkerHeartbeat.last_beat < cutoff))
    existing = db.get(WorkerHeartbeat, worker_id)
    if existing is not None:
        existing.last_beat = _utcnow()
        return True
    total = db.scalar(select(func.count()).select_from(WorkerHeartbeat)) or 0
    if total >= max_workers:
        return False
    db.add(WorkerHeartbeat(worker_id=worker_id, last_beat=_utcnow()))
    return True


def beat(db: Session, worker_id: str) -> None:
    row = db.get(WorkerHeartbeat, worker_id)
    if row is not None:
        row.last_beat = _utcnow()


def deregister(db: Session, worker_id: str) -> None:
    db.execute(delete(WorkerHeartbeat).where(WorkerHeartbeat.worker_id == worker_id))


# --------------------------------------------------------------------------- #
# Item claim & processing
# --------------------------------------------------------------------------- #
@dataclass
class ClaimedItem:
    item: JobItem
    job: ExtractionJob


def claim_item(db: Session, worker_id: str, lease_seconds: int) -> ClaimedItem | None:
    """Atomically claim one queued/expired item across the whole cluster.

    Ordering: oldest job first, oldest item first — FIFO, but rebuild items
    and incremental items of the same job keep their enqueue order.

    Caller MUST hold the process-local write lock (see db.write_lock) so the
    candidate read and conditional update cannot interleave with another
    in-process worker; the other OS process is serialized by SQLite's file
    lock, with item-level retry covering the rare conflict.
    """
    now = _utcnow()
    lease_until = now + _dt.timedelta(seconds=lease_seconds)

    candidate = db.execute(
        select(JobItem)
        .join(ExtractionJob, ExtractionJob.id == JobItem.job_id)
        .where(
            or_(
                JobItem.status == "queued",
                and_(JobItem.status == "running", JobItem.leased_until < now),
            ),
        )
        .order_by(JobItem.job_id, JobItem.id)
        .limit(1)
    ).scalar_one_or_none()
    if candidate is None:
        return None

    # Conditional UPDATE: only wins if nobody else raced us. SQLite takes a
    # write lock for this statement, making the check-and-set atomic.
    result = db.execute(
        update(JobItem)
        .where(
            JobItem.id == candidate.id,
            or_(
                JobItem.status == "queued",
                and_(JobItem.status == "running", JobItem.leased_until < now),
            ),
        )
        .values(
            status="running",
            leased_by=worker_id,
            leased_until=lease_until,
            stage="extract",
            attempts=JobItem.attempts + 1,
        )
    )
    db.commit()
    if result.rowcount != 1:
        return None

    db.expire_all()
    item = db.get(JobItem, candidate.id)
    job = db.get(ExtractionJob, item.job_id)
    if job.status == "queued":
        job.status = "running"
        db.commit()
    return ClaimedItem(item=item, job=job)


def process_item(db: Session, claimed: ClaimedItem) -> str:
    """Execute one checkpointed item. Returns final item status.

    A :class:`_WriteConflict` propagates to the worker loop for an
    optimistic item-level retry; all other exceptions mark the item failed
    with document + stage diagnostics.
    """
    item = claimed.item
    try:
        status = _process_item_inner(db, item, claimed.job)
        item.last_error = None
        item.leased_by = None
        item.leased_until = None
        # No commit here: the caller commits this together with job
        # finalization / generation activation in ONE transaction, so an
        # observer can never see a drained queue without the activated gen.
        db.flush()
        return status
    except _ItemSkip as exc:
        # Document disappeared / stale context: item is harmlessly retired.
        db.rollback()
        item = db.get(JobItem, claimed.item.id)
        if item is not None:
            item.status = "skipped"
            item.stage = "done"
            item.last_error = str(exc)
            item.leased_by = None
            item.leased_until = None
            db.commit()
        return "skipped"
    except (OperationalError, IntegrityError) as exc:
        # SQLITE_BUSY or a racing UNIQUE insert: let the worker retry.
        db.rollback()
        raise _WriteConflict(exc) from exc
    except Exception as exc:  # noqa: BLE001
        reached_stage = getattr(item, "stage", "extract")
        db.rollback()
        item = db.get(JobItem, claimed.item.id)
        if item is not None:
            item.status = "failed"
            # Persist the precise stage the failure was reached at inside
            # this single transaction (extract/index), so operators can
            # locate it even though the work itself rolled back.
            item.stage = reached_stage
            item.last_error = f"{type(exc).__name__}: {exc}"[:2000]
            item.leased_by = None
            item.leased_until = None
            db.commit()
        return "failed"


class _ItemSkip(Exception):
    pass


class _WriteConflict(Exception):
    """Raised when SQLite write serialization forces an item-level retry."""

    def __init__(self, original: Exception) -> None:
        self.original = original
        super().__init__(f"write conflict: {original!r}")


def _process_item_inner(db: Session, item: JobItem, job: ExtractionJob) -> str:
    """Process one claimed item inside a SINGLE short write transaction.

    The whole extract+index sequence is one transaction with no intermediate
    commits. Interleaving reads from one connection with another process's
    writes in WAL mode can otherwise deadlock on SQLite's RESERVED lock for
    the busy_timeout window. Failure localization (which stage) is preserved
    by recording ``stage`` on the row inside the same failed transaction's
    error path — see :func:`process_item`.
    """
    # ---- STAGE GUARDS: verify the immutable context still holds ----------
    ws_doc = db.get(WorkspaceDocument, item.workspace_document_id)
    if ws_doc is None or ws_doc.workspace_id != item.workspace_id:
        # Delete race: the document was deleted after enqueue. No writes.
        raise _ItemSkip("workspace document no longer exists")
    doc = db.get(Document, ws_doc.document_id)
    if doc is None or doc.sha256 != item.document_sha256:
        # Content-addressing guarantees this can't mutate; guard anyway.
        raise _ItemSkip("document digest mismatch or blob missing")

    rules_row = db.get(RuleVersion, item.rule_version_id)
    if rules_row is None or rules_row.workspace_id != item.workspace_id:
        raise _ItemSkip("rule version missing or foreign to workspace")
    compiled = compile_snapshot(rules_row.snapshot)

    # ---- STAGE: extract (idempotent via unique mention constraint) -------
    item.stage = "extract"
    _extract_persist(db, item, ws_doc, doc, compiled.extract(doc.content))

    # ---- STAGE: index (idempotent via stat existence) --------------------
    if item.index_generation_id is not None:
        gen = db.get(IndexGeneration, item.index_generation_id)
        if gen is None or gen.workspace_id != item.workspace_id:
            raise _ItemSkip("index generation vanished")
        item.stage = "index"
        search_index.index_document(db, gen.id, ws_doc, doc.content)

    item.stage = "done"
    item.status = "done"
    return "done"


def _extract_persist(db: Session, item: JobItem, ws_doc: WorkspaceDocument,
                     doc: Document, mentions: list[Mention]) -> None:
    """Insert mentions, skipping any row already present (retry-safe)."""
    existing_keys = {
        (r[0], r[1], r[2])
        for r in db.execute(
            select(Entity.start_char, Entity.end_char, Entity.entity_type).where(
                Entity.workspace_id == item.workspace_id,
                Entity.workspace_document_id == ws_doc.id,
                Entity.rule_version_id == item.rule_version_id,
            )
        ).all()
    }
    for m in mentions:
        key = (m.start_char, m.end_char, m.entity_type)
        if key in existing_keys:
            # Same evidence already produced — never duplicate on rerun.
            continue
        db.add(
            Entity(
                workspace_id=item.workspace_id,
                workspace_document_id=ws_doc.id,
                document_id=doc.id,
                document_sha256=doc.sha256,
                rule_version_id=item.rule_version_id,
                entity_type=m.entity_type,
                text=m.text,
                canonical_name=m.canonical_name,
                start_char=m.start_char,
                end_char=m.end_char,
                matched_rule=m.matched_rule,
            )
        )
        existing_keys.add(key)
    db.flush()


# --------------------------------------------------------------------------- #
# Job finalization / rebuild activation
# --------------------------------------------------------------------------- #
def maybe_finalize_job(db: Session, job_id: int) -> str | None:
    """Recompute a job's status from its items. Returns new status.

    This function never commits on its own: all state changes (job status +
    generation activation) are flushed and must be committed by the OUTER
    transaction. That keeps "all items done" and "new generation active"
    atomic, so an observer can never see a drained queue still serving the
    old generation.

    A rebuild job whose own items are all done may still have to wait for an
    incremental item (an upload that raced the rebuild) to finish writing the
    same generation. In that case the job is already ``succeeded`` while its
    generation is still ``building``; when the racer finishes it re-invokes
    this function and idempotently performs the activation.
    """
    job = db.get(ExtractionJob, job_id)
    if job is None:
        return None
    if job.status == "partial":
        return job.status
    if job.status == "succeeded":
        if job.kind == "rebuild":
            _activate_any_ready_building(db, job.workspace_id)
            db.flush()
        return job.status

    rows = db.execute(
        select(JobItem.status, JobItem.id).where(JobItem.job_id == job_id)
    ).all()
    statuses = [r[0] for r in rows]
    if not statuses:
        job.status = "succeeded"
        job.finished_at = _utcnow()
        db.flush()
        return job.status
    if any(s in ("queued", "running") for s in statuses):
        return None  # still in flight

    failed = [s for s in statuses if s == "failed"]
    new_status = "partial" if failed else "succeeded"
    job.status = new_status
    job.finished_at = _utcnow()
    if failed:
        job.error = f"{len(failed)} item(s) failed; POST /jobs/{{id}}/retry"

    # Activation checks that NO item from ANY job is still pending for the
    # generation; the worker also nudges building generations after every
    # item, so whichever item lands last triggers activation atomically with
    # the "job succeeded" write.
    if job.kind == "rebuild" and new_status == "succeeded":
        _activate_rebuild(db, job)
    db.flush()
    return new_status


def _generation_ready(db: Session, generation_id: int) -> bool:
    pending = db.scalar(
        select(func.count(JobItem.id)).where(
            JobItem.index_generation_id == generation_id,
            JobItem.status.in_(["queued", "running", "failed"]),
        )
    )
    return pending == 0


def _activate_any_ready_building(db: Session, workspace_id: int) -> None:
    """Activate every ready ``building`` generation via its rebuild job.

    Idempotent: :func:`_activate_rebuild` no-ops on already-active/retired
    generations and keeps late builds from overwriting a newer rule pointer.
    """
    building = db.scalars(
        select(IndexGeneration).where(
            IndexGeneration.workspace_id == workspace_id,
            IndexGeneration.status == "building",
        )
    ).all()
    for gen in building:
        owner = db.scalar(
            select(ExtractionJob).where(
                ExtractionJob.target_index_generation_id == gen.id,
                ExtractionJob.kind == "rebuild",
            )
        )
        if owner is not None and _generation_ready(db, gen.id):
            _activate_rebuild(db, owner)


def _activate_rebuild(db: Session, job: ExtractionJob) -> None:
    """Atomically swap the active generation; retire the previous one.

    If the active rule pointer has moved away from this job's version since
    the rebuild was scheduled (user rolled back, or published a newer
    version), the late build must NOT overwrite current state: it is retired
    as an abandoned generation instead.
    """
    state = db.get(WorkspaceState, job.workspace_id)
    new_gen = db.get(IndexGeneration, job.target_index_generation_id)
    if new_gen is None or new_gen.status != "building":
        return  # already activated or abandoned
    # Full-generation check across all jobs: never activate a generation that
    # still has queued/running/failed items (including incremental items).
    if not _generation_ready(db, new_gen.id):
        return
    if state.active_rule_version_id != job.target_rule_version_id:
        new_gen.status = "retired"
        return
    old_gen_id = state.active_index_generation_id
    new_gen.status = "active"
    new_gen.activated_at = _utcnow()
    if old_gen_id is not None and old_gen_id != new_gen.id:
        old_gen = db.get(IndexGeneration, old_gen_id)
        if old_gen is not None:
            old_gen.status = "retired"
    state.active_index_generation_id = new_gen.id


# --------------------------------------------------------------------------- #
# Worker loop
# --------------------------------------------------------------------------- #
class Worker:
    def __init__(self, *, poll_interval: float, lease_seconds: int,
                 heartbeat_seconds: int, max_workers: int,
                 worker_id: str | None = None) -> None:
        self.worker_id = worker_id or f"{os.getpid()}-{uuid.uuid4().hex[:10]}"
        self.poll_interval = poll_interval
        self.lease_seconds = lease_seconds
        self.heartbeat_seconds = heartbeat_seconds
        self.max_workers = max_workers
        self._stop = threading.Event()
        self._thread: threading.Thread | None = None

    def stop(self) -> None:
        self._stop.set()

    def run_forever(self) -> None:
        from ..db import write_session

        registered = False
        last_beat = 0.0
        while not self._stop.is_set():
            try:
                claimed = None
                # Registration/heartbeat/claim all write: hold the Python
                # write lock for the entire transaction so its lock order
                # can never invert against SQLite's own lock.
                with write_session() as db:
                    if not registered:
                        registered = register_worker(
                            db, self.worker_id,
                            max_workers=self.max_workers,
                            lease_seconds=self.lease_seconds,
                        )
                        if not registered:
                            # Cap reached; retry later in case a slot frees.
                            self._wait(self.poll_interval * 4)
                            continue
                    if time.time() - last_beat > self.heartbeat_seconds:
                        beat(db, self.worker_id)
                        last_beat = time.time()
                    claimed = claim_item(db, self.worker_id, self.lease_seconds)
                if claimed is None:
                    self._wait(self.poll_interval)
                    continue

                # Capture plain ids here: after this session closes the ORM
                # objects detach and attribute access would raise.
                self._process_with_retries(claimed.item.id, claimed.job.id)
            except Exception as exc:  # noqa: BLE001
                # A worker must never die on a transient DB error.
                print(f"[worker {self.worker_id}] loop error: {exc!r}", flush=True)
                self._wait(2.0)

    def _process_with_retries(self, item_id: int, job_id: int, *, attempts: int = 5) -> None:
        """Process an item, retrying optimistic write conflicts.

        With two workers writing the same index generation, SQLite may (a)
        raise SQLITE_BUSY on the write lock, or (b) fail a UNIQUE constraint
        because another transaction inserted the same term first. Both are
        serialization conflicts: roll the whole item back and rerun it —
        extraction/indexing are idempotent, so this is always safe.

        The whole read-modify-write sequence is wrapped in the process-local
        write lock, so two in-process worker threads never interleave SQLite
        write transactions. A conflict with the other worker process (separate
        container) is still handled as an optimistic item-level retry.
        """
        from ..db import write_session

        conflict = 0
        while True:
            try:
                with write_session() as db2:
                    item = db2.get(JobItem, item_id)
                    if item is None:
                        return
                    job = db2.get(ExtractionJob, job_id)
                    claimed = ClaimedItem(item=item, job=job)
                    final = process_item(db2, claimed)
                    self._finalize_after_item(db2, item, job)
                if final == "failed":
                    # Back off briefly to avoid hot-looping poison items.
                    self._wait(min(self.poll_interval * 4, 2.0))
                return
            except _WriteConflict:
                conflict += 1
                if conflict >= attempts:
                    # Persist as a normal failure so operators see it and can
                    # retry via the API instead of silently spinning.
                    self._record_giveup(item_id, job_id, conflict)
                    return
                # Jittered backoff before the next optimistic retry.
                self._wait(0.05 * conflict + random.random() * 0.05)

    @staticmethod
    def _finalize_after_item(db: Session, item: JobItem, job: ExtractionJob) -> None:
        maybe_finalize_job(db, job.id)
        # Time-order-independent activation: after every item completion,
        # re-check every building generation of this workspace and let the
        # owning rebuild job activate it if it is now complete. This avoids
        # relying on "which job's item happens to finish last".
        _activate_any_ready_building(db, item.workspace_id)

    @staticmethod
    def _record_giveup(item_id: int, job_id: int, conflict: int) -> None:
        from ..db import write_session

        with write_session() as db:
            item = db.get(JobItem, item_id)
            if item is not None and item.status not in ("done", "skipped"):
                item.status = "failed"
                item.last_error = (
                    f"WriteConflict: {conflict} consecutive serialization failures"
                )[:2000]
                item.leased_by = None
                item.leased_until = None
            maybe_finalize_job(db, job_id)

    def _wait(self, seconds: float) -> None:
        self._stop.wait(timeout=min(seconds, 5.0))

    # Embedded-thread helper ------------------------------------------------
    def start_in_thread(self) -> threading.Thread:
        t = threading.Thread(
            target=self.run_forever, name=f"kex-worker-{self.worker_id}", daemon=True
        )
        self._thread = t
        t.start()
        return t
