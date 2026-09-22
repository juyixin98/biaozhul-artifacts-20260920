"""Experiment lifecycle, stable assignment and per-variant statistics."""
from __future__ import annotations

from decimal import Decimal

from django.db import transaction
from django.db.models import Count, Q, Sum
from django.utils import timezone

from applications.models import Placement
from common.audit import record_audit
from common.exceptions import ConflictError, ValidationError
from common.utils import ZERO, hash_user_key, stable_bucket, weighted_midpoint
from events.models import Event, EventType
from waterfall.models import WaterfallVersion

from .models import Assignment, Experiment, VariantVersion


@transaction.atomic
def create_experiment(
    *,
    placement: Placement,
    name: str,
    version_a_id: int,
    version_b_id: int,
    developer,
) -> Experiment:
    if version_a_id == version_b_id:
        raise ValidationError("variant A and variant B must use different versions")

    versions = {
        v.id: v
        for v in WaterfallVersion.objects.filter(
            id__in=[version_a_id, version_b_id],
            placement=placement,
            status=WaterfallVersion.Status.PUBLISHED,
        )
    }
    missing = {version_a_id, version_b_id} - set(versions)
    if missing:
        raise ValidationError(
            f"versions {sorted(missing)} are not published versions of this placement"
        )

    experiment = Experiment.objects.create(
        placement=placement, name=name[:255]
    )
    VariantVersion.objects.bulk_create(
        [
            VariantVersion(experiment=experiment, variant="A", version=versions[version_a_id]),
            VariantVersion(experiment=experiment, variant="B", version=versions[version_b_id]),
        ]
    )
    record_audit(
        developer=developer,
        action="create",
        resource_type="experiment",
        resource_id=experiment.id,
        resource_repr=experiment.public_key,
        diff={
            "placement_id": placement.id,
            "version_a": version_a_id,
            "version_b": version_b_id,
        },
    )
    return experiment


@transaction.atomic
def start_experiment(*, experiment: Experiment, developer) -> Experiment:
    locked = Experiment.objects.select_for_update().get(pk=experiment.pk)
    if locked.status == Experiment.Status.RUNNING:
        raise ConflictError("experiment is already running")
    if locked.status == Experiment.Status.STOPPED:
        raise ConflictError("a stopped experiment cannot be restarted; create a new one")
    bound_count = locked.variants.count()
    if bound_count != 2:
        raise ValidationError(f"experiment must bind exactly two variants (has {bound_count})")
    locked.status = Experiment.Status.RUNNING
    locked.started_at = timezone.now()
    locked.save(update_fields=["status", "started_at"])
    record_audit(
        developer=developer,
        action="experiment_start",
        resource_type="experiment",
        resource_id=locked.id,
        resource_repr=locked.public_key,
        diff={"status": locked.status},
    )
    return locked


@transaction.atomic
def stop_experiment(*, experiment: Experiment, developer) -> Experiment:
    locked = Experiment.objects.select_for_update().get(pk=experiment.pk)
    if locked.status != Experiment.Status.RUNNING:
        raise ConflictError("only a running experiment can be stopped")
    locked.status = Experiment.Status.STOPPED
    locked.stopped_at = timezone.now()
    locked.save(update_fields=["status", "stopped_at"])
    record_audit(
        developer=developer,
        action="experiment_stop",
        resource_type="experiment",
        resource_id=locked.id,
        resource_repr=locked.public_key,
        diff={"status": locked.status},
    )
    return locked


def assign_variant(*, experiment: Experiment, user_key: str) -> tuple[str, int, bool]:
    """Resolve the stable 50/50 bucket, recording the first sighting.

    Returns ``(variant, version_id, tracked)``. ``tracked`` is True when a new
    assignment row was written; repeat callers always get the same variant.
    """
    variant = stable_bucket(experiment.public_key, user_key)
    version_id = (
        experiment.variants.get(variant=variant).version_id
    )
    user_hash = hash_user_key(user_key)
    _, created = Assignment.objects.get_or_create(
        experiment=experiment,
        user_key_hash=user_hash,
        defaults={"variant": variant},
    )
    # Defense in depth: a stored row that disagrees with the hash would mean a
    # bug, since neither input ever changes. Never silently switch groups.
    if not created:
        stored = Assignment.objects.get(
            experiment=experiment, user_key_hash=user_hash
        ).variant
        if stored != variant:
            variant = stored
            version_id = experiment.variants.get(variant=variant).version_id
    return variant, version_id, created


def experiment_stats(experiment: Experiment) -> dict:
    """Per-variant fill rate and eCPM, computed from frozen attribution.

    Definitions (documented and identical to the scorer's口径):

    * **fill rate** = fills / (fills + failures)
    * **eCPM**      = revenue (USD) * 1000 / impressions

    Variants with no (fills + failures) have no fill-rate sample and report
    ``null`` rather than a misleading 0; likewise eCPM with no impressions is
    ``null``. Counts are always returned so callers can see sample sizes.
    """
    base = Event.objects.filter(experiment=experiment).values("variant")
    agg = base.annotate(
        impressions=Count("id", filter=Q(event_type=EventType.IMPRESSION)),
        fills=Count("id", filter=Q(event_type=EventType.FILL)),
        failures=Count("id", filter=Q(event_type=EventType.FAILURE)),
        revenue=Sum("amount", filter=Q(event_type=EventType.REVENUE)),
    )
    by_variant = {row["variant"]: row for row in agg}

    result = {}
    for variant in ("A", "B"):
        row = by_variant.get(variant) or {}
        impressions = row.get("impressions", 0)
        fills = row.get("fills", 0)
        failures = row.get("failures", 0)
        revenue = row.get("revenue")
        if revenue is None:
            revenue = ZERO
        elif not isinstance(revenue, Decimal):  # SQLite can return float from SUM
            revenue = Decimal(str(revenue))
        opportunities = fills + failures

        fill_rate = (
            weighted_midpoint(Decimal(fills), Decimal(opportunities))
            if opportunities
            else None
        )
        ecpm = (
            weighted_midpoint((revenue * Decimal(1000)), Decimal(impressions))
            if impressions
            else None
        )
        result[variant] = {
            "impressions": impressions,
            "fills": fills,
            "failures": failures,
            "revenue_usd": str(revenue.quantize(Decimal("0.000001"))) if isinstance(revenue, Decimal) else str(revenue),
            "fill_rate": str(fill_rate) if fill_rate is not None else None,
            "ecpm_usd": str(ecpm) if ecpm is not None else None,
            "assigned_users": Assignment.objects.filter(
                experiment=experiment, variant=variant
            ).count(),
        }
    return result
