"""Domain services for the catalog app.

Keeping transactional logic out of views makes it directly callable from
management commands and tests, and guarantees the same locking/audit
behaviour everywhere.
"""
from django.db import transaction

from .models import (
    AuditLog,
    ConfigVersion,
    ConfigVersionEntry,
    PlacementNetwork,
)


def record_audit(
    *, actor, app, action, target_type, target=None, target_repr="", payload=None
):
    return AuditLog.objects.create(
        actor=actor,
        app=app,
        action=action,
        target_type=target_type,
        target_id=str(getattr(target, "pk", "") or ""),
        target_repr=target_repr or str(target) if target else "",
        payload=payload or {},
    )


class PublishError(Exception):
    """Raised when a placement's draft cannot be published."""


@transaction.atomic
def publish_config(*, placement, published_by, note=""):
    """Snapshot the draft waterfall into a new immutable ConfigVersion.

    Concurrency
    -----------
    The placement row is locked (SELECT ... FOR UPDATE) so two concurrent
    publishes serialize. Each transaction re-reads the current max version
    under that lock, hence version numbers are gap-free and unique. The
    ``Placement.active_version`` pointer flips only after every
    ConfigVersionEntry row has been inserted: readers either see the previous
    complete version or the new complete version, never a partial one.

    Validation
    ----------
    * at least 1 enabled network
    * at most ``settings.MAX_NETWORKS_PER_PLACEMENT`` enabled networks
    * priorities 1..N without gaps/duplicates
    * fallback_order values 1..N without gaps/duplicates
    """
    locked_placement = (
        type(placement)
        .objects.select_for_update()
        .select_related("app")
        .get(pk=placement.pk)
    )

    lines = list(
        PlacementNetwork.objects.filter(placement=locked_placement, enabled=True)
        .select_related("network")
        .order_by("priority")
    )

    if not lines:
        raise PublishError("Cannot publish: the waterfall has no enabled networks.")

    max_networks = 8
    from django.conf import settings as dj_settings

    max_networks = getattr(dj_settings, "MAX_NETWORKS_PER_PLACEMENT", 8)
    if len(lines) > max_networks:
        raise PublishError(
            f"Cannot publish: {len(lines)} enabled networks exceeds the limit "
            f"of {max_networks}."
        )

    priorities = [line.priority for line in lines]
    fallbacks = sorted(line.fallback_order for line in lines)
    expected = list(range(1, len(lines) + 1))
    if sorted(priorities) != expected:
        raise PublishError(
            "Cannot publish: enabled network priorities must be exactly 1..N "
            f"without gaps or duplicates (got {sorted(priorities)})."
        )
    if fallbacks != expected:
        raise PublishError(
            "Cannot publish: fallback_order values must be exactly 1..N "
            f"without gaps or duplicates (got {fallbacks})."
        )

    current_max = (
        ConfigVersion.objects.select_for_update()
        .filter(placement=locked_placement)
        .select_related(None)
        .order_by("-version")
        .values_list("version", flat=True)
        .first()
    )
    next_version = (current_max or 0) + 1

    version = ConfigVersion.objects.create(
        placement=locked_placement,
        version=next_version,
        status=ConfigVersion.Status.PUBLISHED,
        published_by=published_by,
        note=note,
    )

    entries = [
        ConfigVersionEntry(
            version=version,
            network=line.network,
            network_code=line.network.code,
            network_name=line.network.display_name,
            priority=line.priority,
            cpm_floor=line.cpm_floor,
            fallback_order=line.fallback_order,
            enabled=True,
        )
        for line in lines
    ]
    ConfigVersionEntry.objects.bulk_create(entries)

    # Atomic switch of the live pointer.
    locked_placement.active_version = version
    locked_placement.save(update_fields=["active_version", "updated_at"])
    version.status = ConfigVersion.Status.ACTIVE
    version.save(update_fields=["status"], _allow_mutation=True)
    # Any previously active version goes back to plain "published"; history is
    # fully preserved.
    ConfigVersion.objects.filter(placement=locked_placement).exclude(
        pk=version.pk
    ).update(status=ConfigVersion.Status.PUBLISHED)

    record_audit(
        actor=published_by,
        app=locked_placement.app,
        action=AuditLog.Action.CONFIG_PUBLISH,
        target_type="ConfigVersion",
        target=version,
        target_repr=f"{locked_placement} v{next_version}",
        payload={
            "placement_code": locked_placement.code,
            "version": next_version,
            "network_count": len(entries),
            "note": note,
        },
    )
    return version
