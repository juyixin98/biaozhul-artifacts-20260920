"""QoS profiles (rclpy-dependent; kept separate from the pure protocol logic)."""

from __future__ import annotations

from rclpy.qos import DurabilityPolicy, QoSProfile, ReliabilityPolicy

# Reliable + transient local: late/reconnected subscribers immediately
# receive the last command and the current epoch (latched).
LATCHED_QOS = QoSProfile(
    depth=10,
    reliability=ReliabilityPolicy.RELIABLE,
    durability=DurabilityPolicy.TRANSIENT_LOCAL,
)
# Status/events are periodic live telemetry: volatile is correct there.
VOLATILE_QOS = QoSProfile(depth=20, reliability=ReliabilityPolicy.RELIABLE)
