"""Batch event import, replay analysis and rebuild orchestration.

Concurrency model
-----------------
* Every mutation of a case's event set happens inside a transaction that
  holds ``SELECT ... FOR UPDATE`` on the case row.
* ``analyze_case`` takes the same lock, recomputes from the events visible
  at that moment, and only then clears ``needs_analysis``.
* A full rebuild additionally holds the project row lock, so rebuilds
  serialize against each other; imports of a case currently being rebuilt
  block briefly on the case lock and then re-flag the case, so an event
  that arrives mid-rebuild is never lost — it is either seen by the
  rebuild or picked up by the import's own incremental pass.
"""

from django.db import transaction
from django.db.models import Max
from django.utils import timezone
from django.utils.dateparse import parse_datetime

from .models import (
    AnalysisRun,
    Case,
    CaseAnalysis,
    Event,
    ImportBatch,
    Project,
    TemplateVersion,
)
from .template_def import CompiledTemplate

# Deviation types
MISSING_PREDECESSOR = "missing_predecessor"
DUPLICATE_EXECUTION = "duplicate_execution"
EXCLUSIVE_VIOLATION = "exclusive_violation"
TIMEOUT = "timeout"
UNKNOWN_ACTIVITY = "unknown_activity"


class BatchImportError(Exception):
    """Raised when a batch fails validation; nothing is persisted."""

    def __init__(self, errors):
        self.errors = errors
        super().__init__("; ".join(errors[:5]))


# ---------------------------------------------------------------------------
# Replay / conformance checking
# ---------------------------------------------------------------------------

def _deviation(dtype, event, message, evidence, constraint):
    return {
        "type": dtype,
        "activity": event.activity,
        "event_id": event.event_id,
        "seq": event.seq,
        "message": message,
        "evidence": evidence,
        "constraint": constraint,
    }


def _event_ref(event):
    return {
        "event_id": event.event_id,
        "seq": event.seq,
        "activity": event.activity,
        "occurred_at": event.occurred_at.isoformat(),
    }


