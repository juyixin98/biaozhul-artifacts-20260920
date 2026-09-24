"""Hardware-free synthetic ROS2 publisher.

Publishes *real* ``sensor_msgs/Image`` and ``sensor_msgs/Imu`` messages over
DDS on a simulation clock, exercising the same edge cases the aligner must
handle:

* steady periodic data,
* a dense burst of camera frames,
* dropped IMU samples,
* duplicated IMU timestamps,
* a camera clock reset (backwards jump).

The scenario is produced purely numerically here (no JSON dependency), so the
launch command alone exercises the whole live path end to end.
"""

from __future__ import annotations

import hashlib
import math
import os
import random
from dataclasses import dataclass
from typing import List

from builtin_interfaces.msg import Time as TimeMsg
from rclpy.node import Node
from sensor_msgs.msg import Image, Imu
from std_msgs.msg import Header

from time_alignment.types import NANOSECONDS_PER_SECOND

# Header stamps are offset from the Unix epoch so that the first frame is not
# a (ambiguous) zero timestamp.
SIM_EPOCH_OFFSET_NS = 1_000_000_000


@dataclass(frozen=True)
class PubEvent:
    kind: str          # "camera" | "imu"
    t_ns: int          # header stamp (event time), can jump backwards
    seq: int
    release_ns: int = 0  # simulation wall time at which it is published


def _ns_to_time(ns: int) -> TimeMsg:
    secs, rem = divmod(int(ns), NANOSECONDS_PER_SECOND)
    msg = TimeMsg()
    msg.sec = int(secs)
    msg.nanosec = int(rem)
    return msg


def build_event_plan(mode: str, duration_s: float, camera_hz: float,
                     imu_hz: float) -> List[PubEvent]:
    """Construct the simulated arrival/event timeline (sorted by sim time)."""
    total_ns = int(duration_s * NANOSECONDS_PER_SECOND)
    cam_period = NANOSECONDS_PER_SECOND / camera_hz
    imu_period = NANOSECONDS_PER_SECOND / imu_hz

    cameras = [(int(k * cam_period), k, int(k * cam_period))
               for k in range(int(camera_hz * duration_s) + 1)]
    imus = [(int(k * imu_period), k, int(k * imu_period))
            for k in range(int(imu_hz * duration_s) + 1)]

    if mode == "drops":
        # Lose the IMU samples around 1.0s: nearby frames must expire.
        imus = [(t, s, r) for (t, s, r) in imus
                if not (900_000_000 <= t <= 1_150_000_000)]
    elif mode == "identical":
        # Duplicate the IMU stamp at 1.0s (two messages, same header time).
        imus.append((1_000_000_000, 100000, 1_000_000_500))
        imus.sort(key=lambda x: (x[2], x[1]))
    elif mode == "burst":
        # Four camera frames emitted within 4ms around 1.0s.
        cameras += [(1_000_000_000 + k * 1_000_000, 200000 + k,
                     1_000_000_000 + k * 1_000_000) for k in range(4)]
    elif mode == "reset":
        # From wall 1.5s on, the camera clock reads 0.5s and climbs again:
        # the header stamp jumps backwards while release time advances.
        cameras = [(t, s, r) for (t, s, r) in cameras if t < 1_500_000_000]
        n_after = int(camera_hz * (duration_s - 1.5)) + 1
        cameras += [
            (500_000_000 + k * int(cam_period), 300000 + k,
             1_500_000_000 + k * int(cam_period))
            for k in range(n_after)
        ]

    events: List[PubEvent] = []
    for t, s, r in cameras:
        if r <= total_ns:
            events.append(PubEvent("camera", t, s, r))
    for t, s, r in imus:
        if r <= total_ns:
            events.append(PubEvent("imu", t, s, r))
    # Delivery order follows release (simulation wall) time; on ties IMU first.
    events.sort(key=lambda e: (e.release_ns, 0 if e.kind == "imu" else 1, e.seq))
    return events


