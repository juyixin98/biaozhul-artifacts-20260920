"""Seed a small but complete demo dataset.

Creates:
  * developer ``demo`` / password ``demo-pass123`` (token printed at the end);
  * one app with one placement;
  * 4 ad networks;
  * draft -> publish v1, a changed draft -> publish v2;
  * a running 50/50 experiment bound to v1/v2;
  * a couple of weeks of synthetic events across the 4 networks;
  * one finalized scoring tick for the current slot.

Idempotent-ish: safe to run on an empty DB; on a populated DB it skips
creation and just reports the demo developer's token.
"""
import random
from datetime import timedelta
from decimal import Decimal

from django.core.management.base import BaseCommand
from django.utils import timezone
from rest_framework.authtoken.models import Token

from accounts.models import Developer
from applications.models import AdNetwork, App, Placement
from events.models import Event, EventType, FailureReason
from experiments.models import Experiment
from experiments.services import create_experiment, start_experiment
from waterfall.models import WaterfallVersion
from waterfall.services import create_or_replace_draft, publish_draft

NETWORKS = [
    ("Meta Audience Network", "meta", "1.80"),
    ("AdMob", "admob", "1.50"),
    ("Unity Ads", "unity", "1.10"),
    ("AppLovin", "applovin", "0.90"),
]


