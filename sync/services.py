"""Batch ingestion, conflict detection, resolution and tombstoning.

The service is deliberately storage-centric: views only parse/serialize.
Writers of one record UUID serialize on a cross-process advisory lock and
write inside their own committed transaction, so two devices syncing the same
UUID concurrently never last-write-win.
"""
from datetime import datetime, timezone

from contextlib import contextmanager

from django.db import IntegrityError, connection, transaction

from forms.models import Crew, FormTemplateVersion
from forms.validation import (
    RecordValidationError,
    check_version_accepted,
    validate_record_data,
)

from .hashing import canonical_content_hash
from .models import (
    ChangeType,
    FormRecord,
    RecordChange,
    RecordRevision,
    RecordStatus,
    RevisionKind,
    SyncBatch,
    SyncResultEntry,
)

# Per-entry outcome codes carried inside the batch envelope.
ACCEPTED_CREATED = 201
ACCEPTED_UPDATED = 200
CONFLICT = 409
REJECTED = 422
BAD_REQUEST = 400


class Outcome(dict):
    """One result row; keys mirror the public sync response contract."""


def _now():
    return datetime.now(timezone.utc)


@contextmanager
def record_write_lock(record_uuid):
    """Serialize every writer of one record UUID across all processes.

    Why not row locks on the record? Two concurrent INSERTs of the same
    unique value collide at the InnoDB unique index and can deadlock/timeout
    (even with INSERT IGNORE) before either row exists to lock. MySQL named
    advisory locks have no such race: ``GET_LOCK`` queues waiters on a name.
    On SQLite (tests/dev) an in-process threading.Lock is enough -- file-level
    locking serializes writers anyway.
    """
    lock_name = f"fieldsnap:record:{record_uuid}"
    if connection.vendor == "mysql":
        with connection.cursor() as cursor:
            cursor.execute("SELECT GET_LOCK(%s, 10)", [lock_name])
            acquired = cursor.fetchone()[0]
        if acquired != 1:
            raise RecordValidationError(
                {"__all__": "could not acquire record lock (timeout)"}
            )
        try:
            yield
        finally:
            with connection.cursor() as cursor:
                cursor.execute("SELECT RELEASE_LOCK(%s)", [lock_name])
    else:
        import threading

        lock = _SQLITE_LOCKS.setdefault(str(record_uuid), threading.Lock())
        with lock:
            yield


_SQLITE_LOCKS = {}


def emit_change(record, revision, change_type):
    RecordChange.objects.create(
        record=record,
        revision=revision,
        change_type=change_type,
        project=record.project,
        crew=record.crew,
        template=record.template,
        record_status=record.status,
        deleted=record.deleted,
    )


def _accepted_outcome(revision, code, *, conflict_with=None):
    return Outcome(
        client_uuid=str(revision.record.record_uuid),
        status=SyncResultEntry.EntryStatus.ACCEPTED
        if code in (ACCEPTED_CREATED, ACCEPTED_UPDATED)
        else SyncResultEntry.EntryStatus.CONFLICT,
        status_code=code,
        record_version=revision.seq,
        revision_seq=revision.seq,
        content_hash=revision.content_hash,
        conflict_with_seq=conflict_with,
        errors=None,
    )


def submit_batch(user, client_batch_id, entries, can_submit):
    """Process a sync batch of <= 50 entries.

    Returns ``(batch, outcomes, replayed)``. Entries are independent: each
    gets its own transaction so one bad/aborted entry never rolls back the
    others, and -- critically -- a per-entry advisory lock is held until that
    entry's transaction actually COMMITS. An outer batch-level transaction
    would reduce the inner block to a savepoint and release the lock while
    the winner's row was still uncommitted, reintroducing duplicate-key races.
    """
    try:
        with transaction.atomic():
            batch, created = SyncBatch.objects.get_or_create(
                submitted_by=user,
                client_batch_id=client_batch_id,
                defaults={"entry_count": len(entries)},
            )
    except IntegrityError:
        # Two concurrent requests carrying the same client batch id: the
        # other one owns it, replay its stored results.
        batch = SyncBatch.objects.get(
            submitted_by=user, client_batch_id=client_batch_id
        )
        return batch, _replay_results(batch), True
    if not created:
        return batch, _replay_results(batch), True

    outcomes = []
    for entry in entries:
        try:
            outcomes.append(_process_entry(user, entry, can_submit))
        except Exception as exc:  # defensive: keep the batch alive
            outcomes.append(
                Outcome(
                    client_uuid=_safe_uuid(entry),
                    status=SyncResultEntry.EntryStatus.ERROR,
                    status_code=BAD_REQUEST,
                    record_version=None,
                    revision_seq=None,
                    content_hash="",
                    conflict_with_seq=None,
                    errors={"__all__": str(exc)},
                )
            )

    with transaction.atomic():
        batch = SyncBatch.objects.get(pk=batch.pk)
        SyncResultEntry.objects.bulk_create(
            [
                SyncResultEntry(
                    batch=batch,
                    client_uuid=o["client_uuid"],
                    status=o["status"],
                    status_code=o["status_code"],
                    record_version=o["record_version"],
                    revision_seq=o["revision_seq"],
                    content_hash=o.get("content_hash", ""),
                    errors=o["errors"],
                    conflict_with_seq=o.get("conflict_with_seq"),
                )
                for o in outcomes
            ]
        )
    return batch, outcomes, False


