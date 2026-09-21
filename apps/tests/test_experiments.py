import json
from datetime import timedelta
from decimal import Decimal

from django.utils import timezone

from apps.catalog.models import PlacementNetwork
from apps.catalog.services import publish_config
from apps.experiments.models import Experiment, ExperimentAssignment
from apps.experiments.services import (
    ExperimentError,
    assign_variant,
    create_experiment,
    experiment_stats,
)
from apps.ingestion.models import AdEvent, EventType
from apps.tests.factories import (
    add_line,
    auth_client,
    create_event,
    make_app,
    make_full_pipeline,
    make_network,
    make_placement,
    make_user,
    sdk_client,
)
from django.test import TestCase


class AssignmentStabilityTests(TestCase):
    def setUp(self):
        self.pipe = make_full_pipeline(num_networks=2)
        self.placement = self.pipe["placement"]
        self.user = self.pipe["user"]
        v1 = self.pipe["version"]
        # make a v2
        line = PlacementNetwork.objects.get(placement=self.placement, priority=1)
        line.cpm_floor = Decimal("8.000000")
        line.save(update_fields=["cpm_floor"])
        self.v2 = publish_config(placement=self.placement, published_by=self.user)
        self.experiment = create_experiment(
            placement=self.placement,
            created_by=self.user,
            name="exp",
            variant_a_version_id=v1.id,
            variant_b_version_id=self.v2.id,
        )

    def test_same_user_always_same_variant(self):
        key = "a" * 64
        first = assign_variant(experiment=self.experiment, user_key_hash=key)
        for _ in range(10):
            self.assertEqual(
                assign_variant(experiment=self.experiment, user_key_hash=key),
                first,
            )
        self.assertEqual(
            ExperimentAssignment.objects.filter(
                experiment=self.experiment, user_key_hash=key
            ).count(),
            1,
        )

    def test_uppercase_input_normalized(self):
        lower = assign_variant(
            experiment=self.experiment, user_key_hash="abcdef"
        )
        upper = assign_variant(
            experiment=self.experiment, user_key_hash="ABCDEF"
        )
        self.assertEqual(lower, upper)

    def test_distribution_is_approximately_50_50(self):
        variants = {"A": 0, "B": 0}
        for i in range(2000):
            import hashlib

            key = hashlib.sha256(f"user-{i}".encode()).hexdigest()
            variants[assign_variant(experiment=self.experiment, user_key_hash=key)] += 1
        # Deterministic hash: allow generous tolerance for 2000 samples.
        self.assertGreater(variants["A"], 900)
        self.assertGreater(variants["B"], 900)
        ratio = variants["A"] / 2000
        self.assertAlmostEqual(ratio, 0.5, delta=0.05)

    def test_salts_isolate_assignments_across_experiments(self):
        # Second experiment on a different placement; same user keys spread
        # independently.
        app2 = make_app(self.user, code="app2")
        p2 = make_placement(app2, code="p2")
        n1 = make_network("ex2n1", "Ex2 N1")
        n2 = make_network("ex2n2", "Ex2 N2")
        add_line(p2, n1, priority=1, floor="1")
        add_line(p2, n2, priority=2, floor="1")
        va = publish_config(placement=p2, published_by=self.user)
        line = PlacementNetwork.objects.get(placement=p2, priority=1)
        line.cpm_floor = Decimal("5")
        line.save(update_fields=["cpm_floor"])
        vb = publish_config(placement=p2, published_by=self.user)
        exp2 = create_experiment(
            placement=p2,
            created_by=self.user,
            name="exp2",
            variant_a_version_id=va.id,
            variant_b_version_id=vb.id,
        )
        import hashlib

        mismatches = 0
        for i in range(50):
            key = hashlib.sha256(f"user-{i}".encode()).hexdigest()
            if assign_variant(
                experiment=self.experiment, user_key_hash=key
            ) != assign_variant(experiment=exp2, user_key_hash=key):
                mismatches += 1
        # With independent salts, roughly half should differ.
        self.assertGreater(mismatches, 5)


