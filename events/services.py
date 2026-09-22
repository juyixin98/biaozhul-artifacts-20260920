"""Idempotent, out-of-order tolerant event batch ingestion.

Response contract (always HTTP 200 for an accepted batch envelope):

    {
      "received": 150,
      "stored":   148,
      "duplicate": 1,
      "failed":   1,
      "results": [
        {"index": 0, "event_id": "evt-1", "status": "stored"},
        {"index": 7, "event_id": "evt-8", "status": "duplicate"},
        {"index": 9, "event_id": "evt-10", "status": "failed",
         "errors": {"network_id": "network 42 does not belong to this app"}}
      ]
    }

Duplicates (same ``(app, event_id)``) are *successful* no-ops -- retries from
flaky mobile networks must not surface as errors. Malformed items are rejected
individually with field-level reasons; good items in the same batch still land.

Out-of-order and late events are both first-class: nothing here depends on
ordering or on ``occurred_at`` being recent.
"""
from __future__ import annotations

from datetime import datetime

from django.conf import settings
from django.db import IntegrityError

from applications.models import AdNetwork, Placement
from common.utils import hash_user_key, to_money
from experiments.models import Experiment
from waterfall.models import WaterfallVersion

from .models import Event, EventType, FailureReason

VALID_VARIANTS = {"A", "B"}
# Insert in chunks so a batch of 2000 does not push MySQL max_allowed_packet.
CHUNK_SIZE = 500


class BatchRejected(Exception):
    """The batch envelope itself is invalid (not an array, too large, ...)."""


def ingest_batch(*, app, data, now=None) -> dict:
    if not isinstance(data, list):
        raise BatchRejected("request body must be a JSON array of event objects")
    if len(data) == 0:
        raise BatchRejected("batch must contain at least one event")
    if len(data) > settings.MAX_EVENTS_PER_BATCH:
        raise BatchRejected(
            f"batch exceeds the maximum of {settings.MAX_EVENTS_PER_BATCH} events "
            f"(got {len(data)})"
        )

    # Pre-fetch owned resources once, keyed for cheap per-item lookup.
    placements = {p.id: p for p in Placement.objects.filter(app=app)}
    network_ids = set(
        AdNetwork.objects.filter(developer_id=app.developer_id)
        .values_list("id", flat=True)
    )
    version_ids = set(
        WaterfallVersion.objects.filter(
            placement__app=app, status=WaterfallVersion.Status.PUBLISHED
        ).values_list("id", flat=True)
    )
    experiments = {
        e.id: e for e in Experiment.objects.filter(placement__app=app)
    }

    prepared: list[tuple[int, Event]] = []
    results: list[dict] = []
    incoming_ids: set[str] = set()
    duplicate_count = 0
    failed_count = 0

    for index, raw in enumerate(data):
        if not isinstance(raw, dict):
            results.append(
                {"index": index, "event_id": None, "status": "failed",
                 "errors": {"_": "event must be a JSON object"}}
            )
            failed_count += 1
            continue

        errors, event_id = _validate_item(
            raw,
            app=app,
            placements=placements,
            network_ids=network_ids,
            version_ids=version_ids,
            experiments=experiments,
            incoming_ids=incoming_ids,
        )
        if errors:
            results.append(
                {"index": index, "event_id": event_id, "status": "failed",
                 "errors": errors}
            )
            failed_count += 1
            continue

        if event_id in incoming_ids:
            # Already prepared earlier in this same batch.
            results.append(
                {"index": index, "event_id": event_id, "status": "duplicate"}
            )
            duplicate_count += 1
            continue
        incoming_ids.add(event_id)

        event = Event(app=app, **_build_kwargs(raw))
        prepared.append((index, event))

    stored = 0
    if prepared:
        # Skip ids that were already persisted by a previous batch/request --
        # this is what makes retries and concurrent duplicate batches safe.
        existing_ids = set(
            Event.objects.filter(app=app, event_id__in=list(incoming_ids))
            .values_list("event_id", flat=True)
        )
        to_insert: list[tuple[int, Event]] = []
        for index, event in prepared:
            if event.event_id in existing_ids:
                results.append(
                    {"index": index, "event_id": event.event_id,
                     "status": "duplicate"}
                )
                duplicate_count += 1
            else:
                to_insert.append((index, event))

        race_duplicates: set[str] = set()
        for start in range(0, len(to_insert), CHUNK_SIZE):
            chunk = [event for _, event in to_insert[start : start + CHUNK_SIZE]]
            try:
                Event.objects.bulk_create(chunk, ignore_conflicts=True)
            except IntegrityError:
                # Fall back to row-by-row if a concurrent batch raced us on a
                # chunk; already-stored rows become duplicates, the rest land.
                for event in chunk:
                    try:
                        event.save()
                    except IntegrityError:
                        race_duplicates.add(event.event_id)
                        results.append(
                            {"index": None, "event_id": event.event_id,
                             "status": "duplicate"}
                        )
                        duplicate_count += 1

        # ``ignore_conflicts`` does not report which rows made it; verify the
        # survivors against the DB so every index gets an exact per-item result.
        persisted = set(
            Event.objects.filter(
                app=app,
                event_id__in=[e.event_id for _, e in to_insert],
            ).values_list("event_id", flat=True)
        )
        for index, event in to_insert:
            if event.event_id in persisted and event.event_id not in race_duplicates:
                results.append(
                    {"index": index, "event_id": event.event_id, "status": "stored"}
                )
                stored += 1
            elif event.event_id not in race_duplicates:
                results.append(
                    {"index": index, "event_id": event.event_id,
                     "status": "duplicate"}
                )
                duplicate_count += 1

    results.sort(key=lambda r: (r["index"] is None, r["index"] if r["index"] is not None else 0))
    return {
        "received": len(data),
        "stored": stored,
        "duplicate": duplicate_count,
        "failed": failed_count,
        "results": results,
    }


