"""Scoring: weighting, normalization, missing samples, ties, determinism,
late events and atomic switching.
"""
from datetime import timedelta
from decimal import Decimal

from django.test import TransactionTestCase
from django.utils import timezone

from events.models import Event, EventType, FailureReason
from scoring.models import ScoreRun
from scoring.services import run_scoring
from scoring.slots import floor_to_slot, window_for_slot

from .factories import (
    make_app,
    make_developer,
    make_networks,
    make_placement,
    publish_waterfall,
)


def add_events(app, placement, networks, *, slot, profile, count=100):
    """Create synthetic fills/failures/impressions/revenue in the 7d window.

    ``profile`` maps network id -> (fill_rate, error_rate, ecpm_usd). Every
    fill is paired with an impression and a revenue event of ecpm/1000.
    """
    created = []
    ts_base = slot - timedelta(days=1)

    def make(eid, etype, *, amount=None, reason=None, minute_offset=0):
        return Event(
            app=app,
            event_id=eid,
            event_type=etype,
            placement=placement,
            network=None,
            occurred_at=ts_base + timedelta(minutes=minute_offset),
            amount=amount,
            failure_reason=reason,
        )

    for network, offset in zip(networks, range(0, 40, 10)):
        fr, er, cpms = profile[network.id]
        nf = round(count * fr)
        ne = round(count * er)
        nn = count - nf - ne
        events = []
        for k in range(nf):
            events.append(make(f"{network.id}-f{k}", EventType.FILL,
                               minute_offset=offset))
        for k in range(ne):
            events.append(make(f"{network.id}-e{k}", EventType.FAILURE,
                               reason=FailureReason.ERROR, minute_offset=offset))
        for k in range(nn):
            events.append(make(f"{network.id}-n{k}", EventType.FAILURE,
                               reason=FailureReason.NO_FILL, minute_offset=offset))
        for k in range(nf):
            events.append(make(f"{network.id}-i{k}", EventType.IMPRESSION,
                               minute_offset=offset))
            amount = (Decimal(cpms) / Decimal(1000)).quantize(Decimal("0.000001"))
            events.append(make(f"{network.id}-r{k}", EventType.REVENUE,
                               amount=amount, minute_offset=offset))
        for ev in events:
            ev.network = network
        Event.objects.bulk_create(events)
        created.extend(events)
    return created


