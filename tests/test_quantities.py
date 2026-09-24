from decimal import Decimal

import pytest

from app.quantities import QuantityError, parse_quantity


def test_cpu_cores_and_millicores():
    assert parse_quantity("cpu", "1") == Decimal("1")
    assert parse_quantity("cpu", "500m") == Decimal("0.5")
    assert parse_quantity("cpu", 2) == Decimal(2)
    assert parse_quantity("cpu", 0.25) == Decimal("0.25")


def test_memory_suffixes():
    assert parse_quantity("memory", "1Ki") == Decimal(1024)
    assert parse_quantity("memory", "1Mi") == Decimal(1024**2)
    assert parse_quantity("memory", "2Gi") == Decimal(2 * 1024**3)
    assert parse_quantity("memory", "1k") == Decimal(1000)
    assert parse_quantity("memory", "1M") == Decimal(1_000_000)
    assert parse_quantity("memory", "1024") == Decimal(1024)


def test_extended_resources_are_integral():
    assert parse_quantity("nvidia.com/gpu", 2) == Decimal(2)
    assert parse_quantity("nvidia.com/gpu", "4") == Decimal(4)
    with pytest.raises(QuantityError):
        parse_quantity("nvidia.com/gpu", "0.5")


def test_invalid_quantities():
    with pytest.raises(QuantityError):
        parse_quantity("cpu", "abc")
    with pytest.raises(QuantityError):
        parse_quantity("memory", "1Xi")
    with pytest.raises(QuantityError):
        parse_quantity("cpu", True)
