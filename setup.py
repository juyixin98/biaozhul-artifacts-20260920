"""ament_python ROS2 package for hardware-free camera/IMU time alignment."""

from setuptools import find_packages, setup

package_name = "time_alignment"

setup(
    name=package_name,
    version="0.1.0",
    packages=find_packages(where="src"),
    package_dir={"": "src"},
    data_files=[
        ("share/ament_index/resource_index/packages",
         ["resource/" + package_name]),
        ("share/" + package_name, ["package.xml"]),
    ],
    install_requires=[],  # rclpy / sensor_msgs are provided by ROS 2
    zip_safe=True,
    maintainer="time-alignment",
    maintainer_email="example@example.com",
    description="Camera/IMU event-time alignment node with SQLite hash-chain "
                "evidence and a hardware-free synthetic publisher.",
    license="Apache-2.0",
    entry_points={
        "console_scripts": [
            # Offline CLI (works without ROS if PYTHONPATH points at src)
            "align-tool = time_alignment.cli:main",
            # ROS 2 nodes
            "aligner = time_alignment_nodes.main:main_aligner",
            "synthetic_publisher = time_alignment_nodes.main:main_publisher",
        ],
    },
)