class ScoringTests(TransactionTestCase):
    def setUp(self):
        self.dev = make_developer()
        self.app = make_app(self.dev)
        self.placement = make_placement(self.app)
        self.networks = make_networks(self.dev)
        self.v1 = publish_waterfall(self.placement, self.networks, self.dev)
        self.slot = floor_to_slot(timezone.now())

    def _profiles(self):
        return {
            # network id -> (fill_rate, error_rate, ecpm usd)
            self.networks[0].id: (0.95, 0.02, "2.00"),
            self.networks[1].id: (0.80, 0.05, "1.50"),
            self.networks[2].id: (0.60, 0.15, "1.00"),
            self.networks[3].id: (0.50, 0.25, "0.50"),
        }

    def test_scores_rank_and_weight(self):
        add_events(
            self.app, self.placement, self.networks,
            slot=self.slot, profile=self._profiles(), count=200,
        )
        run = run_scoring(slot_start=self.slot)
        items = list(run.items.order_by("rank"))
        self.assertEqual([i.network_id for i in items], [n.id for n in self.networks])

        best = items[0]
        worst = items[-1]
        # Best network dominates every metric -> all norms 1 -> total 100.
        self.assertEqual(best.total_score, Decimal("100.000000"))
        self.assertEqual(best.norm_fill_rate, Decimal("1.000000000"))
        self.assertEqual(best.norm_ecpm, Decimal("1.000000000"))
        self.assertEqual(best.norm_reliability, Decimal("1.000000000"))
        # Worst network -> all norms 0 -> total 0.
        self.assertEqual(worst.total_score, Decimal("0.000000"))

        # Spot-check a raw metric: network 0 fill rate 0.95 (190/200).
        self.assertEqual(best.fill_rate, Decimal("0.950000000"))
        # eCPM = 2.00 for network 0.
        self.assertEqual(best.ecpm, Decimal("2.000000"))
        # reliability = 1 - 0.02 = 0.98 (errors = round(200*0.02)=4).
        self.assertEqual(best.reliability, Decimal("0.980000000"))

    def test_repeat_run_is_identical_and_idempotent(self):
        add_events(
            self.app, self.placement, self.networks,
            slot=self.slot, profile=self._profiles(), count=200,
        )
        first = run_scoring(slot_start=self.slot)
        hash1 = first.items_hash
        items1 = list(first.items.order_by("placement_id", "rank", "network_id"))

        # Re-run the same slot: no duplicate run, same hash, same numbers.
        second = run_scoring(slot_start=self.slot)
        self.assertEqual(second.id, first.id)
        self.assertEqual(second.items_hash, hash1)
        self.assertEqual(ScoreRun.objects.filter(slot_start=self.slot).count(), 1)

        items2 = list(second.items.order_by("placement_id", "rank", "network_id"))
        self.assertEqual(len(items1), len(items2))
        for a, b in zip(items1, items2):
            self.assertEqual(a.network_id, b.network_id)
            self.assertEqual(a.total_score, b.total_score)
            self.assertEqual(a.rank, b.rank)
            self.assertEqual(a.fill_rate, b.fill_rate)
            self.assertEqual(a.ecpm, b.ecpm)
            self.assertEqual(a.reliability, b.reliability)

    def test_late_events_change_only_future_ticks(self):
        add_events(
            self.app, self.placement, self.networks,
            slot=self.slot, profile=self._profiles(), count=200,
        )
        run1 = run_scoring(slot_start=self.slot)
        before = list(
            run1.items.order_by("network_id").values_list("total_score", flat=True)
        )

        # A late event landing after the tick finalizes (timestamped inside
        # the same window) MUST NOT mutate the finalized tick...
        Event.objects.create(
            app=self.app,
            event_id="late-after-finalize",
            event_type=EventType.FILL,
            placement=self.placement,
            network=self.networks[3],
            occurred_at=self.slot - timedelta(hours=1),
        )
        run1_repeated = run_scoring(slot_start=self.slot)
        after = list(
            run1_repeated.items.order_by("network_id").values_list(
                "total_score", flat=True
            )
        )
        self.assertEqual(before, after)

        # ...but the NEXT 30-minute slot picks it up via the rolling window.
        next_slot = self.slot + timedelta(minutes=30)
        run2 = run_scoring(slot_start=next_slot)
        self.assertNotEqual(run2.id, run1.id)
        self.assertEqual(run2.slot_start, next_slot)

    def test_missing_samples_are_zeros_not_normalization_anchors(self):
        # Only two networks have any events; the other two are in the current
        # waterfall and thus candidates with zero samples.
        used = self.networks[:2]
        profiles = {
            used[0].id: (0.90, 0.10, "1.00"),
            used[1].id: (0.50, 0.20, "2.00"),
        }
        add_events(
            self.app, self.placement, used,
            slot=self.slot, profile=profiles, count=100,
        )
        run = run_scoring(slot_start=self.slot)
        by_network = {i.network_id: i for i in run.items.all()}

        # Empty networks exist as candidates but score zero and don't anchor
        # normalization of the populated ones.
        for net in self.networks[2:]:
            self.assertIn(net.id, by_network)
            self.assertEqual(by_network[net.id].total_score, Decimal("0.000000"))
            self.assertEqual(by_network[net.id].opportunities, 0)

        # Network0: fill norm 1, ecpm norm 0, reliability norm 1
        # -> (0.40 + 0.25) * 100 = 65.
        n0 = by_network[used[0].id]
        self.assertEqual(n0.norm_fill_rate, Decimal("1.000000000"))
        self.assertEqual(n0.norm_ecpm, Decimal("0.000000000"))
        self.assertEqual(n0.norm_reliability, Decimal("1.000000000"))
        self.assertEqual(n0.total_score, Decimal("65.000000"))
        # Network1: fill norm 0, ecpm norm 1, reliability norm 0
        # -> 0.35 * 100 = 35.
        n1 = by_network[used[1].id]
        self.assertEqual(n1.norm_ecpm, Decimal("1.000000000"))
        self.assertEqual(n1.total_score, Decimal("35.000000"))

    def test_single_network_constant_population_scores_full(self):
        used = [self.networks[0]]
        add_events(
            self.app, self.placement, used,
            slot=self.slot, profile={used[0].id: (0.7, 0.0, "1.00")}, count=100,
        )
        run = run_scoring(slot_start=self.slot)
        only = run.items.filter(network=used[0]).get()
        # max == min for every populated metric -> normalized to 1.
        self.assertEqual(only.total_score, Decimal("100.000000"))

    def test_identical_metrics_tie_break_is_network_id(self):
        # Give the first two networks identical raw profiles.
        used = self.networks[:2]
        profiles = {
            used[0].id: (0.8, 0.0, "1.00"),
            used[1].id: (0.8, 0.0, "1.00"),
        }
        add_events(
            self.app, self.placement, used,
            slot=self.slot, profile=profiles, count=100,
        )
        run = run_scoring(slot_start=self.slot)
        ordered = list(run.items.filter(network_id__in=[n.id for n in used]).order_by("rank"))
        self.assertEqual([i.network_id for i in ordered], sorted(n.id for n in used))
        self.assertEqual(ordered[0].total_score, ordered[1].total_score)

    def test_window_is_seven_days_half_open(self):
        slot, start, end = window_for_slot(self.slot)
        self.assertEqual(end - start, timedelta(days=7))
        self.assertEqual(end, slot)

        # Event exactly at slot boundary is outside (half-open).
        Event.objects.create(
            app=self.app, event_id="at-boundary", event_type=EventType.FILL,
            placement=self.placement, network=self.networks[0], occurred_at=slot,
        )
        run = run_scoring(slot_start=slot)
        item = run.items.get(network=self.networks[0])
        self.assertEqual(item.fills, 0)

    def test_unfinalized_run_never_visible(self):
        # Directly-created pending run is not served by the finalized filter.
        pending = ScoreRun.objects.create(
            slot_start=self.slot - timedelta(minutes=30),
            window_start=self.slot - timedelta(days=7, minutes=30),
            window_end=self.slot - timedelta(minutes=30),
            finalized=False,
        )
        self.assertFalse(ScoreRun.objects.filter(
            id=pending.id, finalized=True
        ).exists())
