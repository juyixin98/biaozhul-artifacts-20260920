"""Real rclpy integration: actual DDS endpoints, actual monitor scanning the
graph, actual announcement beacons. Skipped when rclpy is unavailable.
"""
import os
import time

import pytest

rclpy = pytest.importorskip("rclpy", reason="ROS2/rclpy not installed")

from rclpy.qos import DurabilityPolicy, HistoryPolicy, QoSProfile, ReliabilityPolicy
from std_msgs.msg import String

from qos_diag.nodes import DiagnosticsNode
from qos_diag.nodes.monitor import MonitorNode
from qos_diag.service import DiagnosticsService
from qos_diag.topology import EndpointState, TopologyStore

TAG = os.getpid()


@pytest.fixture(scope="module")
def ros():
    if not rclpy.ok():
        rclpy.init()
    yield
    # leave rclpy initialised if another test/process already owned it;
    # only shut down a context this fixture created


def _wait_for(cond, timeout=15, interval=0.2):
    t0 = time.time()
    while time.time() - t0 < timeout:
        val = cond()
        if val:
            return val
        time.sleep(interval)
    return None


def test_real_graph_reliability_mismatch_detected(ros):
    topic = f"/it_rel_{TAG}"
    store = TopologyStore(grace_period_s=30)
    mon = MonitorNode(store, poll_period_s=0.2, grace_period_s=30)
    pub = DiagnosticsNode("it_pub_be")
    sub = DiagnosticsNode("it_sub_rel")
    qpub = QoSProfile(depth=10, reliability=ReliabilityPolicy.BEST_EFFORT,
                      durability=DurabilityPolicy.VOLATILE,
                      history=HistoryPolicy.KEEP_LAST)
    qsub = QoSProfile(depth=10, reliability=ReliabilityPolicy.RELIABLE,
                      durability=DurabilityPolicy.VOLATILE,
                      history=HistoryPolicy.KEEP_LAST)
    pub.create_publisher(String, topic, qpub)
    sub.create_subscription(String, topic, lambda m: None, qsub)
    pub.start_announcing(0.5)
    sub.start_announcing(0.5)

    def loop_once():
        for _ in range(5):
            mon.poll_once()
            rclpy.spin_once(mon, timeout_sec=0.05)
            rclpy.spin_once(pub, timeout_sec=0.05)
            rclpy.spin_once(sub, timeout_sec=0.05)
            time.sleep(0.1)

    try:
        got = _wait_for(lambda: (loop_once() or
                                 _diag(store, topic, want="INCOMPATIBLE")),
                        timeout=15)
        assert got, "real DDS graph: R1 incompatibility was not detected"
        m = got["matches"][0]
        assert m["blocking_rules"] == ["R1"]
        # depth/history came through the actual beacon topic, not static JSON
        assert got["publishers"][0]["provenance"]["depth"] == "endpoint_announce"
        assert got["publishers"][0]["qos"]["depth"] == 10
        assert got["publishers"][0]["rmw_observed"]["history"] == "UNKNOWN", \
            "Fast-DDS really does not send history on the wire"
    finally:
        pub.destroy_node(); sub.destroy_node(); mon.destroy_node()


def test_real_graph_durability_repair_recovers(ros):
    topic = f"/it_dur_{TAG}"
    store = TopologyStore(grace_period_s=30)
    mon = MonitorNode("it_mon2", store) if False else MonitorNode(store, 0.2, 30)
    sub = DiagnosticsNode("it_sub_tl")
    qsub = QoSProfile(depth=10, reliability=ReliabilityPolicy.RELIABLE,
                      durability=DurabilityPolicy.TRANSIENT_LOCAL,
                      history=HistoryPolicy.KEEP_LAST)
    sub.create_subscription(String, topic, lambda m: None, qsub)
    sub.start_announcing(0.5)

    bad_pub = DiagnosticsNode("it_pub_volatile")
    bad_pub.create_publisher(String, topic, QoSProfile(
        depth=10, reliability=ReliabilityPolicy.RELIABLE,
        durability=DurabilityPolicy.VOLATILE, history=HistoryPolicy.KEEP_LAST))
    bad_pub.start_announcing(0.5)

    def pump(n=4):
        for _ in range(n):
            mon.poll_once()
            for nd in (mon, sub, bad_pub):
                rclpy.spin_once(nd, timeout_sec=0.03)
            time.sleep(0.1)

    try:
        assert _wait_for(lambda: (pump() or _diag(store, topic, "INCOMPATIBLE")),
                         timeout=15), "R2 not detected on real graph"
        # repair: replace volatile publisher with latched one
        bad_pub.destroy_node()
        good_pub = DiagnosticsNode("it_pub_tl")
        good_pub.create_publisher(String, topic, QoSProfile(
            depth=10, reliability=ReliabilityPolicy.RELIABLE,
            durability=DurabilityPolicy.TRANSIENT_LOCAL,
            history=HistoryPolicy.KEEP_LAST))
        good_pub.start_announcing(0.5)

        def pump2(n=4):
            for _ in range(n):
                mon.poll_once()
                for nd in (mon, sub, good_pub):
                    rclpy.spin_once(nd, timeout_sec=0.03)
                time.sleep(0.1)

        got = _wait_for(lambda: (pump2() or
                                 _diag(store, topic, ("COMPATIBLE", "RISK"))),
                        timeout=15)
        assert got, "repair did not restore compatibility on real graph"
        assert got["severity"] in ("COMPATIBLE", "RISK")
        good_pub.destroy_node()
    finally:
        sub.destroy_node(); mon.destroy_node()


def test_endpoint_disappears_and_returns_through_states(ros):
    topic = f"/it_grace_{TAG}"
    store = TopologyStore(grace_period_s=1.0)
    mon = MonitorNode(store, poll_period_s=0.1, grace_period_s=1.0)
    ephemeral = DiagnosticsNode("it_flaky")
    ephemeral.create_publisher(String, topic, QoSProfile(depth=5))

    def pump(n=3, nodes=(mon,)):
        for _ in range(n):
            mon.poll_once()
            for nd in nodes:
                rclpy.spin_once(nd, timeout_sec=0.02)
            time.sleep(0.05)

    try:
        assert _wait_for(lambda: (pump(2, (mon, ephemeral)) or
                                  _endpoint(store, topic, "it_flaky", "ACTIVE")),
                         timeout=10)
        ephemeral.destroy_node()
        # immediately after disappearance must be SUSPECT, not EXPIRED/failed
        pump(2)
        states = [e.state for e in store.all()
                  if e.topic == topic and e.node_name == "it_flaky"]
        assert states and states[0] == EndpointState.SUSPECT
        # beyond grace -> expired
        time.sleep(1.4)
        mon.poll_once()
        states = [e.state for e in store.all()
                  if e.topic == topic and e.node_name == "it_flaky"]
        assert states[0] == EndpointState.EXPIRED
    finally:
        if ephemeral.handle:
            ephemeral.destroy_node()
        mon.destroy_node()


def _diag(store, topic, want):
    svc = DiagnosticsService(store, 30)
    data = svc.diagnose_topic(topic)
    if isinstance(want, str):
        want = (want,)
    if data["severity"] in want and data["matches"]:
        return data
    return None


def _endpoint(store, topic, node, state):
    pump_proxy = None
    for e in store.all():
        if e.topic == topic and e.node_name == node and e.state.value == state:
            return e
    return None