def _safe_uuid(entry):
    try:
        return entry.get("uuid")
    except AttributeError:
        return "00000000-0000-0000-0000-000000000000"


def _replay_results(batch):
    return [
        Outcome(
            client_uuid=str(r.client_uuid),
            status=r.status,
            status_code=r.status_code,
            record_version=r.record_version,
            revision_seq=r.revision_seq,
            content_hash=r.content_hash,
            conflict_with_seq=r.conflict_with_seq,
            errors=r.errors,
        )
        for r in batch.results.all()
    ]


def _rejection(entry, errors, code=REJECTED):
    return Outcome(
        client_uuid=entry.get("uuid"),
        status=SyncResultEntry.EntryStatus.REJECTED,
        status_code=code,
        record_version=None,
        revision_seq=None,
        content_hash="",
        conflict_with_seq=None,
        errors=errors,
    )


def _process_entry(user, entry, can_submit):
    if not isinstance(entry, dict):
        return _rejection({}, {"__all__": "entry must be an object"}, BAD_REQUEST)

    record_uuid = entry.get("uuid")
    template_id = entry.get("template_id")
    version_no = entry.get("template_version")
    base_version = entry.get("record_version", 0)
    data = entry.get("data")
    collected_at = entry.get("collected_at")
    crew_id = entry.get("crew_id")

    # ---- shape checks --------------------------------------------------
    if not record_uuid:
        return _rejection(entry, {"uuid": "required"}, BAD_REQUEST)
    if crew_id is None:
        return _rejection(entry, {"crew_id": "required"}, BAD_REQUEST)
    if not isinstance(base_version, int) or base_version < 0:
        return _rejection(entry, {"record_version": "must be a non-negative integer"}, BAD_REQUEST)
    if data is None:
        return _rejection(entry, {"data": "required"}, BAD_REQUEST)
    try:
        collected = _parse_collected_at(collected_at)
    except RecordValidationError as exc:
        return _rejection(entry, exc.errors, BAD_REQUEST)

    try:
        template_version = (
            FormTemplateVersion.objects.select_related("template", "template__project")
            .get(template_id=template_id, version=version_no)
        )
    except FormTemplateVersion.DoesNotExist:
        return _rejection(
            entry,
            {"template_version": (
                "template_version_unavailable: pin a published version "
                "(fetch the current template and retry)"
            )},
            REJECTED,
        )
    template = template_version.template
    project = template.project

    # ---- access checks -------------------------------------------------
    try:
        crew = Crew.objects.get(pk=crew_id, project=project)
    except Crew.DoesNotExist:
        return _rejection(entry, {"crew_id": "unknown crew for this project"}, REJECTED)
    if not can_submit(user, crew.id):
        return _rejection(entry, {"crew_id": "not a member of this crew"}, 403)

    # ---- template compatibility ---------------------------------------
    try:
        check_version_accepted(template, template_version)
    except RecordValidationError as exc:
        return _rejection(entry, exc.errors, REJECTED)

    # ---- field validation against the PINNED version ------------------
    outcome, normalized = validate_record_data(template_version, data)
    if not outcome.ok:
        return _rejection(entry, outcome.errors, REJECTED)

    content_hash = canonical_content_hash(normalized)
    return _upsert_revision(
        user=user,
        record_uuid=record_uuid,
        project=project,
        crew=crew,
        template=template,
        template_version=template_version,
        base_version=base_version,
        data=normalized,
        content_hash=content_hash,
        collected_at=collected,
    )


def _parse_collected_at(value):
    if not value:
        raise RecordValidationError({"collected_at": "required"})
    try:
        parsed = datetime.fromisoformat(str(value).replace("Z", "+00:00"))
        return parsed
    except (TypeError, ValueError):
        raise RecordValidationError(
            {"collected_at": "must be ISO-8601 (e.g. 2026-09-20T08:30:00Z)"}
        )


