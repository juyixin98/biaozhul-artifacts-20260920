"""Shared pytest fixtures.

Tests run with ROS disabled: timing and ordering are asserted against the
in-memory ring sink, which is fully deterministic. A separate, opt-in DDS test
module exercises real rclpy publishing when ``RUN_DDS_TESTS=1`` is set.
"""
from __future__ import annotations

import os
import sys
import threading
import time
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))
# The service must import without a sourced ROS environment for the ring-only
# tests; rosbag2_py is only imported lazily where a bag is actually built.

os.environ.setdefault("ROS_ENABLED", "false")
os.environ.setdefault("BAG_ROOT", str(ROOT / "tests" / "_bags"))
os.environ.setdefault("CHECKPOINT_DIR", str(ROOT / "tests" / "_state"))

from rosreplay.bagio import load_bag  # noqa: E402
from rosreplay.config import Settings  # noqa: E402
from rosreplay.sessions import SessionManager  # noqa: E402


def _make_bag(path: Path, seconds: float = 1.0) -> Path:
    sys.path.insert(0, str(ROOT / "scripts"))
    import generate_bag

    generate_bag.generate(str(path), seconds=seconds)
    return path


@pytest.fixture(scope="session", autouse=True)
def _ros_path() -> None:
    """Allow rosbag2_py import even if ROS was not sourced in the shell.

    In CI the environment already sources ROS; this is a convenience for
    running pytest directly when the site-packages path is known.
    """
    try:
        import rosbag2_py  # noqa: F401
    except Exception:
        site = Path("/opt/ros/jazzy/lib/python3.12/site-packages")
        if site.exists() and str(site) not in sys.path:
            sys.path.insert(0, str(site))


@pytest.fixture
def tmp_bag_root(tmp_path: Path) -> Path:
    root = tmp_path / "bags"
    root.mkdir()
    _make_bag(root / "demo", seconds=1.0)
    return root


@pytest.fixture
def settings(tmp_path: Path, tmp_bag_root: Path) -> Settings:
    return Settings(
        bag_root=tmp_bag_root,
        checkpoint_dir=tmp_path / "checkpoints",
        ros_enabled=False,
        topic_prefix="/replay",
    )


@pytest.fixture
def manager(settings: Settings) -> SessionManager:
    mgr = SessionManager(settings)
    yield mgr
    mgr.shutdown_all()


@pytest.fixture
def index(tmp_bag_root: Path):
    return load_bag(tmp_bag_root / "demo")


# Helpers exported for tests.
def wait_until(predicate, timeout: float = 3.0, interval: float = 0.005) -> bool:
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if predicate():
            return True
        time.sleep(interval)
    return predicate()


@pytest.fixture
def helpers():
    class H:
        wait_until = staticmethod(wait_until)

    return H()
