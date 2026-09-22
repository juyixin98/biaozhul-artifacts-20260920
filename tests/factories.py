"""Shared factories/helpers for the test-suite."""
from __future__ import annotations

from decimal import Decimal

from rest_framework.test import APIClient

from accounts.models import Developer
from applications.models import AdNetwork, App, Placement
from waterfall.services import create_or_replace_draft, publish_draft


def make_developer(username: str = "dev", password: str = "pa55word123") -> Developer:
    return Developer.objects.create_user(username=username, password=password)


def auth_client(developer: Developer) -> APIClient:
    from rest_framework.authtoken.models import Token

    token, _ = Token.objects.get_or_create(user=developer)
    client = APIClient()
    client.credentials(HTTP_AUTHORIZATION=f"Token {token.key}")
    return client


def make_app(developer: Developer, bundle: str = "com.acme.app") -> App:
    return App.objects.create(
        developer=developer, name=bundle, bundle_id=bundle, platform="android"
    )


def make_placement(app: App, key: str = "home_rewarded") -> Placement:
    return Placement.objects.create(
        app=app, name=key, placement_key=key, ad_type="rewarded"
    )


def make_networks(developer: Developer, codes=("meta", "admob", "unity", "vungle")):
    return [
        AdNetwork.objects.create(developer=developer, name=code.title(), code=code)
        for code in codes
    ]


def draft_payload(networks, floors=None, order=None):
    floors = floors or ["1.00"] * len(networks)
    order = order or list(range(len(networks)))
    return {
        "note": "test draft",
        "entries": [
            {
                "network_id": networks[i].id,
                "priority": i + 1,
                "fallback_order": order[i] + 1,
                "floor_cpm": floors[i],
            }
            for i in range(len(networks))
        ],
    }


def publish_waterfall(placement, networks, developer, **kwargs):
    create_or_replace_draft(
        placement=placement,
        entries_data=draft_payload(networks, **kwargs)["entries"],
        developer=developer,
    )
    return publish_draft(placement=placement, developer=developer)


def sdk_client(app: App) -> APIClient:
    client = APIClient()
    client.credentials(HTTP_AUTHORIZATION=f"ApiKey {app.api_key}")
    return client


def event_payload(event_id, event_type, placement, *, network=None, version=None,
                  amount=None, failure_reason=None, occurred_at=None,
                  experiment=None, variant=None, user_key=None):
    from django.utils import timezone

    payload = {
        "event_id": event_id,
        "event_type": event_type,
        "placement_id": placement.id,
        "occurred_at": (occurred_at or timezone.now()).isoformat(),
    }
    if network is not None:
        payload["network_id"] = network.id
    if version is not None:
        payload["version_id"] = version.id
    if amount is not None:
        payload["amount"] = amount
    if failure_reason is not None:
        payload["failure_reason"] = failure_reason
    if experiment is not None:
        payload["experiment_id"] = experiment.id
        payload["variant"] = variant
    if user_key is not None:
        payload["user_key"] = user_key
    return payload


D = Decimal