def _upsert_revision(
    *,
    user,
    record_uuid,
    project,
    crew,
    template,
    template_version,
    base_version,
    data,
    content_hash,
    collected_at,
):
    """Locked insert/update for one record UUID.

    All writers of one UUID first take a cross-process advisory lock (see
    :func:`record_write_lock`); within it, a row lock guards updates against
    the (now impossible) concurrent writer and keeps the read-modify-write
    atomic.
    """
    with record_write_lock(record_uuid), transaction.atomic():
        record = FormRecord.objects.filter(record_uuid=record_uuid).first()
        is_new = record is None

        if is_new:
            # We hold the UUID mutex and no record exists, so this is a plain
            # create; any concurrent request only proceeds once we commit.
            record = FormRecord.objects.create(
                record_uuid=record_uuid,
                project=project,
                crew=crew,
                template=template,
                current_version=template_version,
                current_revision=None,
                status=RecordStatus.OK,
                created_by=user,
            )
            revision = _append_revision(
                record,
                kind=RevisionKind.CREATED,
                base_version=0,
                template_version=template_version,
                data=data,
                content_hash=content_hash,
                collected_at=collected_at,
                user=user,
            )
            record.current_revision = revision
            record.save(update_fields=("current_revision",))
            emit_change(record, revision, ChangeType.CREATED)
            return _accepted_outcome(revision, ACCEPTED_CREATED)

        if record.template_id != template.id or record.project_id != project.id:
            return Outcome(
                client_uuid=str(record_uuid),
                status=SyncResultEntry.EntryStatus.REJECTED,
                status_code=REJECTED,
                record_version=None,
                revision_seq=None,
                content_hash="",
                conflict_with_seq=None,
                errors={"uuid": "uuid already used with a different template/project"},
            )

        if record.deleted:
            return Outcome(
                client_uuid=str(record_uuid),
                status=SyncResultEntry.EntryStatus.REJECTED,
                status_code=CONFLICT,
                record_version=None,
                revision_seq=None,
                content_hash=content_hash,
                conflict_with_seq=None,
                errors={"uuid": "record_deleted: create a new UUID to replace it"},
            )

        # Idempotent retry: same UUID + same content -> original result.
        same = record.revisions.filter(content_hash=content_hash).first()
        if same is not None:
            if same.kind == RevisionKind.CREATED:
                code, status_label = ACCEPTED_CREATED, SyncResultEntry.EntryStatus.ACCEPTED
            elif same.kind in (RevisionKind.UPDATED, RevisionKind.RESOLVED):
                code, status_label = ACCEPTED_UPDATED, SyncResultEntry.EntryStatus.ACCEPTED
            else:
                # divergent/stale sibling re-sent: still a conflict awaiting
                # the supervisor, but never duplicated.
                code, status_label = CONFLICT, SyncResultEntry.EntryStatus.CONFLICT
            return Outcome(
                client_uuid=str(record_uuid),
                status=status_label,
                status_code=code,
                record_version=same.seq,
                revision_seq=same.seq,
                content_hash=content_hash,
                conflict_with_seq=record.conflict_of.seq if record.conflict_of_id else None,
                errors=(
                    {"__all__": "record_has_conflict: awaiting supervisor resolution"}
                    if code == CONFLICT
                    else None
                ),
            )

        head = record.current_revision
        if record.status == RecordStatus.CONFLICT:
            # Pending conflict; nothing can overwrite it until resolved.
            kind = (
                RevisionKind.DIVERGENT
                if base_version >= head.seq
                else RevisionKind.STALE
            )
            revision = _append_revision(
                record,
                kind=kind,
                base_version=base_version,
                template_version=template_version,
                data=data,
                content_hash=content_hash,
                collected_at=collected_at,
                user=user,
            )
            emit_change(record, revision, ChangeType.CONFLICT)
            return Outcome(
                client_uuid=str(record_uuid),
                status=SyncResultEntry.EntryStatus.CONFLICT,
                status_code=CONFLICT,
                record_version=revision.seq,
                revision_seq=revision.seq,
                content_hash=content_hash,
                conflict_with_seq=record.conflict_of.seq,
                errors={"__all__": (
                    "record_has_conflict: a supervisor must resolve the existing "
                    "conflict before further edits are accepted as current"
                )},
            )

        if base_version >= head.seq:
            # Client's base is at least the server head: real new content,
            # fast-forward accepted (base==head normal edit, base>head a
            # legitimate newer write).
            revision = _append_revision(
                record,
                kind=RevisionKind.UPDATED,
                base_version=base_version,
                template_version=template_version,
                data=data,
                content_hash=content_hash,
                collected_at=collected_at,
                user=user,
            )
            record.current_version = template_version
            record.current_revision = revision
            record.save(update_fields=("current_version", "current_revision", "updated_at"))
            emit_change(record, revision, ChangeType.UPDATED)
            return _accepted_outcome(revision, ACCEPTED_UPDATED)

        # base_version < head and content differs: the client edited an
        # out-of-date copy. Both sides are retained; nothing is overwritten.
        # base == head-1 is a near-simultaneous divergence; anything older is
        # a stale branch -- both wait for the supervisor.
        kind = (
            RevisionKind.DIVERGENT
            if base_version == head.seq - 1
            else RevisionKind.STALE
        )
        revision = _append_revision(
            record,
            kind=kind,
            base_version=base_version,
            template_version=template_version,
            data=data,
            content_hash=content_hash,
            collected_at=collected_at,
            user=user,
        )
        record.status = RecordStatus.CONFLICT
        record.conflict_of = head
        record.save(update_fields=("status", "conflict_of", "updated_at"))
        # Emit after the status flip so the stream carries the new state.
        emit_change(record, revision, ChangeType.CONFLICT)
        return Outcome(
            client_uuid=str(record_uuid),
            status=SyncResultEntry.EntryStatus.CONFLICT,
            status_code=CONFLICT,
            record_version=revision.seq,
            revision_seq=revision.seq,
            content_hash=content_hash,
            conflict_with_seq=head.seq,
            errors={"__all__": (
                "concurrent_modification: your edit branched from v"
                f"{base_version} but v{head.seq} exists; both versions kept, "
                "awaiting supervisor resolution"
            )},
        )


