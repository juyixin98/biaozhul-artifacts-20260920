import json
from datetime import timedelta
from decimal import Decimal

from django.utils import timezone
from rest_framework import status

from apps.ingestion.models import AdEvent, EventType
from apps.ingestion.services import BatchTooLarge, ingest_batch
from apps.tests.factories import (
    event_payload,
    make_full_pipeline,
    sdk_client,
)
from django.test import TestCase


class IngestionValidationTests(TestCase):
    def setUp(self):
        self.pipe = make_full_pipeline(num_networks=2)
        self.app = self.pipe["app"]
        self.placement = self.pipe["placement"]
        self.net = self.pipe["networks"][0]
        self.now = timezone.now()

    def _post(self, events):
        return sdk_client().post(
            "/api/sdk/events/",
            data=json.dumps({"events": events}),
            content_type="application/json",
            HTTP_X_SDK_KEY=str(self.app.sdk_key),
        )

    def test_sdk_key_required(self):
        resp = sdk_client().post(
            "/api/sdk/events/",
            data=json.dumps({"events": []}),
            content_type="application/json",
        )
        self.assertEqual(resp.status_code, 401)

    def test_empty_batch_rejected(self):
        resp = self._post([])
        self.assertEqual(resp.status_code, 400)

    def test_batch_too_large_rejected(self):
        big = [
            event_payload(
                f"e{i}",
                app=self.app,
                placement=self.placement,
                network=self.net,
                event_time=self.now,
            )
            for i in range(2001)
        ]
        resp = self._post(big)
        self.assertEqual(resp.status_code, 400)
        self.assertIn("2000", resp.data["message"])

    def test_valid_batch_accepted_and_persisted(self):
        events = [
            event_payload(
                f"e{i}",
                app=self.app,
                placement=self.placement,
                network=self.net,
                event_time=self.now - timedelta(minutes=i),
            )
            for i in range(3)
        ]
        resp = self._post(events)
        self.assertEqual(resp.status_code, 202)
        self.assertEqual(resp.data["accepted"], 3)
        self.assertEqual(AdEvent.objects.count(), 3)

    def test_per_item_errors_dont_kill_good_items(self):
        good = event_payload(
            "good",
            app=self.app,
            placement=self.placement,
            network=self.net,
            event_time=self.now,
        )
        bad_type = event_payload(
            "badtype",
            app=self.app,
            placement=self.placement,
            network=self.net,
            event_time=self.now,
        )
        bad_type["event_type"] = "explosion"
        unknown_net = event_payload(
            "unknown",
            app=self.app,
            placement=self.placement,
            network=self.net,
            event_time=self.now,
        )
        unknown_net["network_code"] = "ghost-network"
        missing_time = {
            "event_id": "notime",
            "event_type": "impression",
            "placement_code": self.placement.code,
            "network_code": self.net.code,
        }

        resp = self._post([good, bad_type, unknown_net, missing_time])
        self.assertEqual(resp.status_code, 207)
        self.assertEqual(resp.data["accepted"], 1)
        self.assertEqual(resp.data["failed"], 3)
        by_id = {r["event_id"]: r for r in resp.data["results"]}
        self.assertEqual(by_id["badtype"]["error_code"], "invalid_event_type")
        self.assertEqual(by_id["unknown"]["error_code"], "unknown_network")
        self.assertEqual(by_id["notime"]["error_code"], "invalid_timestamp")

    def test_all_items_bad_returns_400(self):
        bad = [
            {
                "event_id": "x",
                "event_type": "impression",
                "placement_code": self.placement.code,
                "network_code": self.net.code,
                # no time
            }
        ]
        resp = self._post(bad)
        self.assertEqual(resp.status_code, 400)


class IdempotencyTests(TestCase):
    def setUp(self):
        self.pipe = make_full_pipeline(num_networks=2)
        self.app = self.pipe["app"]
        self.placement = self.pipe["placement"]
        self.net = self.pipe["networks"][0]
        self.version = self.pipe["version"]
        self.now = timezone.now()

    def _post(self, events):
        return sdk_client().post(
            "/api/sdk/events/",
            data=json.dumps({"events": events}),
            content_type="application/json",
            HTTP_X_SDK_KEY=str(self.app.sdk_key),
        )

    def test_identical_replay_is_duplicate_not_double_inserted(self):
        payload = event_payload(
            "dup-1",
            app=self.app,
            placement=self.placement,
            network=self.net,
            event_time=self.now,
            config_version_id=self.version.id,
        )
        first = self._post([payload])
        self.assertEqual(first.status_code, 202)
        second = self._post([payload])
        self.assertEqual(second.status_code, 202)
        self.assertEqual(second.data["accepted"], 0)
        self.assertEqual(second.data["duplicates"], 1)
        self.assertEqual(AdEvent.objects.filter(event_id="dup-1").count(), 1)

    def test_conflicting_replay_reports_conflict_per_item(self):
        # Both networks are in the published pipeline (num_networks=2 in
        # setUp), so changing network_code reaches the fingerprint compare.
        payload = event_payload(
            "conf-1",
            app=self.app,
            placement=self.placement,
            network=self.pipe["networks"][0],
            event_time=self.now,
        )
        self._post([payload])
        changed = dict(payload)
        changed["network_code"] = self.pipe["networks"][1].code
        resp = self._post([changed])
        # Nothing accepted in this batch -> 400, and the single item carries
        # the per-entry conflict detail.
        self.assertEqual(resp.status_code, 400)
        result = resp.data["results"][0]
        self.assertEqual(result["status"], "error")
        self.assertEqual(result["error_code"], "conflict")

    def test_duplicate_event_id_within_same_batch(self):
        payload = event_payload(
            "same",
            app=self.app,
            placement=self.placement,
            network=self.net,
            event_time=self.now,
        )
        resp = self._post([payload, dict(payload)])
        self.assertEqual(resp.status_code, 207)
        codes = {r.get("error_code") for r in resp.data["results"] if r["status"] == "error"}
        self.assertIn("duplicate_in_batch", codes)
        self.assertEqual(AdEvent.objects.filter(event_id="same").count(), 1)


