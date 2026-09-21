"""Network scoring algorithm.

Runs every 30 minutes over the trailing 7 days of *event_time* (so late and
out-of-order events are naturally included in later runs and explicit
backfills).

Metrics
-------
For every (placement, network) that has >= 1 impression in the window:

* fill_rate   = fills / impressions
* ecpm        = total_revenue / impressions * 1000   (currency per 1000
                impressions; Decimal fixed-point)
* reliability = successes / (successes + failures)
  where successes = fills + revenue events, failures = failure events.
  (fill without explicit failure; revenue without fill both count as
  successful outcomes — either proves the network responded.)

A (placement, network) with zero impressions is *not* scored: it has no
sample, and inventing a score would pollute the ranking. Networks currently
configured on the placement but with zero impressions are listed separately
on the run payload so the gap is visible rather than silently ranked.

Normalization (per placement, across that placement's scored networks)
----------------------------------------------------------------------
Min-max scaling to [0, 1]:

    n(x) = (x - min) / (max - min)

* when max == min (incl. all values equal) every network gets 0.5 for that
  component — the component carries no ranking information, so it neither
  rewards nor penalizes anyone;
* a component with no sample at all for a network (e.g. reliability NULL
  because that network had no fill/failure/revenue signals) is treated as
  neutral 0.5 in the weighted sum, but the normalized column stays NULL so
  "missing" is never confused with "average performance".

Weighted score
--------------
score = 0.40 * fill_rate_norm + 0.35 * ecpm_norm + 0.25 * reliability_norm
(each missing component contributes 0.25/0.40/0.35 weight * 0.5)

Tie-breaking (deterministic, identical on repeated runs)
--------------------------------------------------------
1. score descending
2. fill_rate raw descending
3. ecpm raw descending
4. impressions descending (more samples win)
5. network_code ascending (lexicographic, stable final arbiter)

All Decimal math is quantized to 6 dp with ROUND_HALF_UP.
"""
import hashlib
from dataclasses import dataclass
from datetime import timedelta
from decimal import Decimal

from django.conf import settings
from django.db import transaction
from django.db.models import Count, Sum
from django.utils import timezone

from apps.common.money import quantize_score
from apps.ingestion.models import AdEvent, EventType
from .models import NetworkScore, ScoreRun, ScoreRunStatus

NEUTRAL = Decimal("0.5")
ZERO = Decimal("0")


# ---------------------------------------------------------------------------
# Period helpers
# ---------------------------------------------------------------------------

