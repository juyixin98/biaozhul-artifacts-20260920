"""Deterministic sample data for local development / demos.

Creates:
  * developer users demo_a / demo_b (password: password123)
  * 5 global ad networks
  * demo_a: app "demo-app" with one rewarded placement, 4-network waterfall,
    two published versions (v1, v2) and a running 50/50 experiment
  * ~3 days of plausible local events spread across the networks
  * one completed scoring run for the latest closed period

Idempotent: safe to run repeatedly; it skips creation if data already exists.
"""
import hashlib
import random
from datetime import timedelta
from decimal import Decimal

from django.contrib.auth import get_user_model
from django.core.management.base import BaseCommand
from django.utils import timezone

from apps.catalog.models import (
    AdNetwork,
    App,
    Placement,
    PlacementNetwork,
)
from apps.catalog.services import publish_config
from apps.experiments.models import Experiment
from apps.experiments.services import create_experiment
from apps.ingestion.models import AdEvent, EventType
from apps.scoring.services import run_latest

User = get_user_model()

NETWORK_DEFS = [
    ("admob", "AdMob", 2.50),
    ("meta", "Meta Audience Network", 3.10),
    ("unity", "Unity Ads", 1.80),
    ("applovin", "AppLovin", 4.20),
    ("ironsource", "ironSource", 2.00),
]


