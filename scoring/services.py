"""Deterministic network scoring for 30-minute ticks.

Methodology (the exact口径 the API documents):

1. **Window**: half-open ``[slot_start - 7d, slot_start)`` on event
   ``occurred_at``. The window contains the last 7 complete days of data.

2. **Candidates per placement**: every network that either
   a. appears in the placement's *currently published* waterfall version, or
   b. produced any fill/failure/revenue/impression event in the window.
   This means a network removed from the waterfall still appears in historical
   comparisons (it has real samples), while a newly configured network with no
   samples yet is explicitly visible with zero/empty metrics.

3. **Raw metrics** (all fixed-point Decimal; never floats):
   * fill rate   = fills / (fills + failures)              [0, 1]
   * reliability  = opportunities_without_error / (fills + failures)
     where ``no_fill`` failures are *not* errors (they are commercial misses,
     already penalized via fill rate); timeout/error failures are.
   * eCPM (USD)   = revenue * 1000 / impressions

4. **Missing samples**: when ``fills + failures == 0`` the network has no
   fill/reliability samples -- raw values are 0 and the network is excluded
   from the min-max reference population for those metrics (a 0 forced into
   normalization would otherwise drag every real network down). With no
   impressions, eCPM is treated the same way. A network with no samples at all
   scores 0 overall.

5. **Normalization**: per placement and per metric, min-max over the reference
   population: ``(x - min) / (max - min)`` with half-even rounding at 9 dp.
   When ``max == min`` every member gets 1.0 (all tied, all get full credit;
   this keeps a single-network waterfall at its real quality rather than 0).

6. **Total** = 100 * (0.40*n_fill + 0.35*n_ecpm + 0.25*n_reliability),
   rounded half-even to 6 dp.

7. **Tie-breaking** (total score equal): (i) higher raw fill rate, then
   (ii) higher raw eCPM, (iii) higher raw reliability, (iv) lower network id.
   Rule (iv) guarantees byte-identical ordering on repeated runs regardless of
   database row order.
"""
from __future__ import annotations

import hashlib
import json
from decimal import Decimal, localcontext

from django.db import transaction
from django.db.models import Count, Q, Sum
from django.utils import timezone

from applications.models import AdNetwork, Placement
from common.utils import ZERO
from events.models import ERROR_REASONS, Event, EventType
from waterfall.models import WaterfallVersion

from .models import ScoreItem, ScoreRun
from .slots import window_for_slot

RATE_PLACES = Decimal("0.000000001")  # 9 decimal places
SCORE_PLACES = Decimal("0.000001")
ONE = Decimal("1")
HUNDRED = Decimal("100")
THOUSAND = Decimal("1000")


def _q9(value: Decimal) -> Decimal:
    return value.quantize(RATE_PLACES)


def _q6(value: Decimal) -> Decimal:
    return value.quantize(SCORE_PLACES)


def _ratio(numerator: int | Decimal, denominator: int | Decimal) -> Decimal:
    with localcontext() as ctx:
        ctx.prec = 28
        if denominator == 0:
            return ZERO
        return Decimal(numerator) / Decimal(denominator)


def _normalize(values: dict[int, Decimal]) -> dict[int, Decimal]:
    """Min-max normalize; members with NULL-like samples are simply absent.

    ``values`` maps candidate-id -> raw metric for candidates that have a
    sample for this metric. Candidates without a sample are normalized to 0 by
    the caller.
    """
    if not values:
        return {}
    lo = min(values.values())
    hi = max(values.values())
    if hi == lo:
        return {key: ONE for key in values}
    span = hi - lo
    out = {}
    for key, value in values.items():
        out[key] = _q9((value - lo) / span)
    return out


@transaction.atomic
def run_scoring(*, slot_start=None, now=None) -> ScoreRun:
    """Build (or return the existing) finalized run for a 30-minute slot."""
    now = now or timezone.now()
    slot_start, window_start, window_end = window_for_slot(slot_start or now)

    # Idempotency: lock the run row so concurrent schedulers serialize; the
    # winner builds the run, the loser returns the identical finalized result.
    existing = ScoreRun.objects.select_for_update().filter(slot_start=slot_start).first()
    if existing is not None and existing.finalized:
        return existing

    if existing is None:
        run = ScoreRun.objects.create(
            slot_start=slot_start,
            window_start=window_start,
            window_end=window_end,
        )
    else:
        run = existing
        run.items.all().delete()

    items_to_create: list[ScoreItem] = []
    item_payload: list[dict] = []

    placements = list(Placement.objects.select_related("app").all())
    for placement in placements:
        items_to_create.extend(
            _score_placement(run, placement, window_start, window_end, item_payload)
        )

    ScoreItem.objects.bulk_create(items_to_create)

    # Deterministic content hash over canonical payload. Inputs that produce
    # identical items produce identical hashes -- useful for verifying that a
    # repeated schedule gives the same answer.
    canonical = json.dumps(item_payload, sort_keys=True, separators=(",", ":"))
    run.items_hash = hashlib.sha256(canonical.encode("utf-8")).hexdigest()
    run.finalized = True
    run.finalized_at = timezone.now()
    run.save(update_fields=["items_hash", "finalized", "finalized_at"])
    return run


