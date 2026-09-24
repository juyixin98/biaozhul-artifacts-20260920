"""ROS2 camera/IMU alignment node.

Subscribes to ``sensor_msgs/Image`` and ``sensor_msgs/Imu``, feeds the real
message bytes (plus header time) into the rclpy-independent pairing engine
and persists every decision to SQLite with a tamper-evident hash/HMAC chain.

Pairing evidence is published as JSON text on ``/alignment/evidence`` so the
behaviour is observable without any frontend.

Parameters (all dynamically reconfigurable; a validated change is staged and
applied atomically at the next message boundary, receiving a new config
version)::

    tolerance_ms, imu_policy (reuse|exclusive),
    out_of_order_ms, reset_threshold_ms,
    camera_cache_max, imu_cache_max,
    camera_topic, imu_topic, db_path, hmac_secret
"""

from __future__ import annotations

import hashlib
import json
import os
import struct
from pathlib import Path
from typing import Any

from rcl_interfaces.msg import SetParametersResult
from rclpy.node import Node
from sensor_msgs.msg import Image, Imu
from std_msgs.msg import String

from time_alignment.config import AlignConfig
from time_alignment.engine import PairingEngine
from time_alignment.storage import Storage
from time_alignment.types import CAMERA, IMU, NANOSECONDS_PER_SECOND, Status

# ROS parameter name -> engine field for the millisecond doubles.
_MS_PARAMS = {
    "tolerance_ms": "tolerance_ns",
    "out_of_order_ms": "out_of_order_ns",
    "reset_threshold_ms": "reset_threshold_ns",
}
_INT_PARAMS = ("camera_cache_max", "imu_cache_max")
_STR_PARAMS = ("imu_policy",)


def hash_image(msg: Image) -> str:
    """SHA-256 over the real image payload bytes and geometry metadata."""
    h = hashlib.sha256()
    h.update(struct.pack("<qii", msg.header.stamp.sec, msg.height, msg.width))
    h.update(msg.encoding.encode("utf-8"))
    h.update(bytes(msg.data))
    return h.hexdigest()


def hash_imu(msg: Imu) -> str:
    """SHA-256 over canonical little-endian binary of the real IMU fields."""
    h = hashlib.sha256()
    h.update(struct.pack("<q", msg.header.stamp.sec))
    h.update(struct.pack("<q", msg.header.stamp.nanosec))
    vec = (
        msg.linear_acceleration.x, msg.linear_acceleration.y,
        msg.linear_acceleration.z,
        msg.angular_velocity.x, msg.angular_velocity.y,
        msg.angular_velocity.z,
    )
    h.update(struct.pack("<6d", *vec))
    return h.hexdigest()


def parse_frame_seq(frame_id: str, fallback: int) -> tuple[str, int]:
    """``"cam0#42"`` -> (``"cam0"``, 42). Plain frame ids keep the fallback."""
    if "#" in frame_id:
        name, tail = frame_id.rsplit("#", 1)
        try:
            return name, int(tail)
        except ValueError:
            return frame_id, fallback
    return frame_id, fallback


def stamp_ns(header: Any, now_ns: int) -> tuple[int, bool]:
    """Event time from the header; zero stamps fall back to arrival clock."""
    ns = int(header.stamp.sec) * NANOSECONDS_PER_SECOND + int(header.stamp.nanosec)
    if ns == 0:
        return now_ns, True
    return ns, False


