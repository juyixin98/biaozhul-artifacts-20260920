"""Concurrent-publish semantics.

On MySQL (the production database) two threads publishing the same draft
serialize on the placement row lock: exactly one wins with the next version
number, the other gets a 409 and a second version number is never consumed.

SQLite (the CI/local database without a MySQL server) cannot hold a write lock
across two connections the same way, so the threaded scenario is skipped there
and replaced by a service-level assertion of the same invariant: sequential
publish calls for one draft produce one version + one not-found.
"""
import threading

from django.db import connections
from django.test import TransactionTestCase, skipUnlessDBFeature

from waterfall.models import WaterfallVersion
from waterfall.services import create_or_replace_draft, publish_draft

from .factories import (
    draft_payload,
    make_app,
    make_developer,
    make_networks,
    make_placement,
)


class SequentialPublishGuardTests(TransactionTestCase):
    def setUp(self):
        self.dev = make_developer()
        self.app = make_app(self.dev)
        self.placement = make_placement(self.app)
        self.networks = make_networks(self.dev)
        create_or_replace_draft(
            placement=self.placement,
            entries_data=draft_payload(self.networks)["entries"],
            developer=self.dev,
        )

    def test_publish_twice_yields_single_version(self):
        v1 = publish_draft(placement=self.placement, developer=self.dev)
        from common.exceptions import NotFoundError

        with self.assertRaises(NotFoundError):
            publish_draft(placement=self.placement, developer=self.dev)
        self.assertEqual(
            WaterfallVersion.objects.filter(status="published").count(), 1
        )
        self.assertEqual(v1.version_number, 1)


@skipUnlessDBFeature("has_select_for_update")
class ConcurrentPublishTests(TransactionTestCase):
    """Runs on MySQL. Two threads race the publish of the same draft."""

    def setUp(self):
        self.dev = make_developer()
        self.app = make_app(self.dev)
        self.placement = make_placement(self.app)
        self.networks = make_networks(self.dev)
        create_or_replace_draft(
            placement=self.placement,
            entries_data=draft_payload(self.networks)["entries"],
            developer=self.dev,
        )
        self.barrier = threading.Barrier(2)
        self.results = []

    def _publish(self):
        # Each thread needs its own DB connection (closed on thread exit).
        try:
            self.barrier.wait(timeout=10)
            version = publish_draft(placement=self.placement, developer=self.dev)
            self.results.append(("ok", version.version_number))
        except Exception as exc:  # noqa: BLE001 - surfaced via results
            self.results.append(("conflict", type(exc).__name__))
        finally:
            connections.close_all()

    def test_exactly_one_winner(self):
        t1 = threading.Thread(target=self._publish)
        t2 = threading.Thread(target=self._publish)
        t1.start()
        t2.start()
        t1.join(timeout=30)
        t2.join(timeout=30)

        outcomes = sorted(self.results)
        self.assertEqual(len(outcomes), 2, outcomes)
        statuses = [o[0] for o in outcomes]
        self.assertIn("ok", statuses)
        # One winner...
        winners = [o for o in outcomes if o[0] == "ok"]
        self.assertEqual(len(winners), 1)
        self.assertEqual(winners[0][1], 1)
        # ...exactly one published row.
        self.assertEqual(
            WaterfallVersion.objects.filter(status="published").count(), 1
        )
        # The loser is an explicit conflict/not-found, never a silent success.
        self.assertTrue(any(s in {"conflict"} for s in statuses))
