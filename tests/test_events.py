"""Event ingestion: idempotency, out-of-order, late events, per-item failures,
fixed-point money and batch limits.
"""
from datetime import timedelta
from decimal import Decimal

from django.test import TransactionTestCase
from django.utils import timezone

from events.models import Event, EventType, FailureReason
from events.services import BatchRejected, ingest_batch

from .factories import (
    event_payload,
    make_app,
    make_developer,
    make_networks,
    make_placement,
    publish_waterfall,
    sdk_client,
)


class IngestionServiceTests(TransactionTestCase):
    def setUp(self):
        self.dev = make_developer()
        self.app = make_app(self.dev)
        self.placement = make_placement(self.app)
        self.networks = make_networks(self.dev)
        self.v1 = publish_waterfall(self.placement, self.networks, self.dev)

    def _batch(self, items):
        return ingest_batch(app=self.app, data=items)

    def test_valid_events_stored(self):
        now = timezone.now()
        result = self._batch([
            event_payload("e1", EventType.IMPRESSION, self.placement,
                          network=self.networks[0], version=self.v1,
                          occurred_at=now - timedelta(seconds=5)),
            event_payload("e2", EventType.REVENUE, self.placement,
                          network=self.networks[0], version=self.v1,
                          amount="1.234567", occurred_at=now),
            event_payload("e3", EventType.FAILURE, self.placement,
                          network=self.networks[1], version=self.v1,
                          failure_reason=FailureReason.NO_FILL, occurred_at=now),
        ])
        self.assertEqual(result["received"], 3)
        self.assertEqual(result["stored"], 3)
        self.assertEqual(result["failed"], 0)
        revenue = Event.objects.get(event_id="e2")
        self.assertEqual(revenue.amount, Decimal("1.234567"))

    def test_duplicate_ids_are_idempotent_within_and_across_batches(self):
        payload = event_payload(
            "dup1", EventType.IMPRESSION, self.placement,
            network=self.networks[0],
        )
        first = self._batch([payload])
        self.assertEqual(first["stored"], 1)

        second = self._batch([payload, payload])
        self.assertEqual(second["received"], 2)
        self.assertEqual(second["stored"], 0)
        self.assertEqual(second["duplicate"], 2)
        self.assertEqual(Event.objects.filter(event_id="dup1").count(), 1)

        # A duplicate retry with different payload still does not overwrite.
        mutated = dict(payload)
        mutated["event_type"] = EventType.FAILURE
        mutated["failure_reason"] = FailureReason.ERROR
        self._batch([mutated])
        self.assertEqual(
            Event.objects.get(event_id="dup1").event_type, EventType.IMPRESSION
        )

    def test_out_of_order_and_late_events_accepted(self):
        now = timezone.now()
        # Revenue before its impression; events 6 days old.
        result = self._batch([
            event_payload("late1", EventType.REVENUE, self.placement,
                          network=self.networks[0], version=self.v1,
                          amount="0.500000",
                          occurred_at=now - timedelta(days=6, minutes=2)),
            event_payload("late0", EventType.IMPRESSION, self.placement,
                          network=self.networks[0], version=self.v1,
                          occurred_at=now - timedelta(days=6)),
        ])
        self.assertEqual(result["stored"], 2)
        stored = list(Event.objects.filter(event_id__in=["late0", "late1"]))
        self.assertEqual(len(stored), 2)

    def test_partial_batch_failures_report_specific_items(self):
        good = event_payload(
            "ok1", EventType.IMPRESSION, self.placement, network=self.networks[0]
        )
        bad_type = event_payload(
            "bad1", "not-a-type", self.placement, network=self.networks[0]
        )
        bad_money = event_payload(
            "bad2", EventType.REVENUE, self.placement,
            network=self.networks[0], amount="abc",
        )
        bad_placement = event_payload(
            "bad3", EventType.IMPRESSION, self.placement
        )
        bad_placement["placement_id"] = 999999
        missing_id = {"event_type": EventType.IMPRESSION}

        result = self._batch([good, bad_type, bad_money, bad_placement, missing_id])
        self.assertEqual(result["received"], 5)
        self.assertEqual(result["stored"], 1)
        self.assertEqual(result["failed"], 4)

        failed = {r["index"]: r for r in result["results"] if r["status"] == "failed"}
        self.assertIn("event_type", failed[1]["errors"])
        self.assertIn("amount", failed[2]["errors"])
        self.assertIn("placement_id", failed[3]["errors"])
        self.assertIn("event_id", failed[4]["errors"])

    def test_float_amount_rejected(self):
        result = self._batch([
            event_payload("f1", EventType.REVENUE, self.placement,
                          network=self.networks[0], amount=0.1),
        ])
        self.assertEqual(result["failed"], 1)
        self.assertIn("amount", result["results"][0]["errors"])

    def test_batch_envelope_limits(self):
        with self.assertRaises(BatchRejected):
            self._batch("not-a-list")
        with self.assertRaises(BatchRejected):
            self._batch([])
        oversized = [
            event_payload(f"x{i}", EventType.IMPRESSION, self.placement)
            for i in range(2001)
        ]
        with self.assertRaises(BatchRejected):
            self._batch(oversized)

    def test_foreign_app_resources_rejected(self):
        other = make_developer("other")
        other_app = make_app(other, bundle="com.other.app")
        # An app key for the other app trying to send events with this app's
        # placement / network / version must fail per item.
        result = ingest_batch(
            app=other_app,
            data=[
                event_payload(
                    "x1", EventType.IMPRESSION, self.placement,
                    network=self.networks[0], version=self.v1,
                )
            ],
        )
        self.assertEqual(result["failed"], 1)
        self.assertIn("placement_id", result["results"][0]["errors"])


class EventBatchAPITests(TransactionTestCase):
    def setUp(self):
        self.dev = make_developer()
        self.app = make_app(self.dev)
        self.placement = make_placement(self.app)
        self.networks = make_networks(self.dev)
        publish_waterfall(self.placement, self.networks, self.dev)
        self.client = sdk_client(self.app)

    def test_batch_endpoint_requires_api_key(self):
        from rest_framework.test import APIClient

        anon = APIClient()
        resp = anon.post("/api/v1/events/batch/", [], format="json")
        self.assertEqual(resp.status_code, 401)

    def test_batch_endpoint_roundtrip(self):
        resp = self.client.post(
            "/api/v1/events/batch/",
            [event_payload("a1", EventType.IMPRESSION, self.placement)],
            format="json",
        )
        self.assertEqual(resp.status_code, 200)
        body = resp.json()
        self.assertEqual(body["stored"], 1)
        self.assertEqual(body["duplicate"], 0)

        # Retry the exact same request (flaky-network semantics).
        resp2 = self.client.post(
            "/api/v1/events/batch/",
            [event_payload("a1", EventType.IMPRESSION, self.placement)],
            format="json",
        )
        self.assertEqual(resp2.json()["stored"], 0)
        self.assertEqual(resp2.json()["duplicate"], 1)

    def test_oversized_batch_is_400(self):
        events = [
            event_payload(f"b{i}", EventType.IMPRESSION, self.placement)
            for i in range(2001)
        ]
        resp = self.client.post("/api/v1/events/batch/", events, format="json")
        self.assertEqual(resp.status_code, 400)
