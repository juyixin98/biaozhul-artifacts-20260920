"""Shared factories / builders for the test suite."""
from decimal import Decimal

from django.contrib.auth import get_user_model
from django.utils import timezone
from rest_framework.authtoken.models import Token
from rest_framework.test import APIClient

from apps.catalog.models import (
    AdNetwork,
    App,
    Placement,
    PlacementNetwork,
)
from apps.catalog.services import publish_config
from apps.ingestion.models import AdEvent, EventType

User = get_user_model()


def make_user(username="dev", password="password123"):
    user = User.objects.create_user(username=username, password=password)
    Token.objects.get_or_create(user=user)
    return user


def auth_client(user):
    client = APIClient()
    client.force_authenticate(user=user)
    return client


def make_app(owner=None, code="app1", name="App One"):
    owner = owner or make_user()
    return App.objects.create(owner=owner, code=code, name=name)


def make_network(code="admob", name="AdMob"):
    return AdNetwork.objects.create(code=code, display_name=name)


def make_placement(app=None, code="inter_main", fmt="interstitial"):
    app = app or make_app()
    return Placement.objects.create(app=app, code=code, name=code, format=fmt)


def add_line(placement, network, priority, floor="1.00", fallback=None, enabled=True):
    return PlacementNetwork.objects.create(
        placement=placement,
        network=network,
        priority=priority,
        cpm_floor=Decimal(floor),
        fallback_order=fallback if fallback is not None else priority,
        enabled=enabled,
    )


def publish(placement, user, note=""):
    return publish_config(
        placement=placement, published_by=user, note=note
    )


def make_full_pipeline(
    num_networks=3, username="pipeline_owner", code="pipeline", prefix="net"
):
    """user -> app -> placement -> N lines -> published v1. Returns dict."""
    user = make_user(username)
    app = make_app(user, code=code)
    placement = make_placement(app, code="p")
    networks = []
    for i in range(num_networks):
        network = make_network(f"{prefix}{i}", f"{prefix.title()} {i}")
        networks.append(network)
        add_line(placement, network, priority=i + 1, floor=f"{1 + i}.50")
    version = publish(placement, user)
    return {
        "user": user,
        "app": app,
        "placement": placement,
        "networks": networks,
        "version": version,
    }


def sdk_client():
    """Anonymous client used for X-SDK-Key endpoints."""
    return APIClient()


def event_payload(
    event_id,
    *,
    app,
    placement,
    network,
    event_type=EventType.IMPRESSION,
    event_time=None,
    revenue=None,
    error_code="",
    config_version_id=None,
    experiment_id=None,
    experiment_variant="",
    user_key_hash="",
):
    payload = {
        "event_id": event_id,
        "event_type": event_type,
        "event_time": (event_time or timezone.now()).isoformat(),
        "placement_code": placement.code,
        "network_code": network.code,
    }
    if revenue is not None:
        payload["revenue"] = str(revenue)
    if error_code:
        payload["error_code"] = error_code
    if config_version_id is not None:
        payload["config_version_id"] = config_version_id
    if experiment_id is not None:
        payload["experiment_id"] = experiment_id
    if experiment_variant:
        payload["experiment_variant"] = experiment_variant
    if user_key_hash:
        payload["user_key_hash"] = user_key_hash
    return payload


def create_event(event_id, app, placement, network, event_type, event_time, **kw):
    return AdEvent.objects.create(
        event_id=event_id,
        app=app,
        placement=placement,
        network=network,
        event_type=event_type,
        event_time=event_time,
        **kw,
    )
