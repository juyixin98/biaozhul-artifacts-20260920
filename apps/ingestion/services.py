"""Batch event ingestion service.

Acceptance model (per batch, max settings.EVENT_BATCH_MAX_SIZE items):

* ``accepted``  - newly persisted
* ``duplicates`` - event_id already known AND payload identical (idempotent
  replay, e.g. client timeout/retry)
* ``failed``     - per-item validation error or *conflicting* replay
  (same event_id, different payload -> ``conflict`` so mix-ups surface)

Out-of-order and late events are accepted as long as ``event_time`` is within
[now-EVENT_MAX_AGE_DAYS, now+EVENT_MAX_FUTURE_MINUTES]; scoring windows are
always derived from ``event_time``, never from arrival time.
"""
from dataclasses import dataclass, field
from datetime import timedelta
from decimal import Decimal, InvalidOperation

from django.conf import settings
from django.db import IntegrityError, transaction
from django.utils import timezone

from apps.catalog.models import AdNetwork, ConfigVersion, Placement
from apps.common.money import quantize_money
from .models import AdEvent, EventType


class BatchTooLarge(Exception):
    pass


@dataclass
class ItemResult:
    index: int
    event_id: str
    status: str  # accepted | duplicate | error
    error_code: str = ""
    message: str = ""
    field: str = ""

    def as_dict(self):
        out = {"index": self.index, "event_id": self.event_id, "status": self.status}
        if self.status == "error":
            out.update(
                error_code=self.error_code,
                message=self.message,
            )
            if self.field:
                out["field"] = self.field
        return out


@dataclass
class BatchResult:
    results: list = field(default_factory=list)

    @property
    def accepted(self):
        return sum(r.status == "accepted" for r in self.results)

    @property
    def duplicates(self):
        return sum(r.status == "duplicate" for r in self.results)

    @property
    def failed(self):
        return sum(r.status == "error" for r in self.results)

    def as_dict(self):
        return {
            "received": len(self.results),
            "accepted": self.accepted,
            "duplicates": self.duplicates,
            "failed": self.failed,
            "results": [r.as_dict() for r in self.results],
        }


def _err(index, event_id, code, message, field=""):
    return ItemResult(
        index=index,
        event_id=str(event_id or ""),
        status="error",
        error_code=code,
        message=message,
        field=field,
    )


def _parse_event_time(value):
    from django.utils.dateparse import parse_datetime

    if isinstance(value, str):
        dt = parse_datetime(value)
    else:
        dt = None
    if dt is None:
        return None
    if timezone.is_naive(dt):
        dt = timezone.make_aware(dt, timezone.utc)
    return dt


def _fingerprint_matches(stored: AdEvent, candidate: dict) -> bool:
    """Compare every attribution/business field of a replay candidate."""
    return (
        stored.event_type == candidate["event_type"]
        and stored.placement_id == candidate["placement_id"]
        and stored.network_id == candidate["network_id"]
        and stored.event_time == candidate["event_time"]
        and (stored.revenue or Decimal("0")) == (candidate["revenue"] or Decimal("0"))
        and (stored.error_code or "") == (candidate["error_code"] or "")
        and (stored.config_version_id or None)
        == (candidate["config_version_id"] or None)
        and (stored.experiment_id or None) == (candidate["experiment_id"] or None)
        and (stored.experiment_variant or "")
        == (candidate["experiment_variant"] or "")
        and (stored.user_key_hash or "") == (candidate["user_key_hash"] or "")
    )