def _append_revision(
    record,
    *,
    kind,
    base_version,
    template_version,
    data,
    content_hash,
    collected_at,
    user,
):
    """Append the next sequence; callers already hold the record row lock."""
    last_seq = record.revisions.order_by("-seq").values_list("seq", flat=True).first() or 0
    revision = RecordRevision.objects.create(
        record=record,
        seq=last_seq + 1,
        base_version=base_version,
        template_version=template_version,
        data=data,
        content_hash=content_hash,
        collected_at=collected_at,
        submitted_by=user,
        kind=kind,
    )
    return revision


def resolve_conflict(*, supervisor, record, merged_data, template_version_no, note):
    """Supervisor resolution: store the merged copy as a new, authoritative rev.

    Both sides stay in the revision history; nothing is overwritten.
    """
    record_uuid = record.record_uuid
    with record_write_lock(record_uuid), transaction.atomic():
        record = FormRecord.objects.select_for_update().get(record_uuid=record_uuid)
        if record.status != RecordStatus.CONFLICT:
            raise RecordValidationError({"__all__": "record is not in conflict"})

        template_version = FormTemplateVersion.objects.get(
            template=record.template, version=template_version_no
        )
        check_version_accepted(record.template, template_version)
        outcome, normalized = validate_record_data(template_version, merged_data)
        if not outcome.ok:
            raise RecordValidationError(outcome.errors)

        content_hash = canonical_content_hash(normalized)
        existing = record.revisions.filter(content_hash=content_hash).first()
        if existing is not None:
            # Merged copy equals one side: that revision becomes authoritative.
            # Its original kind (divergent/stale/updated) stays in history; the
            # resolution markers below record the supervisor's decision.
            revision = existing
            revision.resolved_by = supervisor
            revision.resolution_note = note
            revision.save(update_fields=("resolved_by", "resolution_note"))
        else:
            revision = _append_revision(
                record,
                kind=RevisionKind.RESOLVED,
                base_version=record.current_revision.seq,
                template_version=template_version,
                data=normalized,
                content_hash=content_hash,
                collected_at=_now(),
                user=supervisor,
            )
            revision.resolved_by = supervisor
            revision.resolution_note = note
            revision.save(update_fields=("resolved_by", "resolution_note"))

        record.status = RecordStatus.OK
        record.conflict_of = None
        record.current_version = template_version
        record.current_revision = revision
        record.save(
            update_fields=(
                "status", "conflict_of", "current_version", "current_revision", "updated_at"
            )
        )
        emit_change(record, revision, ChangeType.RESOLVED)
        return record, revision


def delete_record(*, user, record):
    """Soft delete: append a tombstone, retain every revision."""
    record_uuid = record.record_uuid
    with record_write_lock(record_uuid), transaction.atomic():
        record = FormRecord.objects.select_for_update().get(record_uuid=record_uuid)
        if record.deleted:
            return record, None
        record.deleted = True
        record.status = RecordStatus.DELETED
        record.save(update_fields=("deleted", "status", "updated_at"))
        # Tombstones deliberately carry no revision payload -- clients delete
        # the row rather than overwrite it with the head's data.
        change = RecordChange.objects.create(
            record=record,
            revision=None,
            change_type=ChangeType.DELETED,
            project=record.project,
            crew=record.crew,
            template=record.template,
            record_status=record.status,
            deleted=True,
        )
        return record, change
