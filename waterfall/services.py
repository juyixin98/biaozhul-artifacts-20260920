"""Draft building / publishing service for waterfall configurations.

Concurrency model
-----------------
Every mutating operation takes a ``SELECT ... FOR UPDATE`` row lock on the
placement for the duration of the transaction. Consequently:

* two concurrent ``create_or_replace_draft`` calls serialize -- the second
  replaces the draft the first produced (it never creates a second draft);
* two concurrent ``publish`` calls serialize -- the loser observes the draft
  already published and receives a 409 instead of double-freezing / producing
  two versions with the same number.

The version freeze and the ``CurrentVersion`` pointer swap happen in the same
transaction, which is the atomic switch required by the spec.
"""
from __future__ import annotations

from django.db import transaction
from django.db.models import Max
from django.utils import timezone

from applications.models import AdNetwork, Placement
from common.audit import record_audit
from common.exceptions import ConflictError, NotFoundError, ValidationError
from common.utils import to_money

from .models import CurrentVersion, WaterfallEntry, WaterfallVersion


def _validate_entries(placement: Placement, entries_data: list[dict]) -> list[dict]:
    """Validate and normalize the waterfall payload.

    Rules:
      * 1..:data:`settings.MAX_NETWORKS_PER_PLACEMENT` entries;
      * every network must belong to the placement owner;
      * networks distinct;
      * ``priority`` and ``fallback_order`` are each a permutation of 1..N;
      * floors are fixed-point non-negative decimals.
    """
    from django.conf import settings

    if not isinstance(entries_data, list) or not entries_data:
        raise ValidationError("entries must be a non-empty list")
    n = len(entries_data)
    if n > settings.MAX_NETWORKS_PER_PLACEMENT:
        raise ValidationError(
            f"a placement may reference at most "
            f"{settings.MAX_NETWORKS_PER_PLACEMENT} networks (got {n})"
        )

    normalized: list[dict] = []
    network_ids: list[int] = []
    owner_id = placement.app.developer_id

    for i, raw in enumerate(entries_data):
        prefix = f"entries[{i}]"
        network_id = raw.get("network_id")
        if network_id is None:
            raise ValidationError(f"{prefix}.network_id is required")
        try:
            network = AdNetwork.objects.get(pk=network_id)
        except AdNetwork.DoesNotExist as exc:
            raise ValidationError(f"{prefix}.network_id {network_id} not found") from exc
        if network.developer_id != owner_id:
            raise ValidationError(
                f"{prefix}.network_id {network_id} belongs to another developer"
            )
        network_ids.append(network.id)

        try:
            priority = int(raw["priority"])
            fallback_order = int(raw["fallback_order"])
        except (KeyError, TypeError, ValueError) as exc:
            raise ValidationError(
                f"{prefix}: priority and fallback_order must be integers"
            ) from exc
        if priority < 1 or fallback_order < 1:
            raise ValidationError(f"{prefix}: ordering values start at 1")

        try:
            floor = to_money(raw.get("floor_cpm", "0"), field=f"{prefix}.floor_cpm")
        except ValueError as exc:
            raise ValidationError(str(exc)) from exc

        normalized.append(
            {
                "network": network,
                "priority": priority,
                "fallback_order": fallback_order,
                "floor_cpm": floor,
                "is_enabled": bool(raw.get("is_enabled", True)),
            }
        )

    if len(set(network_ids)) != n:
        raise ValidationError("entries must reference distinct networks")

    priorities = sorted(e["priority"] for e in normalized)
    fallbacks = sorted(e["fallback_order"] for e in normalized)
    expected = list(range(1, n + 1))
    if priorities != expected:
        raise ValidationError(
            f"priority must be a permutation of 1..{n} (got {priorities})"
        )
    if fallbacks != expected:
        raise ValidationError(
            f"fallback_order must be a permutation of 1..{n} (got {fallbacks})"
        )
    return normalized