class AlignerNode(Node):
    def __init__(self, node_name: str = "ta_aligner", **kwargs: Any) -> None:
        super().__init__(node_name, **kwargs)

        # ---- parameters ------------------------------------------------
        self.declare_parameter("camera_topic", "camera/image_raw")
        self.declare_parameter("imu_topic", "imu/data")
        self.declare_parameter("evidence_topic", "alignment/evidence")
        self.declare_parameter("db_path", str(Path.cwd() / "alignment.sqlite"))
        self.declare_parameter("hmac_secret", "")
        self.declare_parameter("tolerance_ms", 5.0)
        self.declare_parameter("imu_policy", "exclusive")
        self.declare_parameter("out_of_order_ms", 100.0)
        self.declare_parameter("reset_threshold_ms", 100.0)
        self.declare_parameter("camera_cache_max", 2048)
        self.declare_parameter("imu_cache_max", 8192)

        cfg = self._read_config(version=1)
        db_path = str(self.get_parameter("db_path").value)
        Path(db_path).parent.mkdir(parents=True, exist_ok=True)
        secret = str(self.get_parameter("hmac_secret").value) or os.environ.get(
            "TIME_ALIGNMENT_HMAC_SECRET", "")
        self._storage = Storage(db_path, hmac_secret=secret or None)
        self._engine = PairingEngine(
            self._storage, cfg, now_ns=self.get_clock().now().nanoseconds,
            initial_recv_ns=self.get_clock().now().nanoseconds)

        self.add_on_set_parameters_callback(self._on_params)

        # ---- ROS wiring -------------------------------------------------
        from rclpy.qos import QoSProfile, ReliabilityPolicy, HistoryPolicy
        sensor_qos = QoSProfile(
            reliability=ReliabilityPolicy.BEST_EFFORT,
            history=HistoryPolicy.KEEP_LAST, depth=20,
        )
        self.create_subscription(
            Image, str(self.get_parameter("camera_topic").value),
            self._on_image, sensor_qos)
        self.create_subscription(
            Imu, str(self.get_parameter("imu_topic").value),
            self._on_imu, sensor_qos)
        self._evidence_pub = self.create_publisher(
            String, str(self.get_parameter("evidence_topic").value), 10)
        self._stats_timer = self.create_timer(2.0, self._publish_snapshot)

        self.get_logger().info(
            f"aligner ready db={db_path} policy={cfg.imu_policy} "
            f"tol_ms={cfg.tolerance_ns / 1e6} ooo_ms={cfg.out_of_order_ns / 1e6}")

    # ------------------------------------------------------------ config
    def _read_config(self, version: int) -> AlignConfig:
        return AlignConfig(
            version=version,
            tolerance_ns=int(float(self.get_parameter("tolerance_ms").value) * 1e6),
            imu_policy=str(self.get_parameter("imu_policy").value),
            out_of_order_ns=int(float(self.get_parameter("out_of_order_ms").value) * 1e6),
            reset_threshold_ns=int(
                float(self.get_parameter("reset_threshold_ms").value) * 1e6),
            camera_cache_max=int(self.get_parameter("camera_cache_max").value),
            imu_cache_max=int(self.get_parameter("imu_cache_max").value),
        )

    def _on_params(self, params: Any) -> SetParametersResult:
        """Validate synchronously; stage the change for the next boundary.
        Static wiring params (topics/db) are accepted but take effect on the
        next node restart."""
        result = SetParametersResult()
        changes: dict[str, Any] = {}
        for p in params:
            if p.name in _MS_PARAMS:
                if p.value < 0:
                    result.successful = False
                    result.reason = f"{p.name} must be >= 0"
                    return result
                changes[_MS_PARAMS[p.name]] = int(float(p.value) * 1e6)
            elif p.name in _INT_PARAMS:
                if int(p.value) < 1:
                    result.successful = False
                    result.reason = f"{p.name} must be >= 1"
                    return result
                changes[p.name] = int(p.value)
            elif p.name in _STR_PARAMS:
                if p.value not in ("reuse", "exclusive"):
                    result.successful = False
                    result.reason = "imu_policy must be 'reuse' or 'exclusive'"
                    return result
                changes[p.name] = str(p.value)
        try:
            if changes:
                v = self._engine.stage_config(
                    recv_ns=self.get_clock().now().nanoseconds,
                    note="ros parameter update", **changes)
                self.get_logger().info(f"staged config v{v}: {changes} "
                                       "(applies at next message boundary)")
        except ValueError as exc:
            result.successful = False
            result.reason = str(exc)
            return result
        result.successful = True
        return result

    # ------------------------------------------------------------ intake
    def _on_image(self, msg: Image) -> None:
        before = self._storage.count_decisions()
        now_ns = self.get_clock().now().nanoseconds
        ev_ns, stamped = stamp_ns(msg.header, now_ns)
        frame_id, seq = parse_frame_seq(msg.header.frame_id, fallback=-1)
        ev = self._engine.add_event(
            kind=CAMERA, stamp_ns=ev_ns, recv_ns=now_ns, seq=(seq if seq >= 0 else None),
            payload_hash=hash_image(msg), frame_id=frame_id,
            payload={"encoding": msg.encoding, "h": msg.height, "w": msg.width})
        self._emit_new_evidence(before, ev_ns, stamped, source="camera")

    def _on_imu(self, msg: Imu) -> None:
        before = self._storage.count_decisions()
        now_ns = self.get_clock().now().nanoseconds
        ev_ns, stamped = stamp_ns(msg.header, now_ns)
        frame_id, seq = parse_frame_seq(msg.header.frame_id, fallback=-1)
        self._engine.add_event(
            kind=IMU, stamp_ns=ev_ns, recv_ns=now_ns, seq=(seq if seq >= 0 else None),
            payload_hash=hash_imu(msg), frame_id=frame_id,
            payload={"ax": msg.linear_acceleration.x,
                     "wz": msg.angular_velocity.z})
        self._emit_new_evidence(before, ev_ns, stamped, source="imu")

    def _emit_new_evidence(self, before: int, event_ns: int,
                           stamp_fell_back: bool, source: str) -> None:
        rows = self._storage.all_decisions()
        for row in rows[before:]:
            payload = {
                "type": "decision",
                "triggered_by": source,
                "trigger_event_ns": event_ns,
                "stamp_fell_back_to_recv": stamp_fell_back,
                "decision": row,
            }
            m = String()
            m.data = json.dumps(payload, ensure_ascii=False)
            self._evidence_pub.publish(m)
            if row["status"] == Status.MATCHED.value:
                self.get_logger().info(
                    f"MATCH cam_seq={row['camera_seq']}@{row['camera_stamp_ns']/1e9:.3f}s "
                    f"-> imu_seq={row['imu_seq']}@{row['imu_stamp_ns']/1e9:.3f}s "
                    f"dt={row['dt_ns']/1e6:+.3f}ms tie={row['tie']} "
                    f"cfg_v{row['config_version']} ep{row['epoch_id']}")
            else:
                self.get_logger().info(
                    f"{row['status']} seq="
                    f"{row['camera_seq'] if row['camera_seq'] is not None else row['imu_seq']} "
                    f"reason={row['reason']} cfg_v{row['config_version']} "
                    f"ep{row['epoch_id']}")

    def _publish_snapshot(self) -> None:
        snap = self._engine.snapshot()
        snap["type"] = "snapshot"
        snap["decisions_total"] = self._storage.count_decisions()
        m = String()
        m.data = json.dumps(snap, ensure_ascii=False)
        self._evidence_pub.publish(m)

    def shutdown(self) -> None:
        self._engine.finalize(recv_ns=self.get_clock().now().nanoseconds)
        chain = self._storage.verify_chain()
        self.get_logger().info(
            f"finalized: {chain['rows']} decisions, {chain['algorithm']} "
            f"chain verified OK")
        self._storage.close()
