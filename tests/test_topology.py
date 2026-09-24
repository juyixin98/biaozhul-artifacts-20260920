"""Tests for endpoint lifecycle: transient absence must not be permanent failure."""
import time

from qos_diag.qos_model import QoSProfile
from qos_diag.topology import Announcement, EndpointState, TopologyStore


def disc(store, ts, gid="aabb", topic="/t", role="publisher", node="n1"):
    return store.upsert_discovered(
        gid=gid, topic=topic, role=role, node=node, ns="/",
        message_type="std_msgs/msg/String",
        rmw_reliability="RELIABLE", rmw_durability="VOLATILE",
        rmw_history="UNKNOWN", rmw_depth=0, ts=ts)


def test_new_endpoint_active_with_timestamps():
    s = TopologyStore(grace_period_s=5)
    ep = disc(s, 100.0)
    assert ep.state == EndpointState.ACTIVE
    assert ep.first_seen_ts == 100.0 and ep.last_seen_ts == 100.0
    assert ep.observed.profile.reliability == "RELIABLE"
    # depth unknown until beacon fuses in
    assert ep.observed.profile.depth == 0
    assert ep.observed.source_of("reliability") == "rmw_discovery"


def test_absence_within_grace_is_suspect_not_failure():
    s = TopologyStore(grace_period_s=5)
    disc(s, 100.0)
    changed = s.mark_absent(set(), 101.0)
    assert changed[0].state == EndpointState.SUSPECT
    # still counted as present for pairing while within grace
    assert s.present(103.0), "transient absence must still be treated present"


def test_expiry_only_after_grace():
    s = TopologyStore(grace_period_s=5)
    disc(s, 100.0)
    s.mark_absent(set(), 101.0)
    s.mark_absent(set(), 105.9)
    assert s.get("publisher:aabb").state == EndpointState.SUSPECT
    s.mark_absent(set(), 106.1)
    eps = s.all()
    assert eps[0].state == EndpointState.EXPIRED
    assert not s.present(106.1)


def test_rediscovery_returns_to_active_and_counts():
    s = TopologyStore(grace_period_s=5)
    disc(s, 100.0)
    s.mark_absent(set(), 101.0)
    s.mark_absent(set(), 107.0)
    assert s.all()[0].state == EndpointState.EXPIRED
    ep = disc(s, 110.0)
    assert ep.state == EndpointState.ACTIVE
    assert ep.returns == 1
    assert ep.absent_since_ts is None
    assert ep.last_seen_ts == 110.0
    assert ep.first_seen_ts == 100.0  # identity preserved


def test_announcement_fuses_depth_history_without_inventing_endpoint():
    s = TopologyStore(grace_period_s=5)
    a = Announcement(node_name="n1", node_namespace="/", role="publisher",
                     topic="/t", gid=None, message_type="std_msgs/msg/String",
                     reliability="RELIABLE", durability="VOLATILE",
                     history="KEEP_LAST", depth=42, seq=1, sent_ts=100.5)
    assert s.apply_announcement(a, 100.5) is None
    assert s.all() == [], "beacon alone must not create a presence endpoint"
    disc(s, 101.0)
    ep = s.apply_announcement(a, 101.5)
    assert ep.observed.profile.depth == 42
    assert ep.observed.profile.history == "KEEP_LAST"
    assert ep.observed.source_of("depth") == "endpoint_announce"
    # wire truth still wins for reliability
    assert ep.observed.source_of("reliability") == "rmw_discovery"


def test_stale_beacon_seq_ignored():
    s = TopologyStore(grace_period_s=5)
    disc(s, 100.0)
    a1 = Announcement(node_name="n1", role="publisher", topic="/t",
                      reliability="RELIABLE", durability="VOLATILE",
                      history="KEEP_LAST", depth=42, seq=5, sent_ts=100)
    a0 = Announcement(node_name="n1", role="publisher", topic="/t",
                      reliability="RELIABLE", durability="VOLATILE",
                      history="KEEP_LAST", depth=7, seq=4, sent_ts=99)
    s.apply_announcement(a1, 100.0)
    s.apply_announcement(a0, 101.0)
    assert s.all()[0].observed.profile.depth == 42