def _normalize_item(index, raw, app, placement_map, network_map, experiment_model):
    """Validate one raw dict. Returns (candidate_dict, None) or (None, error)."""
    event_id = str(raw.get("event_id") or "").strip()
    if not event_id:
        return None, _err(index, event_id, "required", "event_id is required", "event_id")
    if len(event_id) > 64:
        return None, _err(
            index, event_id, "invalid", "event_id exceeds 64 characters", "event_id"
        )

    event_type = raw.get("event_type")
    if event_type not in EventType.values:
        return None, _err(
            index,
            event_id,
            "invalid_event_type",
            f"event_type must be one of {sorted(EventType.values)}",
            "event_type",
        )

    event_time = _parse_event_time(raw.get("event_time"))
    if event_time is None:
        return None, _err(
            index,
            event_id,
            "invalid_timestamp",
            "event_time must be an ISO-8601 datetime",
            "event_time",
        )
    now = timezone.now()
    if event_time < now - timedelta(days=settings.EVENT_MAX_AGE_DAYS):
        return None, _err(
            index,
            event_id,
            "event_too_old",
            f"event_time older than {settings.EVENT_MAX_AGE_DAYS} days",
            "event_time",
        )
    if event_time > now + timedelta(minutes=settings.EVENT_MAX_FUTURE_MINUTES):
        return None, _err(
            index,
            event_id,
            "event_in_future",
            "event_time is too far in the future",
            "event_time",
        )

    placement_code = raw.get("placement_code")
    placement = placement_map.get(placement_code)
    if placement is None:
        return None, _err(
            index,
            event_id,
            "unknown_placement",
            f"Unknown placement_code '{placement_code}' for this app",
            "placement_code",
        )

    network_code = raw.get("network_code")
    network = network_map.get(network_code)
    if network is None:
        return None, _err(
            index,
            event_id,
            "unknown_network",
            f"Unknown or inactive network_code '{network_code}'",
            "network_code",
        )

    # Revenue semantics.
    revenue = None
    raw_revenue = raw.get("revenue")
    if event_type == EventType.REVENUE:
        if raw_revenue is None:
            return None, _err(
                index,
                event_id,
                "required",
                "revenue is required for revenue events",
                "revenue",
            )
        try:
            revenue = quantize_money(raw_revenue)
        except (InvalidOperation, ValueError, TypeError):
            return None, _err(
                index,
                event_id,
                "invalid_money",
                "revenue must be a non-negative decimal string (6 dp max)",
                "revenue",
            )
        if revenue < Decimal("0"):
            return None, _err(
                index,
                event_id,
                "invalid_money",
                "revenue must be non-negative",
                "revenue",
            )
    elif raw_revenue is not None:
        return None, _err(
            index,
            event_id,
            "unexpected_field",
            "revenue is only allowed on revenue events",
            "revenue",
        )

    error_code = str(raw.get("error_code") or "")[:64]
    if event_type != EventType.FAILURE and error_code:
        return None, _err(
            index,
            event_id,
            "unexpected_field",
            "error_code is only allowed on failure events",
            "error_code",
        )

    # Config attribution (optional): any version of THIS placement, active or
    # superseded, is valid — SDKs may lag behind a publish.
    config_version_id = raw.get("config_version_id")
    if config_version_id is not None:
        version = ConfigVersion.objects.filter(
            id=config_version_id, placement=placement
        ).first()
        if version is None:
            return None, _err(
                index,
                event_id,
                "unknown_config_version",
                "config_version_id does not belong to this placement",
                "config_version_id",
            )
        config_version_id = version.id

    # Experiment attribution (optional).
    experiment_id = raw.get("experiment_id")
    variant = str(raw.get("experiment_variant") or "")
    experiment = None
    if experiment_id is not None:
        experiment = experiment_model.objects.filter(
            id=experiment_id, placement=placement
        ).first()
        if experiment is None:
            return None, _err(
                index,
                event_id,
                "unknown_experiment",
                "experiment_id does not belong to this placement",
                "experiment_id",
            )
        if variant not in ("A", "B"):
            return None, _err(
                index,
                event_id,
                "invalid_variant",
                "experiment_variant must be 'A' or 'B' when experiment_id is set",
                "experiment_variant",
            )
        experiment_id = experiment.id
    elif variant:
        return None, _err(
            index,
            event_id,
            "missing_experiment",
            "experiment_variant requires experiment_id",
            "experiment_id",
        )

    user_key_hash = str(raw.get("user_key_hash") or "").strip().lower()[:64]

    candidate = {
        "event_id": event_id,
        "event_type": event_type,
        "event_time": event_time,
        "placement_id": placement.id,
        "network_id": network.id,
        "revenue": revenue,
        "error_code": error_code,
        "config_version_id": config_version_id,
        "experiment_id": experiment_id,
        "experiment_variant": variant if experiment_id else "",
        "user_key_hash": user_key_hash,
    }
    return candidate, None


def ingest_batch(*, app, raw_events, now=None) -> BatchResult:
    if len(raw_events) > settings.EVENT_BATCH_MAX_SIZE:
        raise BatchTooLarge(
            f"batch exceeds {settings.EVENT_BATCH_MAX_SIZE} events "
            f"(got {len(raw_events)})"
        )

    from apps.experiments.models import Experiment

    placement_map = {p.code: p for p in Placement.objects.filter(app=app)}
    network_map = {n.code: n for n in AdNetwork.objects.filter(is_active=True)}

    candidates_by_id: dict[str, dict] = {}
    result = BatchResult()
    to_insert: list[tuple[int, dict]] = []

    for index, raw in enumerate(raw_events):
        if not isinstance(raw, dict):
            result.results.append(
                _err(index, "", "invalid_item", "each event must be a JSON object")
            )
            continue

        candidate, error = _normalize_item(
            index, raw, app, placement_map, network_map, Experiment
        )
        if error is not None:
            result.results.append(error)
            continue

        eid = candidate["event_id"]
        if eid in candidates_by_id:
            result.results.append(
                _err(
                    index,
                    eid,
                    "duplicate_in_batch",
                    "event_id appears more than once in this batch",
                    "event_id",
                )
            )
            continue
        candidates_by_id[eid] = candidate
        to_insert.append((index, candidate))

    # Pre-fetch already known ids (normal retries hit this path).
    existing = {
        e.event_id: e
        for e in AdEvent.objects.filter(event_id__in=list(candidates_by_id.keys()))
    }

    pending: list[tuple[int, dict]] = []
    for index, candidate in to_insert:
        stored = existing.get(candidate["event_id"])
        if stored is None:
            pending.append((index, candidate))
        elif _fingerprint_matches(stored, candidate):
            result.results.append(
                ItemResult(index, candidate["event_id"], "duplicate")
            )
        else:
            result.results.append(
                _err(
                    index,
                    candidate["event_id"],
                    "conflict",
                    "event_id already exists with a different payload",
                )
            )

    # Insert new rows; handle races where a concurrent request won the insert.
    # Each save gets its own savepoint: a unique-violation rolls back just that
    # row and the loop keeps going.
    for index, candidate in pending:
        event = AdEvent(app=app, **candidate)
        try:
            with transaction.atomic():
                event.save()
        except IntegrityError:
            stored = AdEvent.objects.filter(event_id=candidate["event_id"]).first()
            if stored is not None and _fingerprint_matches(stored, candidate):
                result.results.append(
                    ItemResult(index, candidate["event_id"], "duplicate")
                )
            else:
                result.results.append(
                    _err(
                        index,
                        candidate["event_id"],
                        "conflict",
                        "event_id already exists with a different payload",
                    )
                )
        else:
            result.results.append(
                ItemResult(index, candidate["event_id"], "accepted")
            )

    result.results.sort(key=lambda r: r.index)
    return result
