"""Topology store: endpoint lifecycle, timestamps, graceful absent-period.

A discovered endpoint can vanish from the DDS graph for two very different
reasons:

  1. the peer process died / left the domain — permanent until it returns;
  2. discovery is transiently flapping (SDP not yet exchanged, network hiccup,
     participant lease not yet renewed) — it will come back momentarily.

We therefore never mark an endpoint FAILED the instant it is missing. Each
endpoint is a state machine:

    ACTIVE  --missing >= grace_period--> EXPIRED
    ACTIVE  --missing <  grace_period--> SUSPECT (still treated as present)
    EXPIRED --rediscovered------------> ACTIVE (new last_seen, endpoint_returns++)

Announcements (out-of-band /qos_diag/endpoints beacons) carry history/depth
that DDS discovery does not transport; they are *corroborating* evidence and
are fused per GID/key. An announcement without a live DDS endpoint does not
create a topology endpoint — the DDS graph remains source of truth for
presence, announcements only enrich attributes.
"""
from __future__ import annotations

import time
from enum import Enum
from typing import Optional

from pydantic import BaseModel, Field

from .qos_model import EndpointQoS, QoSProfile, EvidenceSource


class EndpointState(str, Enum):
    ACTIVE = "ACTIVE"
    SUSPECT = "SUSPECT"   # temporarily absent, within grace window
    EXPIRED = "EXPIRED"   # absent beyond grace; not a permanent tombstone — can return


class Endpoint(BaseModel):
    gid: str
    topic: str
    role: str                       # "publisher" | "subscription"
    node_name: str
    node_namespace: str = "/"
    message_type: Optional[str] = None
    observed: EndpointQoS           # fused, best-known profile + provenance
    # raw rmw-observed policy names when present
    rmw_reliability: Optional[str] = None
    rmw_durability: Optional[str] = None
    rmw_history: Optional[str] = None
    rmw_depth: Optional[int] = None
    announced_profile: Optional[QoSProfile] = None
    announce_seq: int = 0
    first_seen_ts: float
    last_seen_ts: float             # last time present in DDS graph
    last_announce_ts: Optional[float] = None
    state: EndpointState = EndpointState.ACTIVE
    absent_since_ts: Optional[float] = None
    returns: int = 0                # EXPIRED/SUSPECT -> ACTIVE transitions

    def is_present(self, now: float, grace_s: float) -> bool:
        return self.state == EndpointState.ACTIVE or (
            self.state == EndpointState.SUSPECT and (now - (self.absent_since_ts or now)) < grace_s
        )


class Announcement(BaseModel):
    """Payload of one /qos_diag/endpoints beacon (see nodes/announcer.py)."""

    node_name: str
    node_namespace: str = "/"
    role: str
    topic: str
    gid: Optional[str] = None
    message_type: Optional[str] = None
    reliability: str
    durability: str
    history: str
    depth: int
    seq: int = 0
    sent_ts: float


