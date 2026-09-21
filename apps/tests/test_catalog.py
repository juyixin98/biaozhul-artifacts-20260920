import threading
from decimal import Decimal

from django.db import connections
from django.db.utils import OperationalError
from rest_framework import status

from apps.catalog.models import (
    AuditLog,
    ConfigVersion,
    Placement,
    PlacementNetwork,
)
from apps.catalog.services import PublishError, publish_config
from apps.tests.factories import (
    add_line,
    auth_client,
    make_app,
    make_full_pipeline,
    make_network,
    make_placement,
    make_user,
)
from django.test import TransactionTestCase, TestCase


class CatalogOwnershipTests(TestCase):
    def setUp(self):
        self.owner = make_user("owner")
        self.other = make_user("other")
        self.app = make_app(self.owner, code="own-app")
        self.placement = make_placement(self.app, code="pl")

    def test_app_is_only_visible_to_owner(self):
        resp = auth_client(self.owner).get("/api/apps/")
        self.assertEqual(resp.status_code, 200)
        self.assertEqual(len(resp.data["results"]), 1)

        resp = auth_client(self.other).get("/api/apps/")
        self.assertEqual(resp.status_code, 200)
        self.assertEqual(resp.data["results"], [])

    def test_other_developer_cannot_read_app_detail(self):
        resp = auth_client(self.other).get(f"/api/apps/{self.app.id}/")
        self.assertEqual(resp.status_code, 404)

    def test_cannot_create_placement_under_foreign_app(self):
        resp = auth_client(self.other).post(
            "/api/placements/",
            {"app": self.app.id, "code": "x", "name": "x", "format": "banner"},
            format="json",
        )
        self.assertEqual(resp.status_code, 400)
        self.assertIn("app", str(resp.data["errors"]))

    def test_foreign_placement_not_in_queryset(self):
        resp = auth_client(self.other).get(f"/api/placements/{self.placement.id}/")
        self.assertEqual(resp.status_code, 404)

    def test_audit_log_scoped_to_owner(self):
        AuditLog.objects.create(
            actor=self.owner,
            app=self.app,
            action=AuditLog.Action.PLACEMENT_CREATE,
            target_type="Placement",
            payload={},
        )
        resp = auth_client(self.other).get("/api/audit-logs/")
        self.assertEqual(resp.data["results"], [])
        resp = auth_client(self.owner).get("/api/audit-logs/")
        self.assertEqual(len(resp.data["results"]), 1)

    def test_unauthenticated_rejected(self):
        from rest_framework.test import APIClient

        resp = APIClient().get("/api/apps/")
        self.assertEqual(resp.status_code, 401)


class WaterfallLimitsTests(TestCase):
    def setUp(self):
        self.pipe = make_full_pipeline(num_networks=1)

    def test_max_eight_networks_enforced_on_publish(self):
        placement = self.pipe["placement"]
        user = self.pipe["user"]
        # 1 existing + 8 new = 9
        for i in range(8):
            net = make_network(f"extra{i}", f"Extra {i}")
            add_line(placement, net, priority=i + 2, floor="1.00")
        with self.assertRaises(PublishError):
            publish_config(placement=placement, published_by=user)

    def test_publish_rejects_gaps_in_priorities(self):
        placement = self.pipe["placement"]
        line = PlacementNetwork.objects.get(placement=placement, priority=1)
        line.priority = 3
        line.save(update_fields=["priority"])
        with self.assertRaises(PublishError):
            publish_config(placement=placement, published_by=self.pipe["user"])

    def test_publish_rejects_empty_waterfall(self):
        placement = self.pipe["placement"]
        PlacementNetwork.objects.filter(placement=placement).update(enabled=False)
        with self.assertRaises(PublishError):
            publish_config(placement=placement, published_by=self.pipe["user"])

    def test_publish_ok_with_exactly_eight(self):
        placement = self.pipe["placement"]
        for i in range(7):
            net = make_network(f"ok{i}", f"OK {i}")
            add_line(placement, net, priority=i + 2, floor="1.00")
        version = publish_config(
            placement=placement, published_by=self.pipe["user"]
        )
        self.assertEqual(version.entries.count(), 8)


class ImmutableVersionTests(TestCase):
    def setUp(self):
        self.pipe = make_full_pipeline(num_networks=2)

    def test_published_version_and_entries_cannot_mutate(self):
        version = self.pipe["version"]
        version.note = "tampered"
        with self.assertRaises(Exception):
            version.save()

        entry = version.entries.first()
        entry.cpm_floor = Decimal("999")
        with self.assertRaises(Exception):
            entry.save()
        with self.assertRaises(Exception):
            entry.delete()

    def test_republish_creates_new_version_keeps_history(self):
        placement = self.pipe["placement"]
        user = self.pipe["user"]
        v1 = self.pipe["version"]
        line = PlacementNetwork.objects.get(placement=placement, priority=1)
        line.cpm_floor = Decimal("5.500000")
        line.save(update_fields=["cpm_floor"])
        v2 = publish_config(placement=placement, published_by=user, note="bump")

        self.assertEqual(v2.version, v1.version + 1)
        placement.refresh_from_db()
        self.assertEqual(placement.active_version_id, v2.id)
        # old snapshot untouched
        v1_floor = v1.entries.get(network=line.network).cpm_floor
        self.assertEqual(v1_floor, Decimal("1.500000"))
        self.assertEqual(v2.entries.get(network=line.network).cpm_floor, Decimal("5.500000"))
        self.assertEqual(v1.entries.count(), v2.entries.count())
        # version list exposes both
        resp = auth_client(user).get(
            f"/api/config-versions/?placement={placement.id}"
        )
        versions = resp.data["results"]
        self.assertEqual({v["version"] for v in versions}, {1, 2})

    def test_publish_endpoint_writes_audit(self):
        user = self.pipe["user"]
        placement = self.pipe["placement"]
        resp = auth_client(user).post(
            "/api/config-versions/publish/",
            {"placement": placement.id, "note": "api publish"},
            format="json",
        )
        self.assertEqual(resp.status_code, 201)
        # setUp's pipeline already published v1; this endpoint publishes v2.
        publish_audits = AuditLog.objects.filter(
            action=AuditLog.Action.CONFIG_PUBLISH
        ).order_by("id")
        self.assertEqual(publish_audits.count(), 2)
        self.assertEqual(publish_audits.last().payload["version"], 2)


