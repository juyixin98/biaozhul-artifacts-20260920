"""共享 URDF 片段与边界矩阵夹具。"""

from pathlib import Path

import pytest

EXAMPLES = Path(__file__).resolve().parent.parent / "examples"


def read_example(name: str) -> bytes:
    return (EXAMPLES / name).read_bytes()


def make_urdf(
    links: str,
    joints: str = "",
    name: str = "test_bot",
    header: str = '<?xml version="1.0"?>',
) -> bytes:
    return f"{header}\n<robot name=\"{name}\">\n{links}\n{joints}\n</robot>\n".encode()


def link_xml(name: str, inertial: str = "") -> str:
    return f'  <link name="{name}">{inertial}</link>'


def inertial_xml(
    mass: str = "1.0",
    ixx: str = "0.01",
    iyy: str = "0.01",
    izz: str = "0.01",
    ixy: str = "0.0",
    ixz: str = "0.0",
    iyz: str = "0.0",
) -> str:
    return (
        "<inertial>"
        f'<mass value="{mass}"/>'
        f'<inertia ixx="{ixx}" iyy="{iyy}" izz="{izz}" '
        f'ixy="{ixy}" ixz="{ixz}" iyz="{iyz}"/>'
        "</inertial>"
    )


def joint_xml(
    name: str,
    jtype: str,
    parent: str,
    child: str,
    extra: str = "",
) -> str:
    return (
        f'  <joint name="{name}" type="{jtype}">'
        f'<parent link="{parent}"/><child link="{child}"/>{extra}</joint>'
    )


GOOD_LIMIT = '<limit lower="-1.0" upper="1.0" effort="10.0" velocity="1.0"/>'
GOOD_AXIS = '<axis xyz="0 0 1"/>'


@pytest.fixture()
def two_link_arm() -> bytes:
    return read_example("two_link_arm.urdf")


@pytest.fixture()
def fixed_sensor_rig() -> bytes:
    return read_example("fixed_sensor_rig.urdf")
