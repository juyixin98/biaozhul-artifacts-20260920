"""pytest 共享夹具。"""

import math
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from app.kinematics import LinkParams  # noqa: E402


@pytest.fixture
def arm() -> LinkParams:
    return LinkParams(l1=1.0, l2=1.0)


@pytest.fixture
def uneven_arm() -> LinkParams:
    return LinkParams(l1=1.2, l2=0.8)


@pytest.fixture
def elbow_up_only_arm() -> LinkParams:
    """只允许 q2 <= -0.2：肘下支必然违规。"""
    return LinkParams(
        l1=1.0,
        l2=1.0,
        theta1_min=-math.pi,
        theta1_max=math.pi,
        theta2_min=-math.pi,
        theta2_max=-0.2,
    )