def floor_to_period(dt, minutes=None) -> "timezone.datetime":
    minutes = minutes or settings.SCORING_PERIOD_MINUTES
    floored_minute = (dt.minute // minutes) * minutes
    return dt.replace(minute=floored_minute, second=0, microsecond=0)


def latest_closed_period(now=None, minutes=None):
    """The most recent fully-elapsed period start.

    At 12:30 sharp the 12:00-12:30 period is closed and returned; at 12:29
    the 11:30 period is returned.
    """
    minutes = minutes or settings.SCORING_PERIOD_MINUTES
    now = now or timezone.now()
    boundary = floor_to_period(now, minutes)
    if now == boundary:
        boundary -= timedelta(minutes=minutes)
    return boundary


def period_window(period_start, minutes=None, days=None):
    minutes = minutes or settings.SCORING_PERIOD_MINUTES
    days = days or settings.SCORING_WINDOW_DAYS
    period_end = period_start + timedelta(minutes=minutes)
    return period_start, period_end, period_start - timedelta(days=days), period_end


# ---------------------------------------------------------------------------
# Aggregation
# ---------------------------------------------------------------------------

@dataclass
class Aggregate:
    placement_id: int
    network_id: int
    impressions: int = 0
    fills: int = 0
    failures: int = 0
    revenue_events: int = 0
    revenue: Decimal = ZERO

    @property
    def fill_rate(self):
        if self.impressions == 0:
            return None
        return quantize_score(Decimal(self.fills) / Decimal(self.impressions))

    @property
    def ecpm(self):
        if self.impressions == 0:
            return None
        return quantize_score(
            self.revenue / Decimal(self.impressions) * Decimal(1000)
        )

    @property
    def reliability(self):
        signals = self.fills + self.revenue_events + self.failures
        if signals == 0:
            return None
        successes = self.fills + self.revenue_events
        return quantize_score(Decimal(successes) / Decimal(signals))


def _aggregate_window(window_start, window_end) -> dict:
    """Aggregate events whose event_time falls in [window_start, window_end)."""
    base = AdEvent.objects.filter(
        event_time__gte=window_start, event_time__lt=window_end
    )
    counts = {
        (r["placement_id"], r["network_id"], r["event_type"]): r["n"]
        for r in base.values("placement_id", "network_id", "event_type")
        .annotate(n=Count("id"))
    }
    revenues = {
        (r["placement_id"], r["network_id"]): r["total"] or ZERO
        for r in base.filter(event_type=EventType.REVENUE)
        .values("placement_id", "network_id")
        .annotate(total=Sum("revenue"))
    }

    aggregates: dict[tuple[int, int], Aggregate] = {}
    for (placement_id, network_id, event_type), n in counts.items():
        key = (placement_id, network_id)
        agg = aggregates.setdefault(key, Aggregate(placement_id, network_id))
        if event_type == EventType.IMPRESSION:
            agg.impressions = n
        elif event_type == EventType.FILL:
            agg.fills = n
        elif event_type == EventType.FAILURE:
            agg.failures = n
        elif event_type == EventType.REVENUE:
            agg.revenue_events = n
        agg.revenue = revenues.get(key, ZERO)
    return aggregates


def _minmax(values):
    """Return a normalize fn for one component across one placement.

    Networks with no sample (None) are excluded from min/max *and* from the
    all-equal decision: a missing network must not shrink the range seen by
    networks that do have data. Such a network normalizes to None and the
    weighted sum substitutes the neutral 0.5 for that component only.
    """
    present = [v for v in values if v is not None]
    if not present:
        return lambda v: None
    lo, hi = min(present), max(present)
    if hi == lo:
        return lambda v: NEUTRAL if v is not None else None
    span = hi - lo

    def normalize(v):
        if v is None:
            return None
        return quantize_score((v - lo) / span)

    return normalize


# ---------------------------------------------------------------------------
# Run execution
# ---------------------------------------------------------------------------

def _score_checksum(rows):
    """Deterministic fingerprint of the generated score rows."""
    h = hashlib.sha256()
    for row in sorted(rows, key=lambda r: (r.placement_id, r.rank, r.network_id)):
        h.update(
            "|".join(
                [
                    str(row.placement_id),
                    str(row.network_id),
                    str(row.impressions),
                    str(row.fills),
                    str(row.failures),
                    str(row.revenue),
                    str(row.fill_rate or ""),
                    str(row.ecpm or ""),
                    str(row.reliability or ""),
                    str(row.score),
                    str(row.rank),
                ]
            ).encode("utf-8")
        )
        h.update(b"\n")
    return h.hexdigest()


@transaction.atomic
def run_scoring(period_start, *, force=False, now=None):
    """Generate scores for one 30-minute period.

    Returns (ScoreRun, created: bool). With force=False an existing complete
    run for the period is returned untouched — this is what makes the
    scheduler repeat-safe and concurrent tickers harmless.
    """
    period_start, period_end, window_start, window_end = period_window(period_start)

    existing = ScoreRun.objects.select_for_update().filter(
        period_start=period_start
    ).first()
    if existing is not None and existing.status == ScoreRunStatus.COMPLETE and not force:
        return existing, False
    if existing is None:
        run = ScoreRun.objects.create(
            period_start=period_start,
            period_end=period_end,
            window_start=window_start,
            window_end=window_end,
            status=ScoreRunStatus.RUNNING,
        )
    else:
        run = existing
        run.status = ScoreRunStatus.RUNNING
        run.error = ""
        run.save(update_fields=["status", "error"])
        run.network_scores.all().delete()

    try:
        rows = _build_score_rows(run, window_start, window_end)
        NetworkScore.objects.bulk_create(rows)
        run.checksum = _score_checksum(rows)
        run.status = ScoreRunStatus.COMPLETE
        run.completed_at = timezone.now()
        run.save(
            update_fields=["checksum", "status", "completed_at", "error"]
        )
        _activate_run(run)
    except Exception as exc:  # pragma: no cover - defensive
        run.status = ScoreRunStatus.FAILED
        run.error = str(exc)
        run.save(update_fields=["status", "error"])
        raise

    return run, True


def _build_score_rows(run, window_start, window_end) -> list[NetworkScore]:
    aggregates = _aggregate_window(window_start, window_end)

    # Only score pairs with impressions. Group by placement for normalization.
    by_placement: dict[int, list[Aggregate]] = {}
    for agg in aggregates.values():
        if agg.impressions > 0:
            by_placement.setdefault(agg.placement_id, []).append(agg)

    w_fr = Decimal(str(settings.SCORE_WEIGHT_FILL_RATE))
    w_ecpm = Decimal(str(settings.SCORE_WEIGHT_ECPM))
    w_rel = Decimal(str(settings.SCORE_WEIGHT_RELIABILITY))

    rows: list[NetworkScore] = []
    for placement_id, aggs in by_placement.items():
        norm_fr = _minmax([a.fill_rate for a in aggs])
        norm_ecpm = _minmax([a.ecpm for a in aggs])
        norm_rel = _minmax([a.reliability for a in aggs])

        scored = []
        for agg in aggs:
            nfr, nec, nrel = (
                norm_fr(agg.fill_rate),
                norm_ecpm(agg.ecpm),
                norm_rel(agg.reliability),
            )
            score = (
                w_fr * (nfr if nfr is not None else NEUTRAL)
                + w_ecpm * (nec if nec is not None else NEUTRAL)
                + w_rel * (nrel if nrel is not None else NEUTRAL)
            )
            scored.append((agg, nfr, nec, nrel, quantize_score(score)))

        # network_id ordering isn't lexicographic; resolve codes once and use
        # the code as the final stable arbiter.
        network_codes = dict(
            (nid, code)
            for nid, code in _network_codes_for([a.network_id for a in aggs])
        )
        scored.sort(
            key=lambda item: (
                -item[4],
                -(item[0].fill_rate or ZERO),
                -(item[0].ecpm or ZERO),
                -item[0].impressions,
                network_codes.get(item[0].network_id, ""),
            )
        )

        for rank, (agg, nfr, nec, nrel, score) in enumerate(scored, start=1):
            rows.append(
                NetworkScore(
                    run=run,
                    placement_id=placement_id,
                    network_id=agg.network_id,
                    impressions=agg.impressions,
                    fills=agg.fills,
                    failures=agg.failures,
                    revenue=agg.revenue,
                    fill_rate=agg.fill_rate,
                    ecpm=agg.ecpm,
                    reliability=agg.reliability,
                    fill_rate_norm=nfr,
                    ecpm_norm=nec,
                    reliability_norm=nrel,
                    score=score,
                    rank=rank,
                )
            )
    return rows


def _network_codes_for(network_ids):
    from apps.catalog.models import AdNetwork

    return AdNetwork.objects.filter(id__in=set(network_ids)).values_list(
        "id", "code"
    )


def _activate_run(run):
    """Atomically mark this run's scores active and deactivate older runs."""
    run.is_active = True
    run.save(update_fields=["is_active"])
    ScoreRun.objects.exclude(pk=run.pk).update(is_active=False)


def run_latest(*, force=False, now=None):
    """Convenience used by the scheduler: score the latest closed period."""
    now = now or timezone.now()
    period = latest_closed_period(now)
    return run_scoring(period, force=force, now=now)


def backfill(*, from_time, until_time=None, force=True):
    """(Re)compute every closed period in a range — used for late-event
    catch-up. Returns the list of runs processed in chronological order."""
    minutes = settings.SCORING_PERIOD_MINUTES
    until_time = until_time or timezone.now()
    start = floor_to_period(from_time, minutes)
    end_boundary = floor_to_period(until_time, minutes)
    if end_boundary == until_time:
        end_boundary = end_boundary - timedelta(minutes=minutes)

    runs = []
    cursor = start
    while cursor <= end_boundary:
        run, _ = run_scoring(cursor, force=force)
        runs.append(run)
        cursor += timedelta(minutes=minutes)
    return runs
