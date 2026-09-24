"""Shared pytest fixtures and builders."""

from __future__ import annotations

import pytest

from app.analyzer import Analyzer
from app.models import (
    Namespace,
    NetworkPolicy,
    Pod,
    Snapshot,
)
from app.models import LabelSelector


def sel(match_labels=None, match_expressions=None) -> LabelSelector:
    data = {}
    if match_labels is not None:
        data["matchLabels"] = match_labels
    if match_expressions is not None:
        data["matchExpressions"] = match_expressions
    return LabelSelector.model_validate(data)


def ns(name, labels=None) -> Namespace:
    return Namespace(name=name, labels=labels or {})


def pod(name, namespace="default", labels=None, ips=None, ports=None) -> Pod:
    data = {"name": name}
    data["namespace"] = namespace
    data["labels"] = labels or {}
    if ips is not None:
        data["ips"] = ips
    if ports is not None:
        data["containerPorts"] = ports
    return Pod.model_validate(data)


def policy(raw: dict) -> NetworkPolicy:
    base = {"namespace": "default", "podSelector": {}}
    base.update(raw)
    return NetworkPolicy.model_validate(base)


def analyzer(namespaces=(), pods=(), policies=()) -> Analyzer:
    snap = Snapshot.model_validate(
        {
            "namespaces": [n.model_dump(by_alias=True) for n in namespaces],
            "pods": [p.model_dump(by_alias=True) for p in pods],
            "policies": [p.model_dump(by_alias=True) for p in policies],
        }
    )
    return Analyzer(snap)


def q(source, destination, protocol="TCP", port=None, src_ns="default",
      dst_ns="default"):
    return {
        "source": {"pod": {"namespace": src_ns, "name": source}},
        "destination": {"pod": {"namespace": dst_ns, "name": destination}},
        "protocol": protocol,
        "port": port,
    }


def q_ip(source, ip, protocol="TCP", port=None, src_ns="default"):
    return {
        "source": {"pod": {"namespace": src_ns, "name": source}},
        "destination": {"ip": ip},
        "protocol": protocol,
        "port": port,
    }


@pytest.fixture
def build_ns():
    return ns


@pytest.fixture
def build_pod():
    return pod


@pytest.fixture
def build_policy():
    return policy


@pytest.fixture
def make_analyzer():
    return analyzer