def compute_case_analysis(case):
    """Replay a case's events in sequence order against its template.

    Pure function: returns the outcome dict without touching the database.
    """
    events = list(case.events.order_by("seq", "id"))
    tpl = CompiledTemplate(case.template_version.definition)

    seqs = sorted(e.seq for e in events)
    missing = []
    if seqs:
        present = set(seqs)
        missing = [n for n in range(1, seqs[-1] + 1) if n not in present]

    deviations = []
    seen = {}  # activity -> first event that executed it
    first_ts = events[0].occurred_at if events else None

    for event in events:
        act = event.activity
        if act not in tpl.activities:
            deviations.append(
                _deviation(
                    UNKNOWN_ACTIVITY,
                    event,
                    f"activity '{act}' is not defined in the template",
                    {"event": _event_ref(event)},
                    {"defined_activities": sorted(tpl.activities)},
                )
            )
            continue

        if act in seen:
            deviations.append(
                _deviation(
                    DUPLICATE_EXECUTION,
                    event,
                    f"activity '{act}' executed more than once",
                    {
                        "first_execution": _event_ref(seen[act]),
                        "duplicate_execution": _event_ref(event),
                    },
                    {
                        "rule": "activity occurs at most once "
                        "(template graph is acyclic)",
                        "activity": act,
                    },
                )
            )

        for dep in tpl.deps_to.get(act, []):
            done = [s for s in dep["from"] if s in seen]
            violated = (dep["mode"] == "all" and len(done) < len(dep["from"])) or (
                dep["mode"] == "any" and not done
            )
            if violated:
                deviations.append(
                    _deviation(
                        MISSING_PREDECESSOR,
                        event,
                        f"activity '{act}' started without its required "
                        f"predecessor(s)",
                        {
                            "event": _event_ref(event),
                            "completed_predecessors": done,
                        },
                        {
                            "rule": "all_of" if dep["mode"] == "all" else "any_of",
                            "predecessors": dep["from"],
                            "activity": act,
                        },
                    )
                )

        for group in tpl.groups_of.get(act, []):
            for other in group:
                if other != act and other in seen:
                    deviations.append(
                        _deviation(
                            EXCLUSIVE_VIOLATION,
                            event,
                            f"mutually exclusive activities '{other}' and "
                            f"'{act}' both executed",
                            {
                                "first_execution": _event_ref(seen[other]),
                                "conflicting_execution": _event_ref(event),
                            },
                            {"exclusive_group": group},
                        )
                    )

        for limit in tpl.limits_to.get(act, []):
            if limit["from"] is None:
                base_ts, base_desc = first_ts, "case start"
            elif limit["from"] in seen:
                base_ts = seen[limit["from"]].occurred_at
                base_desc = f"activity '{limit['from']}'"
            else:
                continue  # cannot evaluate a limit whose anchor never ran
            elapsed = (event.occurred_at - base_ts).total_seconds()
            if elapsed > limit["max_seconds"]:
                deviations.append(
                    _deviation(
                        TIMEOUT,
                        event,
                        f"activity '{act}' exceeded its time limit "
                        f"from {base_desc}",
                        {
                            "event": _event_ref(event),
                            "anchor": base_desc,
                            "elapsed_seconds": int(elapsed),
                        },
                        {
                            "from": limit["from"],
                            "to": act,
                            "max_seconds": limit["max_seconds"],
                        },
                    )
                )

        if act not in seen:
            seen[act] = event

    if missing:
        status = CaseAnalysis.Status.WAITING
    elif deviations:
        status = CaseAnalysis.Status.NONCONFORMANT
    elif any(t in seen for t in tpl.terminals):
        status = CaseAnalysis.Status.CONFORMANT
    else:
        status = CaseAnalysis.Status.IN_PROGRESS

    return {
        "status": status,
        "missing_seqs": missing,
        "deviations": deviations,
        "event_count": len(events),
    }


def analyze_case(case, run=None):
    """Recompute one case under its row lock and store a new revision.

    If the recomputed outcome is identical to the current one, no new
    revision is created (keeps the revision history meaningful).
    """
    with transaction.atomic():
        case = Case.objects.select_for_update().get(pk=case.pk)
        outcome = compute_case_analysis(case)
        current = case.analyses.filter(is_current=True).first()
        if current is not None and current.outcome() == outcome:
            case.needs_analysis = False
            case.save(update_fields=["needs_analysis"])
            return current
        revision = (
            case.analyses.aggregate(m=Max("revision"))["m"] or 0
        ) + 1
        case.analyses.filter(is_current=True).update(is_current=False)
        analysis = CaseAnalysis.objects.create(
            case=case, run=run, revision=revision, is_current=True, **outcome
        )
        case.needs_analysis = False
        case.save(update_fields=["needs_analysis"])
        return analysis


def rebuild_project(project):
    """Full rebuild: recompute every case in the project from its events.

    Holds the project row lock for the whole run so concurrent rebuilds
    serialize. Events imported concurrently are safe: their import either
    commits before a case is analyzed (and is seen) or blocks on the case
    lock and re-flags the case for its own incremental pass.
    """
    with transaction.atomic():
        Project.objects.select_for_update().get(pk=project.pk)
        run = AnalysisRun.objects.create(
            project=project, kind=AnalysisRun.Kind.FULL
        )
        count = 0
        for case in project.cases.order_by("pk"):
            analyze_case(case, run=run)
            count += 1
        run.cases_processed = count
        run.finished_at = timezone.now()
        run.save()
        return run


# ---------------------------------------------------------------------------
# Batch event import
# ---------------------------------------------------------------------------

def _parse_occurred_at(value):
    if not isinstance(value, str):
        return None
    dt = parse_datetime(value)
    if dt is None:
        return None
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    return dt