class Command(BaseCommand):
    help = "Populate deterministic sample data (safe to re-run)."

    @staticmethod
    def _sdk_key():
        return None  # use model default uuid4

    def handle(self, *args, **options):
        random.seed(42)

        demo_a, _ = User.objects.get_or_create(
            username="demo_a", defaults={"email": "demo_a@example.com"}
        )
        demo_a.set_password("password123")
        demo_a.save()
        demo_b, _ = User.objects.get_or_create(
            username="demo_b", defaults={"email": "demo_b@example.com"}
        )
        demo_b.set_password("password123")
        demo_b.save()

        networks = {}
        for code, name, floor in NETWORK_DEFS:
            networks[code], _ = AdNetwork.objects.get_or_create(
                code=code, defaults={"display_name": name}
            )

        app, created = App.objects.get_or_create(
            owner=demo_a, code="demo-app", defaults={"name": "Demo Game"}
        )
        if created:
            self.stdout.write(f"Created app {app.code} sdk_key={app.sdk_key}")

        placement, created = Placement.objects.get_or_create(
            app=app,
            code="rewarded_home",
            defaults={"name": "Home Rewarded", "format": Placement.Format.REWARDED},
        )

        if not PlacementNetwork.objects.filter(placement=placement).exists():
            lines = [
                ("applovin", 1, Decimal("4.200000"), 3),
                ("meta", 2, Decimal("3.100000"), 1),
                ("admob", 3, Decimal("2.500000"), 2),
                ("unity", 4, Decimal("1.800000"), 4),
            ]
            for code, prio, floor, fb in lines:
                PlacementNetwork.objects.create(
                    placement=placement,
                    network=networks[code],
                    priority=prio,
                    cpm_floor=floor,
                    fallback_order=fb,
                )
            v1 = publish_config(
                placement=placement, published_by=demo_a, note="initial waterfall"
            )

            # v2: swap top two priorities (avoid the unique partial index by
            # moving one row out of the way first).
            line_meta = PlacementNetwork.objects.get(
                placement=placement, network=networks["meta"]
            )
            line_al = PlacementNetwork.objects.get(
                placement=placement, network=networks["applovin"]
            )
            line_al.priority = 99
            line_al.save(update_fields=["priority", "updated_at"])
            line_meta.priority = 1
            line_meta.save(update_fields=["priority", "updated_at"])
            line_al.priority = 2
            line_al.save(update_fields=["priority", "updated_at"])
            v2 = publish_config(
                placement=placement, published_by=demo_a, note="tune priorities"
            )

            create_experiment(
                placement=placement,
                created_by=demo_a,
                name="50/50 waterfall tune",
                variant_a_version_id=v1.id,
                variant_b_version_id=v2.id,
            )
            self.stdout.write(
                f"Published v{v1.version} and v{v2.version}; experiment created"
            )
        else:
            versions = list(placement.versions.order_by("version"))
            v1, v2 = versions[0], versions[1]

        experiment = Experiment.objects.filter(placement=placement).first()

        # Generate ~3 days of events if none exist.
        if not AdEvent.objects.filter(app=app).exists():
            now = timezone.now()
            event_seq = 0
            network_codes = [c for c, _, _ in NETWORK_DEFS if c != "ironsource"]
            # Different fill/reliability profiles so scoring is interesting.
            profiles = {
                "applovin": (0.92, 0.05, Decimal("4.35")),
                "meta": (0.85, 0.08, Decimal("3.05")),
                "admob": (0.70, 0.15, Decimal("2.40")),
                "unity": (0.55, 0.25, Decimal("1.75")),
            }
            variant_for = {}
            for user_idx in range(200):
                key_hash = hashlib.sha256(f"user-{user_idx}".encode()).hexdigest()
                variant_for[key_hash] = experiment.bucket(key_hash)

            for minute_offset in range(0, 60 * 24 * 3, 7):
                event_time = now - timedelta(minutes=minute_offset)
                code = random.choice(network_codes)
                network = networks[code]
                fill_p, fail_p, payout = profiles[code]
                user_hash = random.choice(list(variant_for.keys()))
                variant = variant_for[user_hash]
                version = v1 if variant == "A" else v2
                event_seq += 1

                impression = AdEvent(
                    event_id=f"seed-{event_seq:06d}",
                    app=app,
                    placement=placement,
                    network=network,
                    event_type=EventType.IMPRESSION,
                    event_time=event_time,
                    config_version=version,
                    experiment=experiment,
                    experiment_variant=variant,
                    user_key_hash=user_hash,
                )
                impression.save()
                roll = random.random()
                if roll < fill_p:
                    event_seq += 1
                    AdEvent.objects.create(
                        event_id=f"seed-{event_seq:06d}",
                        app=app,
                        placement=placement,
                        network=network,
                        event_type=EventType.FILL,
                        event_time=event_time + timedelta(seconds=2),
                        config_version=version,
                        experiment=experiment,
                        experiment_variant=variant,
                        user_key_hash=user_hash,
                    )
                    event_seq += 1
                    AdEvent.objects.create(
                        event_id=f"seed-{event_seq:06d}",
                        app=app,
                        placement=placement,
                        network=network,
                        event_type=EventType.REVENUE,
                        event_time=event_time + timedelta(seconds=3),
                        revenue=(payout / Decimal(1000)).quantize(Decimal("0.000001")),
                        config_version=version,
                        experiment=experiment,
                        experiment_variant=variant,
                        user_key_hash=user_hash,
                    )
                elif roll < fill_p + fail_p:
                    event_seq += 1
                    AdEvent.objects.create(
                        event_id=f"seed-{event_seq:06d}",
                        app=app,
                        placement=placement,
                        network=network,
                        event_type=EventType.FAILURE,
                        event_time=event_time + timedelta(seconds=2),
                        error_code="no_fill",
                        config_version=version,
                        experiment=experiment,
                        experiment_variant=variant,
                        user_key_hash=user_hash,
                    )
            self.stdout.write(f"Generated {event_seq} seed events")

        run, created = run_latest(force=True)
        self.stdout.write(
            self.style.SUCCESS(
                f"Sample data ready. Scoring run {run.id} "
                f"period={run.period_start:%Y-%m-%d %H:%M} created={created}"
            )
        )
        self.stdout.write(
            "\nCredentials:\n"
            "  demo_a / password123 (owns demo-app)\n"
            "  demo_b / password123 (owns nothing)\n"
            f"SDK key: {app.sdk_key}\n"
        )
