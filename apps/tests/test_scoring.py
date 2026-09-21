from datetime import timedelta
from decimal import Decimal

from django.utils import timezone

from apps.ingestion.models import EventType
from apps.scoring.models import NetworkScore, ScoreRun, ScoreRunStatus
from apps.scoring.services import (
    backfill,
    floor_to_period,
    latest_closed_period,
    run_latest,
    run_scoring,
)
from apps.tests.factories import (
    add_line,
    create_event,
    make_app,
    make_full_pipeline,
    make_network,
    make_placement,
    make_user,
)
from django.test import TestCase


def emit_lifecycle(app, placement, network, t, *, fill=True, fail=False, revenue=None):
    create_event(
        f"imp-{network.code}-{t.isoformat()}",
        app, placement, network, EventType.IMPRESSION, t
    )
    if fill:
        create_event(
            f"fill-{network.code}-{t.isoformat()}",
            app, placement, network, EventType.FILL, t + timedelta(seconds=1),
        )
    if fail:
        create_event(
            f"fail-{network.code}-{t.isoformat()}",
            app, placement, network, EventType.FAILURE, t + timedelta(seconds=1),
            error_code="no_fill",
        )
    if revenue is not None:
        create_event(
            f"rev-{network.code}-{t.isoformat()}",
            app, placement, network, EventType.REVENUE, t + timedelta(seconds=2),
            revenue=Decimal(revenue),
        )


class PeriodMathTests(TestCase):
    def test_floor_to_30_minutes(self):
        dt = timezone.now().replace(hour=12, minute=17, second=30, microsecond=1)
        floored = floor_to_period(dt)
        self.assertEqual((floored.hour, floored.minute, floored.second), (12, 0, 0))

    def test_latest_closed_period_before_and_after_boundary(self):
        at_12_29 = timezone.now().replace(hour=12, minute=29, second=59)
        # 12:29:59 is inside the 12:00 period, whose start is the boundary.
        self.assertEqual(
            (latest_closed_period(at_12_29).hour,
             latest_closed_period(at_12_29).minute),
            (12, 0),
        )
        # Exactly at the boundary, the period that just opened is excluded.
        at_12_30 = timezone.now().replace(hour=12, minute=30, second=0, microsecond=0)
        self.assertEqual(
            (latest_closed_period(at_12_30).hour,
             latest_closed_period(at_12_30).minute),
            (12, 0),
        )
        at_13_00 = timezone.now().replace(hour=13, minute=0, second=0, microsecond=0)
        self.assertEqual(
            (latest_closed_period(at_13_00).hour,
             latest_closed_period(at_13_00).minute),
            (12, 30),
        )


