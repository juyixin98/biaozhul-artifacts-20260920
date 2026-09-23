"""Shared pytest fixtures and a high-precision assertion helper."""

from __future__ import annotations

import decimal
from decimal import Decimal as D

import pytest

from app.core import config
from app.core.precision import high_precision


@pytest.fixture
def hp():
    """Run a test inside the 80-digit Decimal context."""
    with high_precision():
        yield


def dec(s) -> D:
    return D(str(s))


# Tight, human-readable comparison bounds.
REL_TIGHT = D("1E-50")
ABS_TIGHT = config.ROOT_ABS_TOL