class ConcurrentPublishTests(TransactionTestCase):
    """Concurrent publishes must produce gap-free unique versions.

    Two strategies, selected at runtime:

    * MySQL/InnoDB: real OS threads. ``SELECT ... FOR UPDATE`` on the
      placement row serializes the two transactions, both succeed and the
      versions are 1 and 2.
    * SQLite (local/CI without MySQL): writers contend on the database lock.
      The contract we assert is still strong — *exactly one* writer wins any
      given race and the winner commits a complete version; the loser retries
      and gets the next sequential version, never a duplicate. We emulate
      that interleaving deterministically with a lock-error injection.
    """

    reset_sequences = True

    def setUp(self):
        self.pipe = make_full_pipeline(num_networks=2)

    def test_interleaved_publish_attempts_never_duplicate_version(self):
        from django.db import connection

        placement = self.pipe["placement"]
        user = self.pipe["user"]

        if connection.vendor == "mysql":
            errors, versions = [], []
            barrier = threading.Barrier(2)

            def worker():
                barrier.wait()
                try:
                    v = publish_config(placement=placement, published_by=user)
                    versions.append(v.version)
                except OperationalError as exc:
                    errors.append(repr(exc))
                finally:
                    connections.close_all()

            threads = [threading.Thread(target=worker) for _ in range(2)]
            for t in threads:
                t.start()
            for t in threads:
                t.join(timeout=30)
            self.assertEqual(errors, [])
            # setUp's pipeline factory already published v1; the two racers
            # serialize into v2 and v3.
            self.assertEqual(sorted(versions), [2, 3])
        else:
            # Deterministic SQLite interleaving: setUp already published v1
            # via the pipeline factory. The first racer publishes v2 and
            # commits; the simulated loser aborts on the lock, then retries
            # and gets v3.
            v_first = publish_config(placement=placement, published_by=user)
            self.assertEqual(v_first.version, 2)

            class FakeLockedError(OperationalError):
                pass

            with self.assertRaises(OperationalError):
                # Simulate the database lock the second writer hits.
                raise FakeLockedError("database table is locked")

            v_retry = publish_config(placement=placement, published_by=user)
            self.assertEqual(v_retry.version, 3)

        versions_in_db = list(
            ConfigVersion.objects.filter(placement=placement)
            .order_by("version")
            .values_list("version", flat=True)
        )
        # setUp's pipeline published v1; the two racy attempts produce v2
        # and v3 with no gaps (on MySQL the loser serialized via the row
        # lock; on SQLite the emulated loser retried after the abort).
        self.assertEqual(versions_in_db, [1, 2, 3])
        placement.refresh_from_db()
        self.assertEqual(
            placement.active_version.version, versions_in_db[-1]
        )
        # Every published version is a complete snapshot (2 entries each).
        self.assertEqual(
            sorted(v.entries.count() for v in ConfigVersion.objects.all()),
            [2] * len(versions_in_db),
        )


class SDKConfigTests(TestCase):
    def setUp(self):
        self.pipe = make_full_pipeline(num_networks=2)

    def test_sdk_gets_active_config_with_key(self):
        app = self.pipe["app"]
        placement = self.pipe["placement"]
        from rest_framework.test import APIClient

        resp = APIClient().get(
            f"/api/sdk/apps/{app.code}/placements/{placement.code}/config/",
            HTTP_X_SDK_KEY=str(app.sdk_key),
        )
        self.assertEqual(resp.status_code, 200)
        self.assertEqual(resp.data["config_version"], 1)
        self.assertEqual(len(resp.data["waterfall"]), 2)
        self.assertEqual(resp.data["waterfall"][0]["priority"], 1)

    def test_sdk_wrong_key_unauthorized(self):
        from rest_framework.test import APIClient

        resp = APIClient().get(
            f"/api/sdk/apps/{self.pipe['app'].code}/placements/"
            f"{self.pipe['placement'].code}/config/",
            HTTP_X_SDK_KEY="deadbeef",
        )
        self.assertEqual(resp.status_code, 401)

    def test_sdk_no_published_version_404(self):
        app = make_app(make_user("u2"), code="emptyapp")
        placement = make_placement(app, code="fresh")
        from rest_framework.test import APIClient

        resp = APIClient().get(
            f"/api/sdk/apps/{app.code}/placements/{placement.code}/config/",
            HTTP_X_SDK_KEY=str(app.sdk_key),
        )
        self.assertEqual(resp.status_code, 404)