class ScoringAlgorithmTests(TestCase):
    def setUp(self):
        self.user = make_user("scoreowner")
        self.app = make_app(self.user, code="sapp")
        self.placement = make_placement(self.app, code="sp")
        self.good = make_network("goodnet", "Good Net")
        self.mid = make_network("midnet", "Mid Net")
        self.bad = make_network("badnet", "Bad Net")
        for i, net in enumerate([self.good, self.mid, self.bad], start=1):
            add_line(self.placement, net, priority=i, floor="1.00")

        now = timezone.now()
        # good: always fills, reliable, high revenue
        for h in range(1, 6):
            emit_lifecycle(
                self.app, self.placement, self.good,
                now - timedelta(hours=h), fill=True, revenue="0.005",
            )
        # mid: fills ~half, some failures
        for h in range(1, 6):
            emit_lifecycle(
                self.app, self.placement, self.mid,
                now - timedelta(hours=h, minutes=10),
                fill=h % 2 == 0, fail=h % 2 == 1,
                revenue="0.002" if h % 2 == 0 else None,
            )
        # bad: impressions but only failures, no fills/revenue
        for h in range(1, 6):
            emit_lifecycle(
                self.app, self.placement, self.bad,
                now - timedelta(hours=h, minutes=20),
                fill=False, fail=True,
            )

    def test_components_and_ranking(self):
        run, created = run_latest()
        self.assertTrue(created)
        scores = {
            s.network.code: s for s in NetworkScore.objects.filter(run=run)
        }
        self.assertEqual(set(scores), {"goodnet", "midnet", "badnet"})

        # raw fill rates
        self.assertEqual(scores["goodnet"].fill_rate, Decimal("1.000000"))
        self.assertEqual(scores["badnet"].fill_rate, Decimal("0.000000"))
        # goodnet had no failure -> reliability 1
        self.assertEqual(scores["goodnet"].reliability, Decimal("1.000000"))
        # badnet had only failures -> reliability 0
        self.assertEqual(scores["badnet"].reliability, Decimal("0.000000"))

        # min-max: best gets 1, worst gets 0 for fill_rate
        self.assertEqual(scores["goodnet"].fill_rate_norm, Decimal("1.000000"))
        self.assertEqual(scores["badnet"].fill_rate_norm, Decimal("0.000000"))

        ranks = {code: scores[code].rank for code in scores}
        self.assertEqual(
            sorted(ranks, key=lambda c: ranks[c]),
            ["goodnet", "midnet", "badnet"],
        )

    def test_score_weights_sum_to_one(self):
        from django.conf import settings
        total = (
            settings.SCORE_WEIGHT_FILL_RATE
            + settings.SCORE_WEIGHT_ECPM
            + settings.SCORE_WEIGHT_RELIABILITY
        )
        self.assertAlmostEqual(total, 1.0)

    def test_repeat_run_is_idempotent_and_deterministic(self):
        run1, _ = run_latest()
        rows1 = list(
            NetworkScore.objects.filter(run=run1).values_list(
                "network_id", "score", "rank"
            )
        )
        run2, created = run_scoring(run1.period_start)
        self.assertFalse(created)
        self.assertEqual(run1.id, run2.id)
        rows2 = list(
            NetworkScore.objects.filter(run=run2).values_list(
                "network_id", "score", "rank"
            )
        )
        self.assertEqual(rows1, rows2)
        self.assertEqual(run1.checksum, run2.checksum)


class NormalizationEdgeCaseTests(TestCase):
    def test_all_equal_component_gets_neutral_half(self):
        user = make_user("eq")
        app = make_app(user, code="eqapp")
        placement = make_placement(app, code="eqp")
        n1 = make_network("eq1", "Eq 1")
        n2 = make_network("eq2", "Eq 2")
        add_line(placement, n1, priority=1, floor="1")
        add_line(placement, n2, priority=2, floor="1")
        now = timezone.now()
        # Both networks: every impression fills, same revenue, no failures.
        for h, net in [(1, n1), (2, n1), (1, n2), (2, n2)]:
            emit_lifecycle(
                app, placement, net, now - timedelta(hours=h),
                fill=True, revenue="0.001",
            )
        run, _ = run_latest()
        scores = NetworkScore.objects.filter(run=run)
        for s in scores:
            self.assertEqual(s.fill_rate_norm, Decimal("0.500000"))
            self.assertEqual(s.ecpm_norm, Decimal("0.500000"))
            self.assertEqual(s.reliability_norm, Decimal("0.500000"))
            # identical everything -> equal scores
            self.assertEqual(s.score, Decimal("0.500000"))

    def test_missing_samples_excluded_and_neutral_applied(self):
        user = make_user("miss")
        app = make_app(user, code="missapp")
        placement = make_placement(app, code="missp")
        a = make_network("n-a", "A")
        b = make_network("n-b", "B")
        add_line(placement, a, priority=1, floor="1")
        add_line(placement, b, priority=2, floor="1")
        now = timezone.now()
        # A: full cycle with revenue. B: impressions only (no fill/failure/
        # revenue signals) -> reliability missing, ecpm missing, fill 0.
        emit_lifecycle(app, placement, a, now - timedelta(hours=1),
                       fill=True, revenue="0.002")
        create_event("bimp", app, placement, b, EventType.IMPRESSION,
                     now - timedelta(hours=1))
        run, _ = run_latest()
        scores = {s.network.code: s for s in NetworkScore.objects.filter(run=run)}
        self.assertIsNone(scores["n-b"].reliability)
        self.assertIsNone(scores["n-b"].reliability_norm)
        # Zero impressions-with-revenue means eCPM is legitimately 0 (not
        # missing): it is scored and participates in min-max.
        self.assertEqual(scores["n-b"].ecpm, Decimal("0.000000"))
        self.assertEqual(scores["n-b"].ecpm_norm, Decimal("0.000000"))
        # fill_rate 0 also scored -> normalized 0.
        # score = 0.4*0 + 0.35*0 + 0.25*neutral(0.5) = 0.125
        self.assertEqual(scores["n-b"].score, Decimal("0.125000"))
        self.assertEqual(scores["n-a"].rank, 1)

    def test_networks_without_impressions_are_not_scored(self):
        user = make_user("zeroimp")
        app = make_app(user, code="ziapp")
        placement = make_placement(app, code="zip")
        quiet = make_network("quiet", "Quiet")
        loud = make_network("loud", "Loud")
        add_line(placement, quiet, priority=1, floor="1")
        add_line(placement, loud, priority=2, floor="1")
        now = timezone.now()
        emit_lifecycle(app, placement, loud, now - timedelta(minutes=5),
                       fill=True, revenue="0.001")
        run, _ = run_latest()
        scored = set(
            NetworkScore.objects.filter(run=run).values_list(
                "network__code", flat=True
            )
        )
        self.assertEqual(scored, {"loud"})


