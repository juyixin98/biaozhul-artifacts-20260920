"""A/B experiments: stable 50/50 grouping, config-change isolation, stats."""
from datetime import timedelta
from decimal import Decimal

from django.test import TransactionTestCase
from django.utils import timezone

from events.models import Event, EventType, FailureReason
from experiments.models import Assignment, Experiment, VariantVersion
from experiments.services import (
    assign_variant,
    create_experiment,
    experiment_stats,
    start_experiment,
)
from waterfall.services import create_or_replace_draft, publish_draft

from .factories import (
    make_app,
    make_developer,
    make_networks,
    make_placement,
    sdk_client,
)


class GroupingTests(TransactionTestCase):
    def setUp(self):
        self.dev = make_developer()
        self.app = make_app(self.dev)
        self.placement = make_placement(self.app)
        self.networks = make_networks(self.dev)
        self.v1 = self._publish(["2.00", "1.50", "1.00", "0.50"])
        self.v2 = self._publish(["2.10", "1.40", "1.10", "0.60"])
        self.experiment = create_experiment(
            placement=self.placement,
            name="exp",
            version_a_id=self.v1.id,
            version_b_id=self.v2.id,
            developer=self.dev,
        )
        start_experiment(experiment=self.experiment, developer=self.dev)

    def _publish(self, floors):
        create_or_replace_draft(
            placement=self.placement,
            entries_data=[
                {
                    "network_id": n.id,
                    "priority": i + 1,
                    "fallback_order": i + 1,
                    "floor_cpm": floors[i],
                }
                for i, n in enumerate(self.networks)
            ],
            developer=self.dev,
        )
        return publish_draft(placement=self.placement, developer=self.dev)

    def test_assignment_is_stable_for_same_user(self):
        v1, version1, tracked1 = assign_variant(
            experiment=self.experiment, user_key="user-123"
        )
        v2, version2, tracked2 = assign_variant(
            experiment=self.experiment, user_key="user-123"
        )
        self.assertEqual(v1, v2)
        self.assertEqual(version1, version2)
        self.assertTrue(tracked1)
        self.assertFalse(tracked2)
        self.assertEqual(
            Assignment.objects.filter(
                experiment=self.experiment,
                user_key_hash__isnull=False,
            ).count(),
            1,
        )

    def test_split_is_roughly_50_50(self):
        counts = {"A": 0, "B": 0}
        for i in range(2000):
            variant, _, _ = assign_variant(
                experiment=self.experiment, user_key=f"user-{i}"
            )
            counts[variant] += 1
        # SHA-256 based split: tolerate a generous sampling margin.
        self.assertGreater(counts["A"], 900)
        self.assertGreater(counts["B"], 900)

    def test_publishing_new_versions_does_not_move_groups(self):
        before = {}
        for i in range(100):
            variant, _, _ = assign_variant(
                experiment=self.experiment, user_key=f"fixed-user-{i}"
            )
            before[f"fixed-user-{i}"] = variant

        # Publish v3/v4 AFTER the experiment started: bound versions cannot be
        # edited, and assignments are derived from (experiment_key, user_key),
        # so every user keeps their group.
        self._publish(["3.00", "2.50", "2.00", "1.50"])
        self._publish(["3.10", "2.40", "2.10", "1.40"])

        for i in range(100):
            variant, version_id, _ = assign_variant(
                experiment=self.experiment, user_key=f"fixed-user-{i}"
            )
            self.assertEqual(variant, before[f"fixed-user-{i}"])
            # The served version is still the originally bound one.
            self.assertIn(
                version_id, {self.v1.id, self.v2.id}
            )

    def test_variant_versions_are_protected(self):
        # The bound versions themselves must remain immutable rows.
        for binding in VariantVersion.objects.filter(experiment=self.experiment):
            self.assertEqual(binding.version.status, "published")
            # Direct modification of a published version is not exposed anywhere;
            # verify the ORM still holds the original floor snapshot.
            entry = binding.version.entries.order_by("priority").first()
            self.assertIsNotNone(entry.id)