class ExperimentConstraintsTests(TestCase):
    def setUp(self):
        self.pipe = make_full_pipeline(num_networks=2)
        v1 = self.pipe["version"]
        line = PlacementNetwork.objects.get(
            placement=self.pipe["placement"], priority=1
        )
        line.cpm_floor = Decimal("8")
        line.save(update_fields=["cpm_floor"])
        self.v2 = publish_config(
            placement=self.pipe["placement"], published_by=self.pipe["user"]
        )

    def test_only_one_running_experiment_per_placement(self):
        create_experiment(
            placement=self.pipe["placement"],
            created_by=self.pipe["user"],
            name="first",
            variant_a_version_id=self.pipe["version"].id,
            variant_b_version_id=self.v2.id,
        )
        with self.assertRaises(ExperimentError):
            create_experiment(
                placement=self.pipe["placement"],
                created_by=self.pipe["user"],
                name="second",
                variant_a_version_id=self.pipe["version"].id,
                variant_b_version_id=self.v2.id,
            )

    def test_same_version_for_both_variants_rejected(self):
        with self.assertRaises(ExperimentError):
            create_experiment(
                placement=self.pipe["placement"],
                created_by=self.pipe["user"],
                name="same",
                variant_a_version_id=self.pipe["version"].id,
                variant_b_version_id=self.pipe["version"].id,
            )

    def test_foreign_version_rejected(self):
        other = make_full_pipeline(
            num_networks=1, username="other_pipeline", code="other-pipe",
            prefix="oth",
        )
        with self.assertRaises(ExperimentError):
            create_experiment(
                placement=self.pipe["placement"],
                created_by=self.pipe["user"],
                name="foreign",
                variant_a_version_id=other["version"].id,
                variant_b_version_id=self.v2.id,
            )

    def test_cannot_create_experiment_on_foreign_placement_via_api(self):
        other = make_user("intruder")
        resp = auth_client(other).post(
            "/api/experiments/",
            {
                "placement": self.pipe["placement"].id,
                "name": "hack",
                "variant_a_version": self.pipe["version"].id,
                "variant_b_version": self.v2.id,
            },
            format="json",
        )
        self.assertEqual(resp.status_code, 400)


class HistoricalCohortTests(TestCase):
    """Publishing a new config must not change historical cohort attribution
    or stats."""

    def setUp(self):
        self.pipe = make_full_pipeline(num_networks=1)
        self.placement = self.pipe["placement"]
        self.app = self.pipe["app"]
        self.net = self.pipe["networks"][0]
        self.v1 = self.pipe["version"]
        line = PlacementNetwork.objects.get(placement=self.placement, priority=1)
        line.cpm_floor = Decimal("9")
        line.save(update_fields=["cpm_floor"])
        self.v2 = publish_config(placement=self.placement, published_by=self.pipe["user"])
        self.experiment = create_experiment(
            placement=self.placement,
            created_by=self.pipe["user"],
            name="cohort",
            variant_a_version_id=self.v1.id,
            variant_b_version_id=self.v2.id,
        )
        self.now = timezone.now()

    def _event(self, eid, variant, version, etype, **kw):
        create_event(
            eid, self.app, self.placement, self.net, etype,
            self.now - timedelta(minutes=kw.pop("minutes_ago", 5)),
            config_version=version,
            experiment=self.experiment,
            experiment_variant=variant,
            **kw,
        )

    def test_stats_use_frozen_version_and_variant(self):
        # A: 10 impressions, 8 fills, 8 * 0.002 revenue
        for i in range(10):
            self._event(f"a-imp-{i}", "A", self.v1, EventType.IMPRESSION,
                        minutes_ago=30 + i)
        for i in range(8):
            self._event(f"a-fill-{i}", "A", self.v1, EventType.FILL,
                        minutes_ago=30 + i)
            self._event(f"a-rev-{i}", "A", self.v1, EventType.REVENUE,
                        minutes_ago=30 + i, revenue=Decimal("0.002"))
        # B: 4 impressions, 2 fills, 2 * 0.005
        for i in range(4):
            self._event(f"b-imp-{i}", "B", self.v2, EventType.IMPRESSION,
                        minutes_ago=60 + i)
        for i in range(2):
            self._event(f"b-fill-{i}", "B", self.v2, EventType.FILL,
                        minutes_ago=60 + i)
            self._event(f"b-rev-{i}", "B", self.v2, EventType.REVENUE,
                        minutes_ago=60 + i, revenue=Decimal("0.005"))

        stats = experiment_stats(self.experiment)
        a, b = stats["A"], stats["B"]
        self.assertEqual(a["impressions"], 10)
        self.assertEqual(a["fills"], 8)
        self.assertEqual(a["fill_rate"], "0.800000")
        # ecpm = 0.016 / 10 * 1000 = 1.6
        self.assertEqual(a["ecpm"], "1.600000")
        self.assertEqual(a["revenue"], "0.016000")
        self.assertEqual(b["fill_rate"], "0.500000")
        # ecpm = 0.010 / 4 * 1000 = 2.5
        self.assertEqual(b["ecpm"], "2.500000")

        # Now publish a v3 and even delete the experiment's running status
        # semantics: historical events still point at v1/v2.
        line = PlacementNetwork.objects.get(placement=self.placement, priority=1)
        line.cpm_floor = Decimal("20")
        line.save(update_fields=["cpm_floor"])
        v3 = publish_config(
            placement=self.placement, published_by=self.pipe["user"], note="v3"
        )
        stats2 = experiment_stats(self.experiment)
        self.assertEqual(stats2, stats)
        # frozen version references intact
        versions = set(
            AdEvent.objects.filter(experiment=self.experiment)
            .values_list("config_version__version", flat=True)
            .distinct()
        )
        self.assertEqual(versions, {1, 2})
        self.assertNotIn(3, versions)

    def test_zero_impressions_reports_null_metrics(self):
        stats = experiment_stats(self.experiment)
        self.assertIsNone(stats["A"]["fill_rate"])
        self.assertIsNone(stats["A"]["ecpm"])
        self.assertEqual(stats["A"]["impressions"], 0)


