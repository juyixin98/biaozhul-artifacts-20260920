"""ROS2 camera/IMU event-time alignment (pure backend).

Public modules:
- config:   validated, versioned alignment parameters
- types:    Event / Decision / status constants
- engine:   the time-alignment state machine (no rclpy dependency)
- storage:  SQLite persistence with a tamper-evident hash/HMAC chain
- scenario: offline JSON scenario replay + synthetic scenario generation
- cli:      command line entry points
"""

__version__ = "0.1.0"
