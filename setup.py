from setuptools import setup

package_name = "time_alignment"

setup(
    name=package_name,
    version="1.0.0",
    packages=[package_name],
    data_files=[
        ("share/ament_index/resource_index/packages",
         ["resource/" + package_name]),
        ("share/" + package_name, ["package.xml"]),
    ],
    install_requires=["setuptools"],
    zip_safe=True,
    maintainer="time-alignment team",
    maintainer_email="dev@example.com",
    description="Camera/IMU event-time alignment with SQLite HMAC evidence",
    license="MIT",
    entry_points={
        "console_scripts": [
            "aligner_node = time_alignment.node:main",
            "synthetic_publisher = time_alignment.synthetic:main",
        ],
    },
)