@transaction.atomic
def create_or_replace_draft(
    *,
    placement: Placement,
    entries_data: list[dict],
    developer,
    note: str = "",
) -> WaterfallVersion:
    locked = Placement.objects.select_for_update().select_related("app").get(pk=placement.pk)

    normalized = _validate_entries(locked, entries_data)

    existing = (
        WaterfallVersion.objects.filter(
            placement=locked, status=WaterfallVersion.Status.DRAFT
        )
        .select_for_update()
        .first()
    )
    if existing is None:
        draft = WaterfallVersion.objects.create(
            placement=locked,
            status=WaterfallVersion.Status.DRAFT,
            note=note[:255],
            created_by=developer,
        )
    else:
        draft = existing
        draft.entries.all().delete()
        draft.note = note[:255]
        draft.created_by = developer
        draft.save(update_fields=["note", "created_by", "updated_at"])

    WaterfallEntry.objects.bulk_create(
        [WaterfallEntry(version=draft, **entry) for entry in normalized]
    )

    record_audit(
        developer=developer,
        action="update" if existing else "create",
        resource_type="waterfall_draft",
        resource_id=draft.id,
        resource_repr=f"{locked.placement_key} draft ({len(normalized)} entries)",
        diff={"placement_id": locked.id, "entries": _entries_payload(normalized)},
    )
    return draft


@transaction.atomic
def publish_draft(*, placement: Placement, developer) -> WaterfallVersion:
    """Freeze the draft as the next immutable version and atomically promote it."""
    locked = Placement.objects.select_for_update().select_related("app").get(pk=placement.pk)

    draft = (
        WaterfallVersion.objects.filter(
            placement=locked, status=WaterfallVersion.Status.DRAFT
        )
        .select_for_update()
        .prefetch_related("entries__network")
        .first()
    )
    if draft is None:
        raise NotFoundError("no draft exists for this placement")

    # Re-validate: a draft may contain references to a network that was
    # deactivated after the draft was written.
    entries = list(draft.entries.select_related("network"))
    _validate_entries(
        locked,
        [
            {
                "network_id": e.network_id,
                "priority": e.priority,
                "fallback_order": e.fallback_order,
                "floor_cpm": str(e.floor_cpm),
                "is_enabled": e.is_enabled,
            }
            for e in entries
        ],
    )

    # Defensive double-publish guard (row lock already serializes contenders,
    # but a stale ORM instance must never slip through).
    draft.refresh_from_db(fields=["status"])
    if draft.status != WaterfallVersion.Status.DRAFT:
        raise ConflictError("draft was already published by a concurrent request")

    max_number = (
        WaterfallVersion.objects.filter(
            placement=locked, status=WaterfallVersion.Status.PUBLISHED
        )
        .select_for_update()
        .aggregate(m=Max("version_number"))["m"]
    ) or 0
    next_number = max_number + 1

    draft.status = WaterfallVersion.Status.PUBLISHED
    draft.version_number = next_number
    draft.published_by = developer
    draft.published_at = timezone.now()
    draft.save(
        update_fields=[
            "status",
            "version_number",
            "published_by",
            "published_at",
            "updated_at",
        ]
    )

    CurrentVersion.objects.update_or_create(
        placement=locked, defaults={"version": draft}
    )

    record_audit(
        developer=developer,
        action="publish",
        resource_type="waterfall_version",
        resource_id=draft.id,
        resource_repr=f"{locked.placement_key} v{next_number}",
        diff={
            "placement_id": locked.id,
            "version_number": next_number,
            "version_id": draft.id,
        },
    )
    return draft


def _entries_payload(normalized: list[dict]) -> list[dict]:
    return [
        {
            "network_id": e["network"].id,
            "network_code": e["network"].code,
            "priority": e["priority"],
            "fallback_order": e["fallback_order"],
            "floor_cpm": str(e["floor_cpm"]),
            "is_enabled": e["is_enabled"],
        }
        for e in normalized
    ]
