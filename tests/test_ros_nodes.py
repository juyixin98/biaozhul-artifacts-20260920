"""Live ROS2 end-to-end tests using real rclpy nodes and DDS transport.

These actually instantiate the aligner node and the synthetic publisher in
one process, spin the executor, and assert decisions appear in SQLite with a
verified hash chain. Skipped automatically when rclpy is unavailable.
"""

import json
import time

import pytest

rclpy = pytest.importorskip("rclpy")

from rclpy.executors import SingleThreadedExecutor
from sensor_msgs.msg import Image, Imu
from std_msgs.msg import String

from time_alignment.storage import Storage
from time_alignment.types import Status
from time_alignment_nodes.aligner_node import (
    AlignerNode,
    hash_image,
    hash_imu,
    parse_frame_seq,
)
from time_alignment_nodes.synthetic_publisher import (
    SyntheticPublisher,
    build_event_plan,
)




def test_parse_frame_seq():
    assert parse_frame_seq("cam0#42", -1) == ("cam0", 42)
    assert parse_frame_seq("imu0#7", -1) == ("imu0", 7)
    assert parse_frame_seq("plain", 9) == ("plain", 9)


def test_payload_hashes_are_real_and_distinct():
    # Build two real sensor messages and hash their bytes.
    a = Image()
    a.header.stamp.sec = 1
    a.height = a.width = 2
    a.encoding = "mono8"
    a.data = bytes([1, 2, 3, 4])
    b = Image()
    b.header.stamp.sec = 1
    b.height = b.width = 2
    b.encoding = "mono8"
    b.data = bytes([1, 2, 3, 5])
    ha, hb = hash_image(a), hash_image(b)
    assert len(ha) == 64 and ha != hb

    i1, i2 = Imu(), Imu()
    i1.header.stamp.sec = 1
    i2.header.stamp.sec = 1
    i2.linear_acceleration.z = 9.82
    assert hash_imu(i1) != hash_imu(i2)


def test_build_event_plans_cover_edge_cases():
    steady = build_event_plan("steady", 2.0, 10, 100)
    burst = build_event_plan("burst", 2.0, 10, 100)
    drops = build_event_plan("drops", 2.0, 10, 100)
    ident = build_event_plan("identical", 2.0, 10, 100)
    reset = build_event_plan("reset", 2.0, 10, 100)
    assert len(burst) > len(steady)             # burst injects frames
    assert len(drops) < len(steady)             # drops remove IMUs
    stamps = [e.t_ns for e in ident if e.kind == "imu"]
    assert len(stamps) != len(set(stamps))      # duplicated IMU stamp
    cam_stamps = [e.t_ns for e in reset if e.kind == "camera"]
    assert cam_stamps != sorted(cam_stamps)     # camera clock goes backward


@pytest.fixture()
def ros_ctx():
    rclpy.init()
    executor = SingleThreadedExecutor()
    yield executor
    executor.shutdown()
    if rclpy.ok():
        rclpy.shutdown()


def test_live_publisher_to_aligner_end_to_end(tmp_path, ros_ctx):
    """Real DDS traffic: synthetic publisher -> aligner -> SQLite evidence."""
    db = str(tmp_path / "live.sqlite")
    aligner = AlignerNode(parameter_overrides=[
        rclpy.parameter.Parameter("db_path", value=db),
        rclpy.parameter.Parameter("tolerance_ms", value=5.0),
        rclpy.parameter.Parameter("imu_policy", value="exclusive"),
        rclpy.parameter.Parameter("camera_topic", value="ta_e2e/image"),
        rclpy.parameter.Parameter("imu_topic", value="ta_e2e/imu"),
        rclpy.parameter.Parameter("evidence_topic", value="ta_e2e/evidence"),
    ])
    ros_ctx.add_node(aligner)

    publisher = SyntheticPublisher(parameter_overrides=[
        rclpy.parameter.Parameter("mode", value="steady"),
        rclpy.parameter.Parameter("duration_s", value=2.0),
        rclpy.parameter.Parameter("camera_hz", value=10.0),
        rclpy.parameter.Parameter("imu_hz", value=100.0),
        rclpy.parameter.Parameter("camera_topic", value="ta_e2e/image"),
        rclpy.parameter.Parameter("imu_topic", value="ta_e2e/imu"),
    ])
    ros_ctx.add_node(publisher)

    # Collect published evidence JSON.
    evidence = []
    aligner.create_subscription(
        String, "ta_e2e/evidence",
        lambda m: evidence.append(json.loads(m.data)), 10)

    deadline = time.time() + 20
    while time.time() < deadline and not publisher.done:
        ros_ctx.spin_once(timeout_sec=0.05)
    # Let the aligner settle the tail after the publisher's drain second.
    for _ in range(40):
        ros_ctx.spin_once(timeout_sec=0.05)

    # Final drain
    aligner.shutdown()
    ros_ctx.remove_node(aligner)
    ros_ctx.remove_node(publisher)
    aligner.destroy_node()
    publisher.destroy_node()

    st = Storage(db)
    result = st.verify_chain()
    rows = st.all_decisions()
    matched = [r for r in rows if r["status"] == Status.MATCHED.value]
    st.close()

    assert result["ok"] is True
    assert len(matched) >= 5
    # Evidence messages were published over DDS
    assert any(e.get("type") == "decision" for e in evidence)
    # Matched pairs carry real payload hashes of the underlying bytes
    for r in matched:
        assert r["camera_payload_hash"] and r["imu_payload_hash"]
        assert r["abs_dt_ns"] <= 5_000_000
        assert r["candidates"]


def test_live_dynamic_parameter_update(tmp_path, ros_ctx):
    """ros param set is validated, versioned and applied at the boundary."""
    db = str(tmp_path / "param.sqlite")
    aligner = AlignerNode(parameter_overrides=[
        rclpy.parameter.Parameter("db_path", value=db),
        rclpy.parameter.Parameter("tolerance_ms", value=1.0),
        rclpy.parameter.Parameter("camera_topic", value="ta_p/image"),
        rclpy.parameter.Parameter("imu_topic", value="ta_p/imu"),
    ])
    ros_ctx.add_node(aligner)

    # Invalid update rejected
    from rclpy.parameter import Parameter
    bad = Parameter("tolerance_ms", value=-3.0)
    res = aligner.set_parameters_atomically([bad])
    assert res.successful is False

    good = Parameter("tolerance_ms", value=25.0)
    res = aligner.set_parameters_atomically([good])
    assert res.successful is True
    # Not applied yet: only staged until the next message boundary
    assert aligner._engine.config.tolerance_ns == 1_000_000

    # Feed one event through the real callback path to cross the boundary.
    imu = Imu()
    imu.header.frame_id = "imu0#0"
    imu.header.stamp.sec = 10
    imu.linear_acceleration.z = 9.81
    aligner._on_imu(imu)
    assert aligner._engine.config.tolerance_ns == 25_000_000
    assert aligner._engine.config.version == 2
    versions = aligner._storage.list_configs()
    assert [c["version"] for c in versions] == [1, 2]
    assert versions[1]["params"]["tolerance_ns"] == 25_000_000

    aligner.shutdown()
    ros_ctx.remove_node(aligner)
    aligner.destroy_node()
    Storage(db).verify_chain()
