"""Versioning: immutable published versions, drafts, max-8 networks, ordering."""
from django.test import TransactionTestCase

from applications.models import AdNetwork
from common.exceptions import NotFoundError, ValidationError
from waterfall.models import CurrentVersion, WaterfallEntry, WaterfallVersion
from waterfall.services import create_or_replace_draft, publish_draft

from .factories import (
    auth_client,
    draft_payload,
    make_app,
    make_developer,
    make_networks,
    make_placement,
)


class WaterfallServiceTests(TransactionTestCase):
    reset_sequences = True

    def setUp(self):
        self.dev = make_developer()
        self.app = make_app(self.dev)
        self.placement = make_placement(self.app)
        self.networks = make_networks(self.dev)

    def test_draft_and_publish_creates_immutable_version(self):
        draft = create_or_replace_draft(
            placement=self.placement,
            entries_data=draft_payload(
                self.networks, floors=["1.00", "0.80", "0.60", "0.40"]
            )["entries"],
            developer=self.dev,
        )
        self.assertEqual(draft.status, WaterfallVersion.Status.DRAFT)

        v1 = publish_draft(placement=self.placement, developer=self.dev)
        self.assertEqual(v1.version_number, 1)
        self.assertEqual(v1.status, WaterfallVersion.Status.PUBLISHED)
        self.assertIsNotNone(v1.published_at)
        self.assertEqual(v1.entries.count(), 4)

        pointer = CurrentVersion.objects.get(placement=self.placement)
        self.assertEqual(pointer.version_id, v1.id)

        # A new draft -> v2 increments; v1 rows are untouched (immutability).
        floors = ["2.00", "1.80", "1.60", "1.40"]
        create_or_replace_draft(
            placement=self.placement,
            entries_data=draft_payload(self.networks, floors=floors)["entries"],
            developer=self.dev,
        )
        v2 = publish_draft(placement=self.placement, developer=self.dev)
        self.assertEqual(v2.version_number, 2)
        v1_entries = list(
            WaterfallEntry.objects.filter(version=v1).order_by("priority")
        )
        self.assertEqual(
            [str(e.floor_cpm) for e in v1_entries],
            ["1.000000", "0.800000", "0.600000", "0.400000"],
        )
        self.assertEqual(
            CurrentVersion.objects.get(placement=self.placement).version_id, v2.id
        )

    def test_replace_draft_does_not_create_second_draft(self):
        create_or_replace_draft(
            placement=self.placement,
            entries_data=draft_payload(self.networks)["entries"],
            developer=self.dev,
        )
        create_or_replace_draft(
            placement=self.placement,
            entries_data=draft_payload(
                self.networks, floors=["3", "2", "1", "0"]
            )["entries"],
            developer=self.dev,
        )
        drafts = WaterfallVersion.objects.filter(
            placement=self.placement, status=WaterfallVersion.Status.DRAFT
        )
        self.assertEqual(drafts.count(), 1)
        self.assertEqual(
            [
                str(e.floor_cpm)
                for e in drafts.first().entries.order_by("priority")
            ],
            ["3.000000", "2.000000", "1.000000", "0.000000"],
        )

    def test_max_eight_networks_enforced(self):
        eight = make_networks(
            self.dev, codes=("n1", "n2", "n3", "n4", "n5", "n6", "n7", "n8")
        )
        create_or_replace_draft(
            placement=self.placement,
            entries_data=draft_payload(eight)["entries"],
            developer=self.dev,
        )  # exactly 8 is allowed

        nine = eight + [AdNetwork.objects.create(
            developer=self.dev, name="n9", code="n9"
        )]
        with self.assertRaises(ValidationError):
            create_or_replace_draft(
                placement=self.placement,
                entries_data=draft_payload(nine)["entries"],
                developer=self.dev,
            )

    def test_priority_and_fallback_must_be_permutations(self):
        payload = draft_payload(self.networks)["entries"]
        payload[0]["priority"] = 2  # duplicate priority, missing 1
        with self.assertRaises(ValidationError):
            create_or_replace_draft(
                placement=self.placement, entries_data=payload, developer=self.dev
            )

        payload2 = draft_payload(self.networks)["entries"]
        payload2[1]["fallback_order"] = 4  # duplicate fallback, missing 2
        with self.assertRaises(ValidationError):
            create_or_replace_draft(
                placement=self.placement, entries_data=payload2, developer=self.dev
            )

    def test_publish_without_draft_is_not_found(self):
        with self.assertRaises(NotFoundError):
            publish_draft(placement=self.placement, developer=self.dev)

    def test_network_of_other_developer_rejected(self):
        other = make_developer("otherdev")
        foreign = make_networks(other, codes=("foreign",))[0]
        payload = draft_payload([foreign])["entries"]
        with self.assertRaises(ValidationError):
            create_or_replace_draft(
                placement=self.placement, entries_data=payload, developer=self.dev
            )


class WaterfallAPITests(TransactionTestCase):
    def setUp(self):
        self.dev = make_developer()
        self.client = auth_client(self.dev)
        self.app = make_app(self.dev)
        self.placement = make_placement(self.app)
        self.networks = make_networks(self.dev)

    def test_draft_publish_api_flow(self):
        resp = self.client.put(
            f"/api/v1/placements/{self.placement.id}/draft/",
            draft_payload(self.networks), format="json",
        )
        self.assertEqual(resp.status_code, 200, resp.content)

        resp = self.client.post(
            f"/api/v1/placements/{self.placement.id}/publish/", format="json"
        )
        self.assertEqual(resp.status_code, 200, resp.content)
        self.assertEqual(resp.json()["version_number"], 1)

        # Second publish with no new draft -> 404.
        resp = self.client.post(
            f"/api/v1/placements/{self.placement.id}/publish/", format="json"
        )
        self.assertEqual(resp.status_code, 404)

        resp = self.client.get(
            f"/api/v1/placements/{self.placement.id}/versions/"
        )
        self.assertEqual(resp.status_code, 200)
        self.assertEqual(len(resp.json()), 1)

    def test_invalid_draft_returns_400(self):
        resp = self.client.put(
            f"/api/v1/placements/{self.placement.id}/draft/",
            {"entries": []}, format="json",
        )
        self.assertEqual(resp.status_code, 400)
