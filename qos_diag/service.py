"""Diagnosis service: runs rules over the live topology and shapes API DTOs.

Verdict discipline
------------------
A definite INCOMPATIBLE is reported only for a pub/sub pair whose BOTH
endpoints are ACTIVE in the most recent DDS graph. Pairs touching an endpoint
inside its absence grace window (SUSPECT) are returned as ``PROVISIONAL``
matches: their raw rule outcome is shown for context, but it is never allowed
to turn the topic verdict into INCOMPATIBLE — a momentary non-observation is
not a permanent failure. Pairs are still evaluated for every rule so the
explainable chain is complete.
"""
from __future__ import annotations

import time
from typing import Any

from qos_diag.qos_model import RULES_VERSION
from qos_diag.rules import MatchReport, Severity, evaluate_match
from qos_diag.topology import EndpointState, TopologyStore


class DiagnosticsService:
    def __init__(self, store: TopologyStore, grace_period_s: float):
        self.store = store
        self.grace_period_s = grace_period_s

    def diagnose_topic(self, topic: str, now: float | None = None) -> dict[str, Any]:
        now = now if now is not None else time.time()
        for t, mt, pubs, subs in self.store.active_pair_candidates(now):
            if t == topic:
                return self._shape(t, mt, pubs, subs, now)
        return self._shape(topic, None, [], [], now)

    def diagnose_all(self, now: float | None = None) -> dict[str, Any]:
        now = now if now is not None else time.time()
        topics: dict[str, dict[str, Any]] = {}
        for topic, mt, pubs, subs in self.store.active_pair_candidates(now):
            topics[topic] = self._shape(topic, mt, pubs, subs, now)
        return {
            "generated_ts": now,
            "rules_version": RULES_VERSION,
            "grace_period_s": self.grace_period_s,
            "topics": topics,
        }

    @staticmethod
    def _fqdn(ep) -> str:
        ns = ep.node_namespace or "/"
        base = ns.rstrip("/") or ""
        return f"{base}/{ep.node_name}".replace("//", "/") or f"/{ep.node_name}"

    def _pair(self, topic, p, s, mt) -> tuple[MatchReport, bool]:
        report = evaluate_match(
            topic=topic,
            pub_node=self._fqdn(p), sub_node=self._fqdn(s),
            pub=p.observed, sub=s.observed,
            pub_gid=p.gid, sub_gid=s.gid,
            message_type=p.message_type or s.message_type or mt)
        confirmed = (p.state == EndpointState.ACTIVE
                     and s.state == EndpointState.ACTIVE)
        return report, confirmed

    def _shape(self, topic: str, mt, pubs, subs, now: float) -> dict[str, Any]:
        confirmed_dicts: list[dict] = []
        provisional_dicts: list[dict] = []
        for p in pubs:
            for s in subs:
                report, confirmed = self._pair(topic, p, s, mt)
                d = report.model_dump()
                d["confirmed"] = confirmed
                d["publisher_state"] = p.state.value
                d["subscriber_state"] = s.state.value
                if confirmed:
                    confirmed_dicts.append(d)
                else:
                    provisional_dicts.append(d)

        if confirmed_dicts:
            if any(m["severity"] == "INCOMPATIBLE" for m in confirmed_dicts):
                worst = "INCOMPATIBLE"
            elif any(m["severity"] == "UNKNOWN" for m in confirmed_dicts):
                worst = "UNKNOWN"
            elif any(m["severity"] == "RISK" for m in confirmed_dicts):
                worst = "RISK"
            else:
                worst = "COMPATIBLE"
        elif provisional_dicts:
            # No fully-observed pair right now: report the provisional raw
            # outcome as advisory, never as a hard incompatibility.
            worst = "PROVISIONAL"
        elif pubs and not subs:
            worst = "NO_SUBSCRIBER"
        elif subs and not pubs:
            worst = "NO_PUBLISHER"
        else:
            worst = "EMPTY"

        return {
            "topic": topic,
            "severity": worst,
            "publisher_count": len(pubs),
            "subscription_count": len(subs),
            "matches": confirmed_dicts,
            "provisional_matches": provisional_dicts,
            "publishers": [self._endpoint_dto(e, now) for e in pubs],
            "subscriptions": [self._endpoint_dto(e, now) for e in subs],
        }

    def _endpoint_dto(self, ep, now: float) -> dict[str, Any]:
        absent_for = None
        if ep.absent_since_ts is not None:
            absent_for = round(now - ep.absent_since_ts, 3)
        return {
            "gid": ep.gid,
            "topic": ep.topic,
            "role": ep.role,
            "node": self._fqdn(ep),
            "message_type": ep.message_type,
            "state": ep.state.value,
            "qos": ep.observed.profile.model_dump(),
            "provenance": ep.observed.provenance,
            "rmw_observed": {
                "reliability": ep.rmw_reliability,
                "durability": ep.rmw_durability,
                "history": ep.rmw_history,
                "depth": ep.rmw_depth,
            },
            "announced": ep.announced_profile.model_dump() if ep.announced_profile else None,
            "first_seen_ts": ep.first_seen_ts,
            "last_seen_ts": ep.last_seen_ts,
            "last_announce_ts": ep.last_announce_ts,
            "absent_for_s": absent_for,
            "returns": ep.returns,
        }
