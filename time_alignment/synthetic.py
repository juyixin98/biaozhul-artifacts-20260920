"""Hardware-free synthetic publisher.

Publishes sensor_msgs/Image on ``camera/image`` and sensor_msgs/Imu on
``imu/data`` from either a scenario JSON file (same format as the offline
replay) or a built-in ``nominal`` pattern.  A clock reset is announced on
``clock/reset``.

As everywhere else, rclpy is imported lazily so the message factory and
scenario parsing remain testable without ROS installed.
"""
from __future__ import annotations

import argparse
from typing import Any, Iterator, Optional

from .node import CAMERA_TOPIC, IMU_TOPIC, RESET_TOPIC
from .scenario import load_scenario


# ------------------------------------------------------------- fake messages

class _Stamp:
    def __init__(self, sec: int, nanosec: int) -> None:
        self.sec = sec
        self.nanosec = nanosec


class _Header:
    def __init__(self, stamp_ns: int, frame_id: str) -> None:
        self.stamp = _Stamp(stamp_ns // 1_000_000_000,
                            stamp_ns % 1_000_000_000)
        self.frame_id = frame_id


class FakeImage:
    """Duck-typed sensor_msgs/Image for testing the node without ROS."""

    def __init__(self, stamp_ns: int, frame_id: str = "camera") -> None:
        self.header = _Header(stamp_ns, frame_id)
        self.width = 640
        self.height = 480
        self.encoding = "mono8"
        self.is_bigendian = 0
        self.step = 640
        self.data = b""


class _Vec3:
    def __init__(self, x: float = 0.0, y: float = 0.0, z: float = 0.0) -> None:
        self.x, self.y, self.z = x, y, z


class _Quat:
    def __init__(self) -> None:
        self.x = self.y = self.z = 0.0
        self.w = 1.0


class FakeImu:
    """Duck-typed sensor_msgs/Imu for testing the node without ROS."""

    def __init__(self, stamp_ns: int, frame_id: str = "imu",
                 ax: float = 0.0) -> None:
        self.header = _Header(stamp_ns, frame_id)
        self.linear_acceleration = _Vec3(x=ax)
        self.angular_velocity = _Vec3()
        self.orientation = _Quat()


class FakeEmpty:
    pass


# ------------------------------------------------------------- generation

def nominal_events(duration_s: float = 2.0, imu_hz: float = 200.0,
                   cam_hz: float = 30.0) -> list[dict]:
    """Deterministic nominal pattern: IMU every 5ms, frame every ~33.3ms."""
    events: list[dict] = []
    n_imu = int(duration_s * imu_hz)
    for k in range(n_imu):
        events.append({"kind": "imu", "id": f"i{k:04d}",
                       "t_ns": int(k / imu_hz * 1e9)})
    n_cam = int(duration_s * cam_hz)
    for k in range(n_cam):
        events.append({"kind": "camera", "id": f"c{k:04d}",
                       "t_ns": int(k / cam_hz * 1e9)})
    return events


def scenario_events(path: str) -> list[dict]:
    data = load_scenario(path)
    return [e for e in data["events"] if e["kind"] in ("camera", "imu", "reset")]


