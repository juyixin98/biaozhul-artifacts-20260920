"""Explicit unit conversion.

All processing happens in SI units internally:

* acceleration: m/s^2
* angular rate: rad/s
* time: seconds
* temperature: degrees Celsius
"""

from __future__ import annotations

import math

ACCEL_SCALE_TO_MPS2: dict[str, float] = {
    "m/s^2": 1.0,
    "m/s2": 1.0,
    "g": 9.80665,  # standard gravity g_n
    "mg": 9.80665e-3,
}

GYRO_SCALE_TO_RADPS: dict[str, float] = {
    "rad/s": 1.0,
    "rad/s/": 1.0,
    "deg/s": math.pi / 180.0,
    "dps": math.pi / 180.0,
    "rad/hr": 1.0 / 3600.0,
    "deg/hr": math.pi / 180.0 / 3600.0,
}

TIME_SCALE_TO_SEC: dict[str, float] = {
    "s": 1.0,
    "ms": 1e-3,
    "us": 1e-6,
    "ns": 1e-9,
}


def convert_accel(values, unit: str):
    factor = ACCEL_SCALE_TO_MPS2[unit]
    return values * factor


def convert_gyro(values, unit: str):
    factor = GYRO_SCALE_TO_RADPS[unit]
    return values * factor


def convert_time(values, unit: str):
    factor = TIME_SCALE_TO_SEC[unit]
    return values * factor


def convert_temperature(values, unit: str):
    """Convert to degrees Celsius. Accepts an array-like or None."""
    if values is None:
        return None
    if unit == "c":
        return values
    if unit == "k":
        return values - 273.15
    if unit == "f":
        return (values - 32.0) * (5.0 / 9.0)
    raise ValueError(f"unsupported temperature unit: {unit!r}")