class TieBreakTests(TestCase):
    def test_identical_metrics_tie_break_by_network_code(self):
        user = make_user("tie")
        app = make_app(user, code="tieapp")
        placement = make_placement(app, code="tiep")
        zebra = make_network("zebra", "Zebra")
        alpha = make_network("alpha", "Alpha")
        add_line(placement, zebra, priority=1, floor="1")
        add_line(placement, alpha, priority=2, floor="1")
        now = timezone.now()
        # Identical, perfectly-performing lifecycle for both networks.
        emit_lifecycle(app, placement, zebra, now - timedelta(hours=1),
                       fill=True, revenue="0.001")
        emit_lifecycle(app, placement, alpha, now - timedelta(hours=2),
                       fill=True, revenue="0.001")
        run, _ = run_latest()
        scores = {
            s.network.code: s.rank for s in NetworkScore.objects.filter(run=run)
        }
        self.assertEqual(scores["alpha"], 1)
        self.assertEqual(scores["zebra"], 2)


class LateEventBackfillTests(TestCase):
    def setUp(self):
        self.user = make_user("late")
        self.app = make_app(self.user, code="lateapp")
        self.placement = make_placement(self.app, code="latep")
        self.net = make_network("latenet", "Late Net")
        add_line(self.placement, self.net, priority=1, floor="1")

    def test_late_event_then_backfill_recomputes_old_period_identically(self):
        period = latest_closed_period(timezone.now() - timedelta(minutes=40))
        # Run period with no events.
        run_before, _ = run_scoring(period)
        self.assertEqual(NetworkScore.objects.filter(run=run_before).count(), 0)

        # A delayed event for that window arrives.
        event_time = period + timedelta(minutes=10)
        create_event(
            "late-imp", self.app, self.placement, self.net,
            EventType.IMPRESSION, event_time,
        )
        run_after, created = run_scoring(period)  # idempotent: no change
        self.assertFalse(created)
        self.assertEqual(NetworkScore.objects.filter(run=run_after).count(), 0)

        # Explicit backfill picks up the late event.
        run_fixed, created = run_scoring(period, force=True)
        self.assertTrue(created)
        self.assertEqual(NetworkScore.objects.filter(run=run_fixed).count(), 1)

        # Re-running force again (same table state) yields identical checksum.
        run_again, _ = run_scoring(period, force=True)
        self.assertEqual(run_again.checksum, run_fixed.checksum)

    def test_backfill_walks_multiple_periods(self):
        start = latest_closed_period() - timedelta(minutes=90)
        runs = backfill(from_time=start, force=False)
        self.assertEqual(len(runs), 4)
        self.assertEqual(
            ScoreRun.objects.filter(
                period_start__gte=start, status=ScoreRunStatus.COMPLETE
            ).count(),
            4,
        )


class AtomicActivationTests(TestCase):
    def test_only_one_active_run_after_multiple_periods(self):
        make_full_pipeline(num_networks=1)
        r1, _ = run_latest()
        self.assertTrue(ScoreRun.objects.get(pk=r1.pk).is_active)

        older = latest_closed_period() - timedelta(minutes=30)
        r2, _ = run_scoring(older)
        # newest computed run becomes active; exactly one globally
        r1.refresh_from_db()
        r2.refresh_from_db()
        active_count = ScoreRun.objects.filter(is_active=True).count()
        self.assertEqual(active_count, 1)
        self.assertTrue(r2.is_active)
        self.assertFalse(r1.is_active)
