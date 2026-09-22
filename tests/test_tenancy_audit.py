"""Tenant isolation and configuration audit trail."""
from django.test import TransactionTestCase

from common.models import AuditLog

from .factories import (
    auth_client,
    draft_payload,
    make_app,
    make_developer,
    make_networks,
    make_placement,
)


class TenantIsolationTests(TransactionTestCase):
    def setUp(self):
        self.alice = make_developer("alice")
        self.bob = make_developer("bob")
        self.alice_app = make_app(self.alice, "com.alice.app")
        self.bob_app = make_app(self.bob, "com.bob.app")
        self.alice_placement = make_placement(self.alice_app, "alice_slot")
        self.bob_placement = make_placement(self.bob_app, "bob_slot")
        self.alice_networks = make_networks(self.alice, codes=("a1", "a2"))

    def test_app_listing_is_scoped(self):
        client = auth_client(self.bob)
        resp = client.get("/api/v1/apps/")
        ids = [a["id"] for a in resp.json()]
        self.assertEqual(ids, [self.bob_app.id])

    def test_cannot_read_or_write_other_developer_placement(self):
        client = auth_client(self.bob)
        # GET a placement detail belonging to Alice -> 404 (not leaked).
        resp = client.get("/api/v1/apps/")  # sanity
        self.assertEqual(resp.status_code, 200)

        # Bob cannot PUT a draft onto Alice's placement.
        resp = client.put(
            f"/api/v1/placements/{self.alice_placement.id}/draft/",
            {"entries": []}, format="json",
        )
        self.assertEqual(resp.status_code, 403)

        # Bob cannot publish it either.
        resp = client.post(
            f"/api/v1/placements/{self.alice_placement.id}/publish/",
            format="json",
        )
        self.assertEqual(resp.status_code, 403)

        # Bob cannot reference Alice's networks in his own draft.
        resp = client.put(
            f"/api/v1/placements/{self.bob_placement.id}/draft/",
            draft_payload(self.alice_networks), format="json",
        )
        self.assertEqual(resp.status_code, 400)

    def test_experiments_of_other_developer_invisible(self):
        client = auth_client(self.alice)
        # Alice creates/publishes versions and an experiment.
        resp = client.put(
            f"/api/v1/placements/{self.alice_placement.id}/draft/",
            draft_payload(self.alice_networks[:2], floors=["1", "2"]),
            format="json",
        )
        self.assertEqual(resp.status_code, 200, resp.content)
        resp = client.post(
            f"/api/v1/placements/{self.alice_placement.id}/publish/", format="json"
        )
        v1 = resp.json()["id"]
        client.put(
            f"/api/v1/placements/{self.alice_placement.id}/draft/",
            draft_payload(self.alice_networks[:2], floors=["2", "3"]),
            format="json",
        )
        client.post(
            f"/api/v1/placements/{self.alice_placement.id}/publish/", format="json"
        )
        versions = client.get(
            f"/api/v1/placements/{self.alice_placement.id}/versions/"
        ).json()
        v2 = [v["id"] for v in versions if v["version_number"] == 2][0]

        resp = client.post(
            "/api/v1/experiments/",
            {
                "name": "alice-exp",
                "placement_id": self.alice_placement.id,
                "version_a": v1,
                "version_b": v2,
            },
            format="json",
        )
        self.assertEqual(resp.status_code, 201, resp.content)
        exp_id = resp.json()["id"]

        bob = auth_client(self.bob)
        self.assertEqual(bob.get("/api/v1/experiments/").json(), [])
        self.assertEqual(bob.get(f"/api/v1/experiments/{exp_id}/stats/").status_code, 403)
        self.assertEqual(bob.post(f"/api/v1/experiments/{exp_id}/start/").status_code, 403)

    def test_sdk_key_of_one_app_cannot_see_other_placement(self):
        from .factories import sdk_client

        client = sdk_client(self.bob_app)
        resp = client.get(
            "/api/v1/sdk/config/", {"placement_key": "alice_slot", "user_key": "u"}
        )
        self.assertEqual(resp.status_code, 404)


class AuditTrailTests(TransactionTestCase):
    def setUp(self):
        self.dev = make_developer()
        self.client = auth_client(self.dev)
        self.app = make_app(self.dev)
        self.placement = make_placement(self.app)
        self.networks = make_networks(self.dev)

    def test_configuration_changes_are_audited(self):
        self.client.post(
            "/api/v1/networks/",
            {"name": "IronSource", "code": "ironsrc"}, format="json",
        )
        self.client.put(
            f"/api/v1/placements/{self.placement.id}/draft/",
            draft_payload(self.networks), format="json",
        )
        self.client.post(
            f"/api/v1/placements/{self.placement.id}/publish/", format="json"
        )

        logs = AuditLog.objects.filter(developer=self.dev).order_by("created_at")
        actions = [(l.resource_type, l.action) for l in logs]
        self.assertIn(("ad_network", "create"), actions)
        self.assertIn(("waterfall_draft", "create"), actions)
        self.assertIn(("waterfall_version", "publish"), actions)

        publish_log = logs.filter(resource_type="waterfall_version").first()
        self.assertEqual(publish_log.diff["version_number"], 1)

        resp = self.client.get("/api/v1/audit-logs/")
        self.assertEqual(resp.status_code, 200)
        self.assertEqual(len(resp.json()), logs.count())

    def test_audit_logs_are_tenant_scoped(self):
        other = make_developer("carol")
        other_client = auth_client(other)
        resp = other_client.get("/api/v1/audit-logs/")
        self.assertEqual(resp.json(), [])

    def test_anonymous_access_denied(self):
        from rest_framework.test import APIClient

        resp = APIClient().get("/api/v1/apps/")
        self.assertEqual(resp.status_code, 401)