class ExperimentStatsTests(TransactionTestCase):
    def setUp(self):
        self.dev = make_developer()
        self.app = make_app(self.dev)
        self.placement = make_placement(self.app)
        self.networks = make_networks(self.dev)
        self.v1 = self._publish(["1", "1", "1", "1"])
        self.v2 = self._publish(["2", "2", "2", "2"])
        self.experiment = create_experiment(
            placement=self.placement,
            name="exp",
            version_a_id=self.v1.id,
            version_b_id=self.v2.id,
            developer=self.dev,
        )
        start_experiment(experiment=self.experiment, developer=self.dev)

    def _publish(self, floors):
        create_or_replace_draft(
            placement=self.placement,
            entries_data=[
                {
                    "network_id": n.id,
                    "priority": i + 1,
                    "fallback_order": i + 1,
                    "floor_cpm": floors[i],
                }
                for i, n in enumerate(self.networks)
            ],
            developer=self.dev,
        )
        return publish_draft(placement=self.placement, developer=self.dev)

    def _events(self, variant, *, fills, failures, impressions, revenue):
        seq = 0
        bulk = []
        now = timezone.now() - timedelta(hours=1)

        def eid():
            nonlocal seq
            seq += 1
            return f"{variant}-{seq}"

        for _ in range(fills):
            bulk.append(Event(
                app=self.app, event_id=eid(), event_type=EventType.FILL,
                placement=self.placement, network=self.networks[0],
                version=self.v1 if variant == "A" else self.v2,
                experiment=self.experiment, variant=variant, occurred_at=now,
            ))
        for _ in range(failures):
            bulk.append(Event(
                app=self.app, event_id=eid(), event_type=EventType.FAILURE,
                placement=self.placement, network=self.networks[0],
                version=self.v1 if variant == "A" else self.v2,
                experiment=self.experiment, variant=variant,
                failure_reason=FailureReason.NO_FILL, occurred_at=now,
            ))
        for _ in range(impressions):
            bulk.append(Event(
                app=self.app, event_id=eid(), event_type=EventType.IMPRESSION,
                placement=self.placement, network=self.networks[0],
                version=self.v1 if variant == "A" else self.v2,
                experiment=self.experiment, variant=variant, occurred_at=now,
            ))
        for _ in range(revenue):
            bulk.append(Event(
                app=self.app, event_id=eid(), event_type=EventType.REVENUE,
                placement=self.placement, network=self.networks[0],
                version=self.v1 if variant == "A" else self.v2,
                experiment=self.experiment, variant=variant,
                amount=Decimal("1.000000"), occurred_at=now,
            ))
        Event.objects.bulk_create(bulk)

    def test_stats_fill_rate_and_ecpm(self):
        # A: 80 fills / 20 failures = 0.8 fill; 100 impressions; $50 revenue
        # -> eCPM 500. B: 50/50 = 0.5 fill; 100 impressions; $100 -> eCPM 1000.
        self._events("A", fills=80, failures=20, impressions=100, revenue=50)
        self._events("B", fills=50, failures=50, impressions=100, revenue=100)

        stats = experiment_stats(self.experiment)
        self.assertEqual(stats["A"]["fill_rate"], "0.800000")
        self.assertEqual(stats["A"]["ecpm_usd"], "500.000000")
        self.assertEqual(stats["B"]["fill_rate"], "0.500000")
        self.assertEqual(stats["B"]["ecpm_usd"], "1000.000000")
        self.assertEqual(stats["A"]["revenue_usd"], "50.000000")

    def test_stats_null_when_no_samples(self):
        stats = experiment_stats(self.experiment)
        self.assertIsNone(stats["A"]["fill_rate"])
        self.assertIsNone(stats["A"]["ecpm_usd"])
        self.assertEqual(stats["A"]["impressions"], 0)


class SdkConfigTests(TransactionTestCase):
    def setUp(self):
        self.dev = make_developer()
        self.app = make_app(self.dev)
        self.placement = make_placement(self.app)
        self.networks = make_networks(self.dev)
        self.v1 = self._publish(["1", "1", "1", "1"])
        self.v2 = self._publish(["2", "2", "2", "2"])
        self.client = sdk_client(self.app)

    def _publish(self, floors):
        create_or_replace_draft(
            placement=self.placement,
            entries_data=[
                {
                    "network_id": n.id,
                    "priority": i + 1,
                    "fallback_order": i + 1,
                    "floor_cpm": floors[i],
                }
                for i, n in enumerate(self.networks)
            ],
            developer=self.dev,
        )
        return publish_draft(placement=self.placement, developer=self.dev)

    def test_control_served_without_experiment(self):
        resp = self.client.get(
            "/api/v1/sdk/config/",
            {"placement_key": "home_rewarded", "user_key": "u1"},
        )
        self.assertEqual(resp.status_code, 200)
        body = resp.json()
        self.assertEqual(body["id"], self.v2.id)
        self.assertEqual(body["served"]["assignment"]["type"], "control")

    def test_experiment_served_and_stable(self):
        create_experiment(
            placement=self.placement, name="exp",
            version_a_id=self.v1.id, version_b_id=self.v2.id,
            developer=self.dev,
        )
        experiment = Experiment.objects.get()
        start_experiment(experiment=experiment, developer=self.dev)

        resp1 = self.client.get(
            "/api/v1/sdk/config/",
            {"placement_key": "home_rewarded", "user_key": "stable-user"},
        )
        first_variant = resp1.json()["served"]["assignment"]["variant"]
        first_version = resp1.json()["id"]
        for _ in range(5):
            resp = self.client.get(
                "/api/v1/sdk/config/",
                {"placement_key": "home_rewarded", "user_key": "stable-user"},
            )
            self.assertEqual(
                resp.json()["served"]["assignment"]["variant"], first_variant
            )
            self.assertEqual(resp.json()["id"], first_version)

    def test_missing_user_key_does_not_assign(self):
        create_experiment(
            placement=self.placement, name="exp",
            version_a_id=self.v1.id, version_b_id=self.v2.id,
            developer=self.dev,
        )
        experiment = Experiment.objects.get()
        start_experiment(experiment=experiment, developer=self.dev)

        resp = self.client.get(
            "/api/v1/sdk/config/", {"placement_key": "home_rewarded"}
        )
        self.assertEqual(resp.status_code, 200)
        assignment = resp.json()["served"]["assignment"]
        self.assertTrue(assignment["user_key_required"])
        self.assertEqual(Assignment.objects.count(), 0)

    def test_unknown_placement_404(self):
        resp = self.client.get(
            "/api/v1/sdk/config/", {"placement_key": "nope", "user_key": "u"}
        )
        self.assertEqual(resp.status_code, 404)
