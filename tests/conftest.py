"""Shared pytest fixtures.

ROS-dependent tests are skipped when rosbag2_py cannot be imported (e.g. the
ROS environment wasn't sourced). Pure protocol/crypto tests still run.
"""
from __future__ import annotations

import os
import sys
import tempfile
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

ROS_AVAILABLE = False
try:  # pragma: no cover - environment dependent
    import rosbag2_py  # noqa: F401
    import rclpy  # noqa: F401

    ROS_AVAILABLE = True
except Exception:  # pragma: no cover
    ROS_AVAILABLE = False

requires_ros = pytest.mark.skipif(
    not ROS_AVAILABLE, reason="rosbag2_py/rclpy unavailable (source ROS first)"
)


@pytest.fixture()
def tmp_bag_dir(tmp_path: Path) -> Path:
    return tmp_path


@pytest.fixture()
def state_dir(tmp_path: Path) -> Path:
    d = tmp_path / "state"
    d.mkdir()
    return d


@pytest.fixture()
def good_bag(tmp_path: Path):
    from bagtools.bagmaker import make_bag

    bag_dir = tmp_path / "bags" / "demo"
    desc = make_bag(
        bag_dir,
        topics=("/alpha", "/beta"),
        messages=30,
        gap_ns=50_000_000,  # 50 ms
        start_ns=1_000_000_000,
        same_time_groups=3,
    )
    return desc


@pytest.fixture()
def app_settings(tmp_path, monkeypatch):
    from app.config import Settings

    bag_root = tmp_path / "bags"
    bag_root.mkdir(parents=True, exist_ok=True)
    state = tmp_path / "state"
    state.mkdir()
    monkeypatch.setenv("REPLAY_BAG_ROOTS", str(bag_root))
    monkeypatch.setenv("REPLAY_STATE_DIR", str(state))
    monkeypatch.setenv("REPLAY_HMAC_KEY", "unit-test-secret-key-0123456789ab")
    # Re-import settings so env vars take effect (module is imported lazily).
    return Settings()