def _validate_item(
    raw,
    *,
    app,
    placements,
    network_ids,
    version_ids,
    experiments,
    incoming_ids,
):
    errors: dict[str, str] = {}
    event_id = raw.get("event_id")
    if not isinstance(event_id, str) or not event_id.strip():
        errors["event_id"] = "required non-empty string"
    elif len(event_id) > 64:
        errors["event_id"] = "at most 64 characters"

    event_type = raw.get("event_type")
    if event_type not in EventType.values:
        errors["event_type"] = f"must be one of {', '.join(EventType.values)}"

    placement_id = raw.get("placement_id")
    placement = placements.get(placement_id) if placement_id is not None else None
    if placement_id is None:
        errors["placement_id"] = "required integer"
    elif placement is None:
        errors["placement_id"] = "placement does not exist in this app"

    network_id = raw.get("network_id")
    if network_id is not None:
        if not isinstance(network_id, int) or isinstance(network_id, bool):
            errors["network_id"] = "must be an integer"
        elif network_id not in network_ids:
            errors["network_id"] = "network does not belong to this app's developer"

    version_id = raw.get("version_id")
    if version_id is not None:
        if not isinstance(version_id, int) or isinstance(version_id, bool):
            errors["version_id"] = "must be an integer"
        elif version_id not in version_ids:
            errors["version_id"] = "published version does not exist for this app"

    experiment_id = raw.get("experiment_id")
    variant = raw.get("variant")
    if experiment_id is not None:
        if not isinstance(experiment_id, int) or isinstance(experiment_id, bool):
            errors["experiment_id"] = "must be an integer"
        elif experiment_id not in experiments:
            errors["experiment_id"] = "experiment does not exist for this app"
        else:
            experiment = experiments[experiment_id]
            if experiment.placement_id != (placement.id if placement else None):
                errors["experiment_id"] = "experiment belongs to another placement"
            if variant not in VALID_VARIANTS:
                errors["variant"] = "must be 'A' or 'B' when experiment_id is set"
    elif variant is not None:
        errors["variant"] = "variant requires experiment_id"

    occurred_at = raw.get("occurred_at")
    if not _parse_dt(occurred_at):
        errors["occurred_at"] = "required ISO-8601 datetime (UTC recommended)"

    # Type-specific fields.
    if event_type == EventType.REVENUE:
        amount = raw.get("amount")
        if amount is None:
            errors["amount"] = "required for revenue events"
        else:
            try:
                to_money(amount, field="amount")
            except ValueError as exc:
                errors["amount"] = str(exc)

    if event_type == EventType.FAILURE:
        reason = raw.get("failure_reason")
        if reason not in FailureReason.values:
            errors["failure_reason"] = (
                f"required for failure events, one of {', '.join(FailureReason.values)}"
            )

    return errors, event_id


def _parse_dt(value):
    if not isinstance(value, str):
        return None
    text = value.strip()
    if text.endswith("Z"):
        text = text[:-1] + "+00:00"
    try:
        parsed = datetime.fromisoformat(text)
    except ValueError:
        return None
    return parsed


def _build_kwargs(raw) -> dict:
    occurred_at = _parse_dt(raw["occurred_at"])
    kwargs = {
        "event_id": raw["event_id"],
        "event_type": raw["event_type"],
        "placement_id": raw["placement_id"],
        "network_id": raw.get("network_id"),
        "version_id": raw.get("version_id"),
        "experiment_id": raw.get("experiment_id"),
        "variant": raw.get("variant"),
        "failure_reason": raw.get("failure_reason"),
        "occurred_at": occurred_at,
    }
    if raw["event_type"] == EventType.REVENUE and raw.get("amount") is not None:
        kwargs["amount"] = to_money(raw["amount"], field="amount")
    user_key = raw.get("user_key")
    if user_key:
        kwargs["user_key_hash"] = hash_user_key(user_key)
    return kwargs
