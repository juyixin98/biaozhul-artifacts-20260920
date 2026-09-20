#!/usr/bin/env python3
"""
Pytest configuration shared by all test modules.
"""

import pytest

from app.services import scheduling as sched
from app.models import Task


def test_pytest_setup():
    """Verify pytest works."""
    unit = factory.unit
    tz = "UTC"


# Unit tests with a fake clock, expiry sweep, and constraint validation