def _validate_item(item, index):
    """Field-level validation of one import item. Returns (clean, errors)."""
    errors = []
    prefix = f"events[{index}]"
    if not isinstance(item, dict):
        return None, [f"{prefix}: must be an object"]

    event_id = item.get("event_id")
    if not isinstance(event_id, str) or not event_id.strip():
        errors.append(f"{prefix}: 'event_id' must be a non-empty string")
    case_key = item.get("case_id")
    if not isinstance(case_key, str) or not case_key.strip():
        errors.append(f"{prefix}: 'case_id' must be a non-empty string")
    activity = item.get("activity")
    if not isinstance(activity, str) or not activity.strip():
        errors.append(f"{prefix}: 'activity' must be a non-empty string")
    occurred_at = _parse_occurred_at(item.get("occurred_at"))
    if occurred_at is None:
        errors.append(
            f"{prefix}: 'occurred_at' must be an ISO 8601 datetime string"
        )
    seq = item.get("seq")
    if not isinstance(seq, int) or isinstance(seq, bool) or seq < 1:
        errors.append(f"{prefix}: 'seq' must be a positive integer")
    version_id = item.get("template_version_id")
    if version_id is not None and (
        not isinstance(version_id, int) or isinstance(version_id, bool)
    ):
        errors.append(f"{prefix}: 'template_version_id' must be an integer")

    if errors:
        return None, errors
    return (
        {
            "event_id": event_id.strip(),
            "case_key": case_key.strip(),
            "activity": activity.strip(),
            "occurred_at": occurred_at,
            "seq": seq,
            "template_version_id": version_id,
        },
        [],
    )


def _same_content(clean, event):
    return (
        event.case.case_key == clean["case_key"]
        and event.activity == clean["activity"]
        and event.occurred_at == clean["occurred_at"]
        and event.seq == clean["seq"]
    )


def import_events(project, items):
    """Import a batch of events atomically.

    * Duplicate content (same event_id, same payload) is skipped — the
      import is idempotent.
    * Same event_id or same (case, seq) with different content is a
      conflict: the whole batch is rejected and rolled back.
    * Out-of-order and late arrival are fine; affected cases are
      re-analyzed after the batch commits.

    Returns the ImportBatch row. Raises BatchImportError on rejection
    (the failed batch row is still recorded, with per-item errors).
    """
    if not isinstance(items, list) or not items:
        raise BatchImportError(["request body must be a non-empty list of events"])

    try:
        with transaction.atomic():
            return _import_events_txn(project, items)
    except BatchImportError as exc:
        # Record the rejected batch outside the rolled-back transaction.
        ImportBatch.objects.create(
            project=project,
            status=ImportBatch.Status.FAILED,
            total=len(items),
            errors=exc.errors,
        )
        raise