class Command(BaseCommand):
    help = "Seed demo developer, app, placement, versions, experiment and events."

    def handle(self, *args, **options):
        developer, created = Developer.objects.get_or_create(
            username="demo",
            defaults={"email": "demo@example.com", "company_name": "Demo Studio"},
        )
        if created:
            developer.set_password("demo-pass123")
            developer.save()
        token, _ = Token.objects.get_or_create(user=developer)

        app, app_created = App.objects.get_or_create(
            developer=developer,
            bundle_id="com.demo.revstream",
            defaults={"name": "RevStream Demo", "platform": "android"},
        )
        placement, _ = Placement.objects.get_or_create(
            app=app,
            placement_key="home_rewarded",
            defaults={"name": "Home rewarded", "ad_type": "rewarded"},
        )
        networks = []
        for name, code, _floor in NETWORKS:
            net, _ = AdNetwork.objects.get_or_create(
                developer=developer, code=code, defaults={"name": name}
            )
            networks.append(net)

        def entries(floors):
            order = list(range(len(networks)))
            return [
                {
                    "network_id": networks[i].id,
                    "priority": i + 1,
                    "fallback_order": order[i] + 1,
                    "floor_cpm": floors[i],
                }
                for i in range(len(networks))
            ]

        if not WaterfallVersion.objects.filter(placement=placement).exists():
            create_or_replace_draft(
                placement=placement,
                entries_data=entries(["1.80", "1.50", "1.10", "0.90"]),
                developer=developer,
                note="initial waterfall",
            )
            v1 = publish_draft(placement=placement, developer=developer)

            # Reorder to v2 (bump AppLovin ahead of Unity).
            create_or_replace_draft(
                placement=placement,
                entries_data=[
                    {"network_id": networks[0].id, "priority": 1,
                     "fallback_order": 1, "floor_cpm": "1.90"},
                    {"network_id": networks[1].id, "priority": 2,
                     "fallback_order": 2, "floor_cpm": "1.55"},
                    {"network_id": networks[3].id, "priority": 3,
                     "fallback_order": 3, "floor_cpm": "1.00"},
                    {"network_id": networks[2].id, "priority": 4,
                     "fallback_order": 4, "floor_cpm": "1.05"},
                ],
                developer=developer,
                note="swap unity/applovin order",
            )
            v2 = publish_draft(placement=placement, developer=developer)

            experiment = create_experiment(
                placement=placement,
                name="v1 vs v2 waterfall",
                version_a_id=v1.id,
                version_b_id=v2.id,
                developer=developer,
            )
            start_experiment(experiment=experiment, developer=developer)

        if Event.objects.filter(app=app).count() == 0:
            self._seed_events(app, placement, networks)

        from scoring.services import run_scoring

        run = run_scoring()
        self.stdout.write(self.style.SUCCESS(
            f"scoring run: slot={run.slot_start:%Y-%m-%dT%H:%M}Z items={run.items.count()}"
        ))

        self.stdout.write(self.style.SUCCESS("Demo data ready."))
        self.stdout.write("  developer : demo / demo-pass123")
        self.stdout.write(f"  token     : {token.key}")
        self.stdout.write(f"  app api key: {app.api_key}")
        self.stdout.write("  placement : home_rewarded")

    def _seed_events(self, app, placement, networks):
        rng = random.Random(20260922)
        now = timezone.now()
        # Quality profiles per network: fill p, error p, ecpm.
        profiles = {
            networks[0].id: (0.92, 0.01, Decimal("1.85")),
            networks[1].id: (0.85, 0.03, Decimal("1.52")),
            networks[2].id: (0.70, 0.08, Decimal("1.08")),
            networks[3].id: (0.62, 0.12, Decimal("0.95")),
        }
        version = (
            WaterfallVersion.objects.filter(placement=placement)
            .order_by("-version_number").first()
        )
        experiment = Experiment.objects.filter(placement=placement).first()

        counter = 0
        bulk = []
        for day in range(7):
            for _ in range(300):
                net = rng.choices(networks, weights=[40, 30, 20, 10], k=1)[0]
                fill_p, err_p, ecpm = profiles[net.id]
                ts = now - timedelta(
                    days=day,
                    hours=rng.randint(0, 23),
                    minutes=rng.randint(0, 59),
                    seconds=rng.randint(0, 59),
                )
                variant = rng.choice(["A", "B"])
                roll = rng.random()
                if roll < fill_p:
                    bulk.append(Event(
                        app=app, event_id=f"seed-{counter}", event_type=EventType.FILL,
                        placement=placement, network=net, version=version,
                        experiment=experiment, variant=variant, occurred_at=ts,
                    ))
                    counter += 1
                    # ~80% of fills lead to an impression + revenue event.
                    if rng.random() < 0.8:
                        bulk.append(Event(
                            app=app, event_id=f"seed-{counter}",
                            event_type=EventType.IMPRESSION,
                            placement=placement, network=net, version=version,
                            experiment=experiment, variant=variant, occurred_at=ts,
                        ))
                        counter += 1
                        amount = (ecpm / Decimal(1000) * Decimal(str(
                            rng.uniform(0.8, 1.2)
                        ))).quantize(Decimal("0.000001"))
                        bulk.append(Event(
                            app=app, event_id=f"seed-{counter}",
                            event_type=EventType.REVENUE,
                            placement=placement, network=net, version=version,
                            experiment=experiment, variant=variant, amount=amount,
                            occurred_at=ts + timedelta(seconds=1),
                        ))
                        counter += 1
                elif roll < fill_p + err_p:
                    bulk.append(Event(
                        app=app, event_id=f"seed-{counter}",
                        event_type=EventType.FAILURE,
                        placement=placement, network=net, version=version,
                        experiment=experiment, variant=variant,
                        failure_reason=rng.choice([
                            FailureReason.TIMEOUT, FailureReason.ERROR
                        ]),
                        occurred_at=ts,
                    ))
                    counter += 1
                else:
                    bulk.append(Event(
                        app=app, event_id=f"seed-{counter}",
                        event_type=EventType.FAILURE,
                        placement=placement, network=net, version=version,
                        experiment=experiment, variant=variant,
                        failure_reason=FailureReason.NO_FILL,
                        occurred_at=ts,
                    ))
                    counter += 1
        Event.objects.bulk_create(bulk, batch_size=500)
        self.stdout.write(f"  seeded {len(bulk)} events")
