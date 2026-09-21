"""Experiment lifecycle, stable assignment and per-variant statistics."""
from django.db import IntegrityError, transaction
from django.db.models import Count, Sum
from django.utils import timezone

from apps.catalog.models import ConfigVersion
from apps.catalog.services import record_audit
from apps.ingestion.models import AdEvent, EventType
from .models import Experiment, ExperimentAssignment, VARIANTS


class ExperimentError(Exception):
    pass


@transaction.atomic
def create_experiment(
    *, placement, created_by, name, variant_a_version_id, variant_b_version_id
):
    # Lock the placement so two "create experiment" calls cannot both see zero
    # running experiments; the DB partial unique constraint is the backstop.
    from apps.catalog.models import Placement

    Placement.objects.select_for_update().get(pk=placement.pk)

    if Experiment.objects.filter(
        placement=placement, status=Experiment.Status.RUNNING
    ).exists():
        raise ExperimentError(
            "A running experiment already exists for this placement."
        )

    if variant_a_version_id == variant_b_version_id:
        raise ExperimentError("Variant A and variant B must be different versions.")

    versions = {
        v.id: v
        for v in ConfigVersion.objects.filter(
            placement=placement,
            id__in=[variant_a_version_id, variant_b_version_id],
        )
    }
    if len(versions) != 2:
        raise ExperimentError(
            "Both variant versions must be published versions of this placement."
        )

    try:
        experiment = Experiment.objects.create(
            placement=placement,
            name=name,
            variant_a_version=versions[variant_a_version_id],
            variant_b_version=versions[variant_b_version_id],
            created_by=created_by,
        )
    except IntegrityError as exc:
        raise ExperimentError(
            "A running experiment already exists for this placement."
        ) from exc

    record_audit(
        actor=created_by,
        app=placement.app,
        action="experiment.create",
        target_type="Experiment",
        target=experiment,
        target_repr=name,
        payload={
            "placement_code": placement.code,
            "variant_a_version": versions[variant_a_version_id].version,
            "variant_b_version": versions[variant_b_version_id].version,
        },
    )
    return experiment


@transaction.atomic
def stop_experiment(*, experiment, actor):
    locked = (
        Experiment.objects.select_for_update()
        .filter(pk=experiment.pk)
        .first()
    )
    if locked.status != Experiment.Status.RUNNING:
        return locked
    locked.status = Experiment.Status.STOPPED
    locked.stopped_at = timezone.now()
    locked.save(update_fields=["status", "stopped_at"])
    record_audit(
        actor=actor,
        app=locked.placement.app,
        action="experiment.stop",
        target_type="Experiment",
        target=locked,
        target_repr=locked.name,
        payload={"placement_code": locked.placement.code},
    )
    return locked


def assign_variant(*, experiment, user_key_hash: str) -> str:
    """Return the user's fixed variant, persisting the assignment on first
    sight. Concurrent first-sight requests race onto a unique constraint;
    the loser re-reads the row that won — both callers observe the same
    variant.
    """
    user_key_hash = user_key_hash.strip().lower()
    existing = ExperimentAssignment.objects.filter(
        experiment=experiment, user_key_hash=user_key_hash
    ).first()
    if existing is not None:
        return existing.variant

    variant = experiment.bucket(user_key_hash)
    try:
        with transaction.atomic():
            ExperimentAssignment.objects.create(
                experiment=experiment,
                user_key_hash=user_key_hash,
                variant=variant,
            )
    except IntegrityError:
        row = ExperimentAssignment.objects.get(
            experiment=experiment, user_key_hash=user_key_hash
        )
        return row.variant
    return variant


def variant_config_version(experiment: Experiment, variant: str) -> ConfigVersion:
    return (
        experiment.variant_a_version
        if variant == "A"
        else experiment.variant_b_version
    )


def _empty_variant_stats():
    return {
        "impressions": 0,
        "fills": 0,
        "failures": 0,
        "revenue_events": 0,
        "revenue": "0.000000",
        "fill_rate": None,
        "ecpm": None,
        "assigned_users": 0,
    }


def experiment_stats(experiment: Experiment) -> dict:
    """Per-variant fill rate and eCPM.

    Statistical caliber (统计口径) — documented in README:

    * Population: events carrying ``experiment_id`` and the variant label
      *as recorded at exposure time*. Attribution is frozen on the event, so
      changing a config version never moves an impression between cohorts.
    * fill_rate = fills / impressions.
    * eCPM = total_revenue / impressions * 1000. Revenue events are summed
      with fixed-point Decimal; division is done in Decimal and quantized
      to 6 dp.
    * When a variant has zero impressions, fill_rate/eCPM are reported as
      ``null`` rather than 0 so missing data is never mistaken for bad data.
    """
    from apps.common.money import quantize_score

    return _aggregate_variant_stats(experiment, quantize_score)


def _aggregate_variant_stats(experiment, quantize_score):
    from decimal import Decimal

    stats = {v: _empty_variant_stats() for v in VARIANTS}

    rows = (
        AdEvent.objects.filter(experiment=experiment)
        .values("experiment_variant", "event_type")
        .annotate(n=Count("id"))
    )
    counts = {(r["experiment_variant"], r["event_type"]): r["n"] for r in rows}

    rev_rows = (
        AdEvent.objects.filter(
            experiment=experiment, event_type=EventType.REVENUE
        )
        .values("experiment_variant")
        .annotate(total=Sum("revenue"))
    )
    revenues = {
        r["experiment_variant"]: quantize_score(r["total"] or Decimal("0"))
        for r in rev_rows
    }

    assign_rows = (
        ExperimentAssignment.objects.filter(experiment=experiment)
        .values("variant")
        .annotate(n=Count("id"))
    )
    assigned = {r["variant"]: r["n"] for r in assign_rows}

    for variant in VARIANTS:
        bucket = stats[variant]
        impressions = counts.get((variant, EventType.IMPRESSION), 0)
        fills = counts.get((variant, EventType.FILL), 0)
        failures = counts.get((variant, EventType.FAILURE), 0)
        rev_events = counts.get((variant, EventType.REVENUE), 0)
        total_revenue = revenues.get(variant, Decimal("0"))
        bucket.update(
            impressions=impressions,
            fills=fills,
            failures=failures,
            revenue_events=rev_events,
            revenue=str(total_revenue),
            assigned_users=assigned.get(variant, 0),
        )
        if impressions > 0:
            bucket["fill_rate"] = str(
                quantize_score(Decimal(fills) / Decimal(impressions))
            )
            bucket["ecpm"] = str(
                quantize_score(total_revenue / Decimal(impressions) * Decimal(1000))
            )
    return stats