class SyntheticPublisher(Node):
    def __init__(self, node_name: str = "ta_synthetic_publisher", **kwargs) -> None:
        super().__init__(node_name, **kwargs)
        self.declare_parameter("camera_topic", "camera/image_raw")
        self.declare_parameter("imu_topic", "imu/data")
        self.declare_parameter("mode", "steady")  # steady|burst|drops|identical|reset
        self.declare_parameter("duration_s", 3.0)
        self.declare_parameter("camera_hz", 10.0)
        self.declare_parameter("imu_hz", 100.0)
        self.declare_parameter("frame_id", "cam0")
        self.declare_parameter("imu_frame_id", "imu0")
        self.declare_parameter("seed", 20260923)

        self._camera_topic = str(self.get_parameter("camera_topic").value)
        self._imu_topic = str(self.get_parameter("imu_topic").value)
        self._frame_id = str(self.get_parameter("frame_id").value)
        self._imu_frame_id = str(self.get_parameter("imu_frame_id").value)
        mode = str(self.get_parameter("mode").value)
        duration = float(self.get_parameter("duration_s").value)
        camera_hz = float(self.get_parameter("camera_hz").value)
        imu_hz = float(self.get_parameter("imu_hz").value)
        seed = int(self.get_parameter("seed").value)

        self._rng = random.Random(seed)
        self._plan = build_event_plan(mode, duration, camera_hz, imu_hz)
        self._index = 0
        self._start_ns = self.get_clock().now().nanoseconds
        self._done = False
        self._done_at_ns: int | None = None

        self._cam_pub = self.create_publisher(Image, self._camera_topic, 10)
        self._imu_pub = self.create_publisher(Imu, self._imu_topic, 10)
        # 2 ms wall tick: fine enough for 500 Hz simulated IMU; events are
        # released according to the simulation clock offset from node start.
        self._timer = self.create_timer(0.002, self._tick)
        self.get_logger().info(
            f"synthetic publisher mode={mode} events={len(self._plan)} "
            f"camera={self._camera_topic} imu={self._imu_topic}")

    def _header(self, frame_id: str, stamp_ns: int, seq: int) -> Header:
        h = Header()
        h.stamp = _ns_to_time(stamp_ns + SIM_EPOCH_OFFSET_NS)
        # seq travels in the frame id so the aligner can recover it without a
        # custom message type: "<frame_id>#<seq>".
        h.frame_id = f"{frame_id}#{seq}"
        return h

    def _make_camera(self, ev: PubEvent) -> Image:
        msg = Image()
        msg.header = self._header(self._frame_id, ev.t_ns, ev.seq)
        msg.height = 8
        msg.width = 8
        msg.encoding = "mono8"
        msg.is_bigendian = 0
        msg.step = msg.width
        # Real random bytes: a genuine (tiny) image payload, hashed downstream.
        msg.data = bytes(self._rng.getrandbits(8) for _ in range(msg.height * msg.width))
        return msg

    def _make_imu(self, ev: PubEvent) -> Imu:
        msg = Imu()
        msg.header = self._header(self._imu_frame_id, ev.t_ns, ev.seq)
        phase = ev.t_ns / NANOSECONDS_PER_SECOND
        msg.linear_acceleration.x = math.sin(phase) + self._rng.uniform(-0.002, 0.002)
        msg.linear_acceleration.y = self._rng.uniform(-0.01, 0.01)
        msg.linear_acceleration.z = 9.81 + self._rng.uniform(-0.01, 0.01)
        msg.angular_velocity.z = 0.01 * math.cos(phase)
        msg.orientation_covariance[0] = -1.0  # orientation not provided
        return msg

    def _tick(self) -> None:
        now_ns = self.get_clock().now().nanoseconds
        sim_ns = now_ns - self._start_ns
        while self._index < len(self._plan) and self._plan[self._index].release_ns <= sim_ns:
            ev = self._plan[self._index]
            if ev.kind == "camera":
                self._cam_pub.publish(self._make_camera(ev))
            else:
                self._imu_pub.publish(self._make_imu(ev))
            self._index += 1
        if self._index >= len(self._plan):
            if self._done_at_ns is None:
                self.get_logger().info("published all events")
                self._done_at_ns = now_ns
            elif now_ns - self._done_at_ns >= NANOSECONDS_PER_SECOND:
                # Give subscribers a final second to drain, then stop timers.
                self._timer.cancel()
                self._done = True

    @property
    def done(self) -> bool:
        return self._done
