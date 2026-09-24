import pytest

from app.quantity import QuantityParseError, cpu_to_milli, memory_to_bytes, parse_quantity


@pytest.mark.parametrize(
    "raw,expected_milli",
    [
        ("1", 1000),
        ("2", 2000),
        ("0.5", 500),
        ("500m", 500),
        ("1500m", 1500),
        ("1.5", 1500),
        ("2500u", 3),  # 2.5m 向上取整到 3m
        ("0", 0),
    ],
)
def test_cpu_quantities(raw, expected_milli):
    assert cpu_to_milli(raw) == expected_milli


@pytest.mark.parametrize(
    "raw,expected_bytes",
    [
        ("128Mi", 128 * 2**20),
        ("1Gi", 2**30),
        ("512M", 512 * 10**6),
        ("1G", 10**9),
        ("1024", 1024),
        ("1Ki", 1024),
        ("0", 0),
    ],
)
def test_memory_quantities(raw, expected_bytes):
    assert memory_to_bytes(raw) == expected_bytes


def test_parse_quantity_rejects_garbage():
    for bad in ["", "abc", "10x", "1e3", "mi", "--1"]:
        with pytest.raises(QuantityParseError):
            parse_quantity(bad)


def test_binary_and_decimal_suffixes_differ():
    assert parse_quantity("1Ki") == 1024
    assert parse_quantity("1k") == 1000
