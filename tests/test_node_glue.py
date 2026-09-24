"""Node-layer tests WITHOUT rclpy: duck-typed messages through AlignerCore.

These exercise the exact conversion + subscription handling path the rclpy
node uses (camera_to_event/imu_to_event/AlignerCore.handle_*), plus the
parameter callback semantics, using the fake messages shipped in synthetic.py.
"""
import pytest

from time_alignment.model import AlignParams
from time_alignment.node import (
    AlignerCore, camera_to_event, imu_to_event, ros_time_to_ns,
    split_source_id, create_aligner_node,
)
from time_alignment.storage import EvidenceStore
from time_alignment.synthetic import (
    FakeImage, FakeImu, FakeEmpty, build_message, iter_messages,
    nominal_events)

KEY = b"t" * 32


@pytest.fixture()
def core(tmp_path):
    store = EvidenceStore(tmp_path / "node.db", KEY)
    gen = {"n": 0}
    c = AlignerCore(AlignParams(), store, clock_gen_fn=lambda: gen["n"])
    c._gen = gen
    yield c
    report = store.verify()
    assert report.ok, report.first_error
    store.close()


def test_ros_time_conversion():
    class S: pass
    s = S(); s.sec = 2; s.nanosec = 500_000_000
    assert ros_time_to_ns(s) == 2_500_000_000


def test_camera_and_imu_message_conversion():
    img = FakeImage(12_345_678)
    ev = camera_to_event(img)
    assert ev.t_ns == 12_345_678
    assert ev.source_id == "camera:12345678"
    assert split_source_id(ev.source_id) == ("camera", 12_345_678)
    im = FakeImu(87_654_321, ax=9.5)
    iev = imu_to_event(im)
    assert iev.t_ns == 87_654_321 and iev.payload["ax"] == 9.5


def test_end_to_end_fake_message_flow(core):
    rows = core.handle_imu(FakeImu(10_000_000))
    assert rows[0]["kind"] == "epoch"
    core.handle_imu(FakeImu(12_000_000))
    core.handle_camera(FakeImage(11_000_000))
    rows = core.finalize()
    pairs = [r for r in rows if r["kind"] == "pair"]
    assert pairs and pairs[0]["status"] == "matched"
    assert pairs[0]["imu"]["id"] == "imu:10000000"


def test_exclusive_then_param_switch_to_reuse(core):
    core.handle_imu(FakeImu(10_000_000))
    core.handle_camera(FakeImage(10_000_000))
    core.handle_camera(FakeImage(10_001_000))  # exclusive: no match
    v = core.update_params(imu_exclusive=False, imu_max_uses=-1)
    assert v == 2
    core.handle_imu(FakeImu(200_000_000))
    core.handle_imu(FakeImu(300_000_000))
    core.handle_camera(FakeImage(300_000_000))
    rows = core.handle_camera(FakeImage(300_001_000))
    pairs = [r for r in rows if r["kind"] == "pair" and r["status"] == "matched"]
    assert all(r["params_version"] == 2 for r in pairs)


def test_invalid_param_rejected_immediately(core):
    with pytest.raises(ValueError):
        core.update_params(tolerance_ns=-5)
    assert core.matcher.params.tolerance_ns >= 0


def test_reset_message_starts_epoch(core):
    core.handle_imu(FakeImu(10_000_000))
    rows = core.handle_reset("test-reset", {"why": "unit"})
    assert any(r["kind"] == "epoch" and r.get("event") == "close" for r in rows)
    core.handle_camera(FakeImage(0))
    assert core.matcher.epoch == 2


def test_clock_generation_change_on_messages_starts_epoch(core):
    core.handle_imu(FakeImu(100_000_000))
    core.handle_camera(FakeImage(101_000_000))
    core._gen["n"] = 1  # simulate /clock restart detected upstream
    rows = core.handle_camera(FakeImage(5_000_000))
    assert any(r.get("event") == "open" and r["epoch"] == 2 for r in rows)


def test_factory_replays_scenario_messages():
    topic, msg = build_message({"kind": "camera", "t": 0.01})
    assert topic == "camera/image"
    assert isinstance(msg, FakeImage)
    topic, msg = build_message({"kind": "imu", "t_ns": 5_000_000})
    assert isinstance(msg, FakeImu)
    topic, msg = build_message({"kind": "reset", "t": 0})
    assert topic == "clock/reset" and isinstance(msg, FakeEmpty)


def test_nominal_event_pattern_sane():
    evs = nominal_events(duration_s=0.1, imu_hz=200.0, cam_hz=20.0)
    kinds = [e["kind"] for e in evs]
    assert kinds.count("imu") == 20 and kinds.count("camera") == 2


def test_iter_messages_preserves_receive_order():
    specs = [
        {"kind": "imu", "t": 0.02, "recv": 0.02},
        {"kind": "camera", "t": 0.0, "recv": 0.03},
    ]
    msgs = list(iter_messages(specs))
    assert msgs[0][0] == "imu/data" and msgs[1][0] == "camera/image"
    # wall offsets are relative to earliest recv
    assert msgs[0][2] == 0 and msgs[1][2] == 10_000_000


def test_node_creation_without_rclpy_exits():
    with pytest.raises(SystemExit):
        create_aligner_node(db_path="/tmp/x.db")