def _score_placement(run, placement, window_start, window_end, item_payload):
    # Candidate networks: current published version + any active in window.
    current_version = (
        WaterfallVersion.objects.filter(
            placement=placement, status=WaterfallVersion.Status.PUBLISHED
        )
        .order_by("-version_number")
        .first()
    )
    candidate_ids: set[int] = set()
    if current_version is not None:
        candidate_ids.update(
            current_version.entries.values_list("network_id", flat=True)
        )
    candidate_ids.update(
        Event.objects.filter(
            placement=placement,
            occurred_at__gte=window_start,
            occurred_at__lt=window_end,
        )
        .exclude(network_id=None)
        .values_list("network_id", flat=True).distinct()
    )
    if not candidate_ids:
        return []

    networks = {
        n.id: n for n in AdNetwork.objects.filter(id__in=candidate_ids)
    }
    candidate_ids = set(networks)  # drop dangling references

    aggregates = (
        Event.objects.filter(
            placement=placement,
            network_id__in=candidate_ids,
            occurred_at__gte=window_start,
            occurred_at__lt=window_end,
        )
        .values("network_id")
        .annotate(
            impressions=Count("id", filter=Q(event_type=EventType.IMPRESSION)),
            fills=Count("id", filter=Q(event_type=EventType.FILL)),
            failures=Count("id", filter=Q(event_type=EventType.FAILURE)),
            errors=Count(
                "id",
                filter=Q(
                    event_type=EventType.FAILURE,
                    failure_reason__in=list(ERROR_REASONS),
                ),
            ),
            revenue=Sum(
                "amount", filter=Q(event_type=EventType.REVENUE)
            ),
        )
    )
    raw: dict[int, dict] = {}
    for net_id in candidate_ids:
        raw[net_id] = {
            "impressions": 0, "fills": 0, "failures": 0, "errors": 0,
            "revenue": ZERO,
        }
    for row in aggregates:
        net_id = row["network_id"]
        revenue = row["revenue"]
        if revenue is None:
            revenue = ZERO
        elif not isinstance(revenue, Decimal):
            revenue = Decimal(str(revenue))
        raw[net_id] = {
            "impressions": row["impressions"],
            "fills": row["fills"],
            "failures": row["failures"],
            "errors": row["errors"],
            "revenue": revenue,
        }

    # Raw metrics.
    fill_samples: dict[int, Decimal] = {}
    reliability_samples: dict[int, Decimal] = {}
    ecpm_samples: dict[int, Decimal] = {}
    for net_id, m in raw.items():
        opportunities = m["fills"] + m["failures"]
        m["opportunities"] = opportunities
        if opportunities:
            m["fill_rate"] = _q9(_ratio(m["fills"], opportunities))
            m["reliability"] = _q9(
                _ratio(opportunities - m["errors"], opportunities)
            )
            fill_samples[net_id] = m["fill_rate"]
            reliability_samples[net_id] = m["reliability"]
        else:
            m["fill_rate"] = ZERO
            m["reliability"] = ZERO
        if m["impressions"]:
            m["ecpm"] = _q6(_ratio(m["revenue"] * THOUSAND, m["impressions"]))
            ecpm_samples[net_id] = m["ecpm"]
        else:
            m["ecpm"] = ZERO

    norm_fill = _normalize(fill_samples)
    norm_rel = _normalize(reliability_samples)
    norm_ecpm = _normalize(ecpm_samples)

    from django.conf import settings

    wf = Decimal(str(settings.SCORE_WEIGHT_FILL_RATE))
    we = Decimal(str(settings.SCORE_WEIGHT_ECPM))
    wr = Decimal(str(settings.SCORE_WEIGHT_RELIABILITY))

    scored = []
    for net_id in candidate_ids:
        m = raw[net_id]
        nf = norm_fill.get(net_id, ZERO)
        nr = norm_rel.get(net_id, ZERO)
        ne = norm_ecpm.get(net_id, ZERO)
        total = _q6(HUNDRED * (wf * nf + we * ne + wr * nr))
        scored.append((net_id, m, nf, ne, nr, total))

    # Deterministic ranking: total desc, raw fill desc, ecpm desc,
    # reliability desc, network id asc.
    scored.sort(
        key=lambda t: (
            -t[5], -t[1]["fill_rate"], -t[1]["ecpm"], -t[1]["reliability"], t[0]
        )
    )

    items = []
    for rank, (net_id, m, nf, ne, nr, total) in enumerate(scored, start=1):
        item = ScoreItem(
            run=run,
            placement=placement,
            network=networks[net_id],
            fills=m["fills"],
            failures=m["failures"],
            errors=m["errors"],
            impressions=m["impressions"],
            revenue=_q6(m["revenue"]),
            opportunities=m["opportunities"],
            fill_rate=m["fill_rate"],
            ecpm=m["ecpm"],
            reliability=m["reliability"],
            norm_fill_rate=nf,
            norm_ecpm=ne,
            norm_reliability=nr,
            total_score=total,
            rank=rank,
        )
        items.append(item)
        item_payload.append(
            {
                "placement_id": placement.id,
                "network_id": net_id,
                "fills": m["fills"],
                "failures": m["failures"],
                "errors": m["errors"],
                "impressions": m["impressions"],
                "revenue": str(_q6(m["revenue"])),
                "fill_rate": str(m["fill_rate"]),
                "ecpm": str(m["ecpm"]),
                "reliability": str(m["reliability"]),
                "norm_fill_rate": str(nf),
                "norm_ecpm": str(ne),
                "norm_reliability": str(nr),
                "total_score": str(total),
                "rank": rank,
            }
        )
    return items