class SDKAssignEndpointTests(TestCase):
    def setUp(self):
        self.pipe = make_full_pipeline(num_networks=2)
        self.placement = self.pipe["placement"]
        v1 = self.pipe["version"]
        line = PlacementNetwork.objects.get(placement=self.placement, priority=1)
        line.cpm_floor = Decimal("7")
        line.save(update_fields=["cpm_floor"])
        self.v2 = publish_config(
            placement=self.placement, published_by=self.pipe["user"]
        )
        self.experiment = create_experiment(
            placement=self.placement,
            created_by=self.pipe["user"],
            name="sdkexp",
            variant_a_version_id=v1.id,
            variant_b_version_id=self.v2.id,
        )
        self.app = self.pipe["app"]

    def test_assign_is_stable_across_calls(self):
        client = sdk_client()
        url = f"/api/sdk/apps/{self.app.code}/placements/{self.placement.code}/assign/"
        kwargs = {
            "content_type": "application/json",
            "HTTP_X_SDK_KEY": str(self.app.sdk_key),
        }
        r1 = client.post(url, data=json.dumps({"user_key_hash": "u" * 40}), **kwargs)
        self.assertEqual(r1.status_code, 200)
        variant = r1.data["variant"]
        version_id = r1.data["config_version_id"]
        r2 = client.post(url, data=json.dumps({"user_key_hash": "u" * 40}), **kwargs)
        self.assertEqual(r2.data["variant"], variant)
        self.assertEqual(r2.data["config_version_id"], version_id)
        expected = self.v2.id if variant == "B" else self.pipe["version"].id
        self.assertEqual(version_id, expected)

    def test_assign_requires_sdk_key(self):
        resp = sdk_client().post(
            f"/api/sdk/apps/{self.app.code}/placements/{self.placement.code}/assign/",
            data=json.dumps({"user_key_hash": "x"}),
            content_type="application/json",
        )
        self.assertEqual(resp.status_code, 401)

    def test_stats_endpoint_scoped_to_owner(self):
        resp = auth_client(self.pipe["user"]).get(
            f"/api/experiments/{self.experiment.id}/stats/"
        )
        self.assertEqual(resp.status_code, 200)
        self.assertIn("A", resp.data["variants"])
        other = make_user("peeper")
        self.assertEqual(
            auth_client(other)
            .get(f"/api/experiments/{self.experiment.id}/stats/")
            .status_code,
            404,
        )