def build_message(spec: dict) -> tuple[str, Any]:
    """One scenario event -> (topic, message) using the fake message types."""
    kind = spec["kind"]
    t_ns = spec.get("t_ns")
    if t_ns is None:
        t_ns = int(round(spec["t"] * 1e9))
    if kind == "camera":
        return CAMERA_TOPIC, FakeImage(t_ns, frame_id="camera")
    if kind == "imu":
        return IMU_TOPIC, FakeImu(t_ns, frame_id="imu",
                                  ax=float(abs((t_ns // 1_000_000) % 1000)))
    if kind == "reset":
        return RESET_TOPIC, FakeEmpty()
    raise ValueError(f"unpublishable event kind: {kind}")


def iter_messages(specs: list[dict]) -> Iterator[tuple[str, Any, Optional[int]]]:
    """Yield (topic, message, publish_wall_ns) in scenario receive order."""
    base = None
    # Map scenario receive times onto a wall-clock timeline starting at 0.
    recvs: list[Optional[int]] = []
    for s in specs:
        r = s.get("recv_ns")
        if r is None and "recv" in s:
            r = int(round(s["recv"] * 1e9))
        if r is None and s["kind"] != "reset":
            r = s.get("t_ns")
            if r is None:
                r = int(round(s["t"] * 1e9))
        recvs.append(r)
    real = [r for r in recvs if r is not None]
    base = min(real) if real else 0
    for s, r in zip(specs, recvs):
        yield (*build_message(s), None if r is None else r - base)


# ------------------------------------------------------------- rclpy node

def create_publisher_node(scenario: Optional[str] = None):
    try:
        from rclpy.node import Node
        from rclpy.qos import QoSProfile, ReliabilityPolicy, HistoryPolicy
        from sensor_msgs.msg import Image, Imu as ImuMsg
        from std_msgs.msg import Empty
    except ImportError as e:  # pragma: no cover - environment dependent
        raise SystemExit(
            "rclpy/sensor_msgs not available; source ROS2 first, or use the "
            "offline CLI (python -m time_alignment.cli replay ...)") from e

    specs = (scenario_events(scenario) if scenario
             else nominal_events())
    qos = QoSProfile(depth=64,
                     reliability=ReliabilityPolicy.BEST_EFFORT,
                     history=HistoryPolicy.KEEP_LAST)

    class SyntheticPublisher(Node):
        def __init__(self) -> None:
            super().__init__("synthetic_publisher")
            self._cam_pub = self.create_publisher(Image, CAMERA_TOPIC, qos)
            self._imu_pub = self.create_publisher(ImuMsg, IMU_TOPIC, qos)
            self._reset_pub = self.create_publisher(Empty, RESET_TOPIC, 10)
            self._msgs = list(iter_messages(specs))
            self._idx = 0
            self._wall_ns = None
            self._timer = self.create_timer(0.001, self._tick)
            self.get_logger().info(
                f"publishing {len(self._msgs)} synthetic messages "
                f"(scenario={scenario or 'nominal'})")

        def _tick(self) -> None:
            if self._idx >= len(self._msgs):
                self.get_logger().info("synthetic stream finished")
                self._timer.cancel()
                return
            now = self.get_clock().now().nanoseconds
            if self._wall_ns is None:
                self._wall_ns = now
            # Pace by the scenario's relative receive offset so that
            # reordering / late-arrival patterns reproduce on the wire.
            while self._idx < len(self._msgs):
                topic, msg, off = self._msgs[self._idx]
                if off is not None and now < self._wall_ns + off:
                    return
                if topic == CAMERA_TOPIC:
                    self._cam_pub.publish(self._to_image(msg))
                elif topic == IMU_TOPIC:
                    self._imu_pub.publish(self._to_imu(msg))
                else:
                    self._reset_pub.publish(Empty())
                self._idx += 1

        @staticmethod
        def _to_image(src: FakeImage):
            m = Image()
            m.header.stamp.sec = src.header.stamp.sec
            m.header.stamp.nanosec = src.header.stamp.nanosec
            m.header.frame_id = src.header.frame_id
            m.width, m.height = src.width, src.height
            m.encoding = src.encoding
            m.step = src.step
            m.data = src.data
            return m

        @staticmethod
        def _to_imu(src: FakeImu):
            m = ImuMsg()
            m.header.stamp.sec = src.header.stamp.sec
            m.header.stamp.nanosec = src.header.stamp.nanosec
            m.header.frame_id = src.header.frame_id
            m.linear_acceleration.x = src.linear_acceleration.x
            return m

    return SyntheticPublisher


def main(argv=None) -> None:  # pragma: no cover - needs live ROS
    ap = argparse.ArgumentParser()
    ap.add_argument("--scenario", default=None,
                    help="scenario JSON (defaults to built-in nominal stream)")
    args, ros_args = ap.parse_known_args(argv)
    import rclpy
    NodeCls = create_publisher_node(args.scenario)
    rclpy.init(args=ros_args)
    node = NodeCls()
    try:
        rclpy.spin(node)
    except KeyboardInterrupt:
        pass
    finally:
        node.destroy_node()
        rclpy.shutdown()


if __name__ == "__main__":  # pragma: no cover
    main()