class TopologyStore:
    def __init__(self, grace_period_s: float = 10.0):
        self.grace_period_s = grace_period_s
        # key -> Endpoint (key is gid-based when a GID is known)
        self._eps: dict[str, Endpoint] = {}
        # identity alias (role:ns:node:topic) -> gid key. Needed because rclpy
        # on Jazzy gives a node no Python accessor for its own endpoint GID, so
        # beacons carry identity, discovery carries GID — correlated here.
        self._ident: dict[str, str] = {}
        # identity -> (Announcement, ts) received before the first discovery
        self._pending: dict[str, tuple] = {}
    @staticmethod
    def key(gid: str | None, topic: str, role: str, node: str, ns: str = "/") -> str:
        if gid:
            return f"{role}:{gid}"
        return TopologyStore.identity_key(topic, role, node, ns)

    @staticmethod
    def identity_key(topic: str, role: str, node: str, ns: str = "/") -> str:
        return f"{role}:{ns}:{node}:{topic}"

    @staticmethod
    def key(gid: str | None, topic: str, role: str, node: str, ns: str = "/") -> str:
        if gid:
            return f"{role}:{gid}"
        return f"{role}:{ns}:{node}:{topic}"

    def upsert_discovered(self, *, gid: str, topic: str, role: str, node: str,
                          ns: str, message_type: str | None,
                          rmw_reliability: str, rmw_durability: str,
                          rmw_history: str, rmw_depth: int,
                          ts: float) -> Endpoint:
        k = self.key(gid, topic, role, node, ns)
        ident = self.identity_key(topic, role, node, ns)
        aliased = self._ident.get(ident)
        if aliased and aliased != k:
            # Same node identity now presents a different DDS GID — the peer
            # restarted. Migrate the endpoint record (preserving first_seen
            # history) onto the live GID; the new rmw observation below
            # overwrites its QoS, so stale pre-restart profiles never linger.
            old = self._eps.pop(aliased, None)
            if old is not None and k not in self._eps:
                old.gid = gid
                self._eps[k] = old
        ep = self._eps.get(k)
        if ep is None:
            profile, prov = self._fuse(
                rmw_reliability, rmw_durability, rmw_history, rmw_depth, None)
            ep = Endpoint(
                gid=gid, topic=topic, role=role, node_name=node, node_namespace=ns,
                message_type=message_type,
                observed=EndpointQoS(profile=profile, provenance=prov),
                rmw_reliability=rmw_reliability, rmw_durability=rmw_durability,
                rmw_history=rmw_history, rmw_depth=rmw_depth,
                first_seen_ts=ts, last_seen_ts=ts)
            self._eps[k] = ep
        else:
            was_absent = ep.state in (EndpointState.SUSPECT, EndpointState.EXPIRED)
            if was_absent:
                ep.returns += 1
            ep.state = EndpointState.ACTIVE
            ep.absent_since_ts = None
            ep.last_seen_ts = ts
            ep.node_name, ep.node_namespace = node, ns
            if message_type:
                ep.message_type = message_type
            ep.rmw_reliability, ep.rmw_durability = rmw_reliability, rmw_durability
            ep.rmw_history, ep.rmw_depth = rmw_history, rmw_depth
        self._ident[ident] = k
        pending = self._pending.pop(ident, None)
        if pending is not None:
            self._fuse_announcement(ep, pending[0], pending[1])
        else:
            prof, prov = self._fuse(rmw_reliability, rmw_durability,
                                    rmw_history, rmw_depth, ep.announced_profile)
            ep.observed = EndpointQoS(profile=prof, provenance=prov)
        return ep

    def apply_announcement(self, a: Announcement, ts: float) -> Optional[Endpoint]:
        """Fuse a beacon with the matching endpoint. Beacons identify endpoints
        by (topic, role, node, namespace) because rclpy does not expose local
        GIDs; discovery identifies them by DDS GID. Correlate via _ident."""
        gid_key = self.key(a.gid, a.topic, a.role, a.node_name,
                           a.node_namespace) if a.gid else None
        ident = self.identity_key(a.topic, a.role, a.node_name, a.node_namespace)
        k = gid_key or self._ident.get(ident)
        ep = self._eps.get(k) if k else None
        if ep is None:
            # Announced but DDS has never discovered it: do NOT invent an
            # endpoint; remember the freshest beacon for when discovery lands.
            old = self._pending.get(ident)
            if old and a.seq < old[0].seq:
                return None
            self._pending[ident] = (a, ts)
            return None
        return self._fuse_announcement(ep, a, ts)

    def _fuse_announcement(self, ep: Endpoint, a: Announcement,
                           ts: float) -> Endpoint:
        if a.seq < ep.announce_seq:
            return ep  # stale beacon
        ep.announce_seq = a.seq
        ep.last_announce_ts = ts
        ep.announced_profile = QoSProfile(
            reliability=a.reliability, durability=a.durability,
            history=a.history, depth=a.depth)
        if a.message_type and not ep.message_type:
            ep.message_type = a.message_type
        prof, prov = self._fuse(ep.rmw_reliability, ep.rmw_durability,
                                ep.rmw_history, ep.rmw_depth, ep.announced_profile)
        ep.observed = EndpointQoS(profile=prof, provenance=prov)
        return ep

    @staticmethod
    def _fuse(rmw_rel, rmw_dur, rmw_hist, rmw_depth, announced: Optional[QoSProfile]):
        """Reliability/durability: trust rmw discovery (wire truth).
        History/depth: DDS does not transport these on Fast-DDS, so use the
        endpoint announcement; fall back to rmw value if it ever reports one."""
        prov: dict[str, str] = {}

        rel = rmw_rel if rmw_rel not in (None, "UNKNOWN", "SYSTEM_DEFAULT") else None
        if rel:
            prov["reliability"] = EvidenceSource.RMW_DISCOVERY
        elif announced and announced.reliability not in ("UNKNOWN", "SYSTEM_DEFAULT"):
            rel, prov["reliability"] = announced.reliability, EvidenceSource.ENDPOINT_ANNOUNCE

        dur = rmw_dur if rmw_dur not in (None, "UNKNOWN", "SYSTEM_DEFAULT") else None
        if dur:
            prov["durability"] = EvidenceSource.RMW_DISCOVERY
        elif announced and announced.durability not in ("UNKNOWN", "SYSTEM_DEFAULT"):
            dur, prov["durability"] = announced.durability, EvidenceSource.ENDPOINT_ANNOUNCE

        hist = rmw_hist if rmw_hist not in (None, "UNKNOWN", "SYSTEM_DEFAULT") else None
        depth = rmw_depth if isinstance(rmw_depth, int) and rmw_depth > 0 else None
        if not hist and announced and announced.history not in ("UNKNOWN", "SYSTEM_DEFAULT"):
            hist = announced.history
            prov["history"] = EvidenceSource.ENDPOINT_ANNOUNCE
        elif hist:
            prov["history"] = EvidenceSource.RMW_DISCOVERY
        if depth is None and announced and announced.depth > 0:
            depth = announced.depth
            prov["depth"] = EvidenceSource.ENDPOINT_ANNOUNCE
        elif depth is not None:
            prov["depth"] = EvidenceSource.RMW_DISCOVERY

        profile = QoSProfile(
            reliability=rel or "UNKNOWN",
            durability=dur or "UNKNOWN",
            history=hist or "UNKNOWN",
            depth=depth or 0,
        )
        return profile, prov

    def mark_absent(self, present_keys: set[str], now: float) -> list[Endpoint]:
        """Advance lifecycle for endpoints missing from the latest DDS graph.
        Returns endpoints whose state changed this tick."""
        changed = []
        for k, ep in self._eps.items():
            if k in present_keys:
                continue
            if ep.state == EndpointState.ACTIVE:
                ep.state = EndpointState.SUSPECT
                ep.absent_since_ts = now
                changed.append(ep)
            elif ep.state == EndpointState.SUSPECT:
                if now - (ep.absent_since_ts or now) >= self.grace_period_s:
                    ep.state = EndpointState.EXPIRED
                    changed.append(ep)
        return changed

    def sweep(self, now: float) -> list[Endpoint]:
        return self.mark_absent(set(), now)

    def get(self, k: str) -> Optional[Endpoint]:
        return self._eps.get(k)

    def all(self) -> list[Endpoint]:
        return list(self._eps.values())

    def present(self, now: float | None = None) -> list[Endpoint]:
        now = now if now is not None else time.time()
        return [e for e in self._eps.values() if e.is_present(now, self.grace_period_s)]

    def active_pair_candidates(self, now: float | None = None):
        """Yield (topic, message_type, [pub endpoints], [sub endpoints]) for
        endpoints currently treated as present (ACTIVE or in grace window)."""
        now = now if now is not None else time.time()
        topics: dict[str, dict[str, list[Endpoint]]] = {}
        for ep in self.present(now):
            t = topics.setdefault(ep.topic, {"publisher": [], "subscription": []})
            t[ep.role].append(ep)
        for topic, sides in topics.items():
            mtypes = {e.message_type for e in sides["publisher"] + sides["subscription"]
                      if e.message_type}
            mt = next(iter(mtypes)) if len(mtypes) == 1 else (
                next(iter(mtypes), None) if mtypes else None)
            yield topic, mt, sides["publisher"], sides["subscription"]