def _import_events_txn(project, items):
    with transaction.atomic():
        batch = ImportBatch.objects.create(
            project=project, status=ImportBatch.Status.DONE, total=len(items)
        )

        # Phase 1: validate everything, insert nothing.
        errors = []
        cleans = []
        for i, item in enumerate(items):
            clean, item_errors = _validate_item(item, i)
            errors.extend(item_errors)
            cleans.append(clean)
        if errors:
            raise BatchImportError(errors)

        event_ids = [c["event_id"] for c in cleans]
        case_keys = sorted({c["case_key"] for c in cleans})
        seqs = sorted({c["seq"] for c in cleans})

        existing_by_eid = {
            e.event_id: e
            for e in Event.objects.filter(
                project=project, event_id__in=event_ids
            ).select_related("case")
        }
        existing_by_case_seq = {}
        for e in Event.objects.filter(
            case__project=project, case__case_key__in=case_keys, seq__in=seqs
        ).select_related("case"):
            existing_by_case_seq[(e.case.case_key, e.seq)] = e

        existing_cases = {
            c.case_key: c
            for c in Case.objects.filter(project=project, case_key__in=case_keys)
        }
        version_ids = {
            c["template_version_id"] for c in cleans if c["template_version_id"]
        }
        versions = {
            v.id: v
            for v in TemplateVersion.objects.filter(
                id__in=version_ids, template__project=project
            ).select_related("template")
        }

        seen_ids = {}
        seen_case_seq = {}
        to_insert = []
        skipped = 0

        for i, clean in enumerate(cleans):
            prefix = f"events[{i}]"
            eid = clean["event_id"]
            cs_key = (clean["case_key"], clean["seq"])

            # Conflict: same event_id, different content (DB or this batch).
            if eid in seen_ids:
                if seen_ids[eid] != clean:
                    errors.append(
                        f"{prefix}: event_id '{eid}' already appears in this "
                        f"batch with different content"
                    )
                    continue
                skipped += 1
                continue
            existing = existing_by_eid.get(eid)
            if existing is not None:
                if not _same_content(clean, existing):
                    errors.append(
                        f"{prefix}: event_id '{eid}' already exists with "
                        f"different content"
                    )
                    continue
                skipped += 1
                seen_ids[eid] = clean
                continue

            # Conflict: same (case, seq), different event (DB or this batch).
            if cs_key in seen_case_seq:
                errors.append(
                    f"{prefix}: case '{clean['case_key']}' seq {clean['seq']} "
                    f"already used by event '{seen_case_seq[cs_key]}' in this batch"
                )
                continue
            seq_owner = existing_by_case_seq.get(cs_key)
            if seq_owner is not None:
                errors.append(
                    f"{prefix}: case '{clean['case_key']}' seq {clean['seq']} "
                    f"already used by event '{seq_owner.event_id}'"
                )
                continue

            # Case binding: reuse, or create bound to a published version.
            case = existing_cases.get(clean["case_key"])
            if case is None:
                vid = clean["template_version_id"]
                version = versions.get(vid) if vid else None
                if version is None:
                    errors.append(
                        f"{prefix}: new case '{clean['case_key']}' requires a "
                        f"valid 'template_version_id' in this project"
                    )
                    continue
                if version.status != TemplateVersion.Status.PUBLISHED:
                    errors.append(
                        f"{prefix}: template version {vid} is not published"
                    )
                    continue
            elif (
                clean["template_version_id"]
                and clean["template_version_id"] != case.template_version_id
            ):
                errors.append(
                    f"{prefix}: case '{clean['case_key']}' is bound to template "
                    f"version {case.template_version_id}, not "
                    f"{clean['template_version_id']}"
                )
                continue

            seen_ids[eid] = clean
            seen_case_seq[cs_key] = eid
            to_insert.append(clean)

        if errors:
            raise BatchImportError(errors)

        # Phase 2: insert. Lock each touched case row so a concurrent
        # rebuild cannot clear its dirty flag after we set it.
        touched_keys = sorted({c["case_key"] for c in to_insert})
        locked_cases = {
            c.case_key: c
            for c in Case.objects.select_for_update().filter(
                project=project, case_key__in=touched_keys
            )
        }
        touched = set()
        for clean in to_insert:
            case = locked_cases.get(clean["case_key"])
            if case is None:
                case = Case.objects.create(
                    project=project,
                    case_key=clean["case_key"],
                    template_version_id=clean["template_version_id"],
                )
                locked_cases[clean["case_key"]] = case
            Event.objects.create(
                project=project,
                case=case,
                batch=batch,
                event_id=clean["event_id"],
                activity=clean["activity"],
                occurred_at=clean["occurred_at"],
                seq=clean["seq"],
            )
            case.needs_analysis = True
            case.save(update_fields=["needs_analysis"])
            touched.add(case)

        batch.inserted = len(to_insert)
        batch.skipped = skipped
        batch.save()

        # Incremental re-analysis of exactly the affected cases.
        run = AnalysisRun.objects.create(
            project=project, kind=AnalysisRun.Kind.INCREMENTAL
        )
        for case in touched:
            analyze_case(case, run=run)
        run.cases_processed = len(touched)
        run.finished_at = timezone.now()
        run.save()

        return batch