class EventSemanticsTests(TestCase):
    def setUp(self):
        self.pipe = make_full_pipeline(num_networks=1)
        self.app = self.pipe["app"]
        self.placement = self.pipe["placement"]
        self.net = self.pipe["networks"][0]
        self.now = timezone.now()

    def _ingest(self, events):
        return ingest_batch(app=self.app, raw_events=events)

    def test_revenue_requires_money_and_rejects_float(self):
        payload = event_payload(
            "rev-float",
            app=self.app,
            placement=self.placement,
            network=self.net,
            event_type=EventType.REVENUE,
            event_time=self.now,
        )
        payload["revenue"] = 0.1  # raw float must be rejected
        result = self._ingest([payload])
        self.assertEqual(result.failed, 1)
        self.assertEqual(result.results[0].error_code, "invalid_money")

    def test_revenue_quantized_to_six_dp(self):
        result = self._ingest(
            [
                event_payload(
                    "rev-ok",
                    app=self.app,
                    placement=self.placement,
                    network=self.net,
                    event_type=EventType.REVENUE,
                    event_time=self.now,
                    revenue="0.1234567",
                )
            ]
        )
        self.assertEqual(result.accepted, 1)
        event = AdEvent.objects.get(event_id="rev-ok")
        self.assertEqual(event.revenue, Decimal("0.123457"))

    def test_negative_money_rejected(self):
        payload = event_payload(
            "rev-neg",
            app=self.app,
            placement=self.placement,
            network=self.net,
            event_type=EventType.REVENUE,
            event_time=self.now,
        )
        payload["revenue"] = "-1"
        result = self._ingest([payload])
        self.assertEqual(result.failed, 1)

    def test_revenue_field_on_impression_rejected(self):
        result = self._ingest(
            [
                event_payload(
                    "imp-money",
                    app=self.app,
                    placement=self.placement,
                    network=self.net,
                    event_time=self.now,
                    revenue="1.00",
                )
            ]
        )
        self.assertEqual(result.failed, 1)
        self.assertEqual(result.results[0].error_code, "unexpected_field")

    def test_late_event_within_window_accepted_out_of_order(self):
        # Events arriving "late" and out of chronological order are accepted.
        late_time = self.now - timedelta(days=2, hours=3)
        result = self._ingest(
            [
                event_payload(
                    "late",
                    app=self.app,
                    placement=self.placement,
                    network=self.net,
                    event_time=late_time,
                ),
                event_payload(
                    "now",
                    app=self.app,
                    placement=self.placement,
                    network=self.net,
                    event_time=self.now,
                ),
            ]
        )
        self.assertEqual(result.accepted, 2)

    def test_event_too_old_rejected(self):
        result = self._ingest(
            [
                event_payload(
                    "ancient",
                    app=self.app,
                    placement=self.placement,
                    network=self.net,
                    event_time=self.now - timedelta(days=31),
                )
            ]
        )
        self.assertEqual(result.failed, 1)
        self.assertEqual(result.results[0].error_code, "event_too_old")

    def test_failure_event_keeps_error_code(self):
        result = self._ingest(
            [
                event_payload(
                    "fail",
                    app=self.app,
                    placement=self.placement,
                    network=self.net,
                    event_type=EventType.FAILURE,
                    event_time=self.now,
                    error_code="timeout",
                )
            ]
        )
        self.assertEqual(result.accepted, 1)
        self.assertEqual(AdEvent.objects.get(event_id="fail").error_code, "timeout")

    def test_cross_app_placement_rejected(self):
        from apps.tests.factories import make_app, make_placement

        other_app = make_app(code="other-app")
        other_placement = make_placement(other_app, code="op")
        result = ingest_batch(
            app=self.app,
            raw_events=[
                event_payload(
                    "cross",
                    app=other_app,
                    placement=other_placement,
                    network=self.net,
                    event_time=self.now,
                )
            ],
        )
        self.assertEqual(result.failed, 1)
        self.assertEqual(result.results[0].error_code, "unknown_placement")
