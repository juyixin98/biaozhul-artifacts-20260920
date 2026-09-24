"""Kubernetes resource quantity parsing.

Supported syntax (the explicit subset):
- cpu: cores as number/string ("1", "0.5", 1.5) or millicores ("500m").
- memory: integer bytes, or binary/Kubernetes suffixes Ki, Mi, Gi, Ti, Pi, Ei
  and decimal SI suffixes k, M, G, T, P, E.
- every other resource key (e.g. nvidia.com/gpu, pods): integer count.

All values are returned as :class:`decimal.Decimal` so comparisons are exact
(float millicores like 0.3 are never compared with binary-float error).
"""

from __future__ import annotations

from decimal import Decimal, InvalidOperation

_BINARY = {
    "Ki": 1024,
    "Mi": 1024**2,
    "Gi": 1024**3,
    "Ti": 1024**4,
    "Pi": 1024**5,
    "Ei": 1024**6,
}
_DECIMAL = {
    "k": 1000,
    "M": 1000**2,
    "G": 1000**3,
    "T": 1000**4,
    "P": 1000**5,
    "E": 1000**6,
}


class QuantityError(ValueError):
    """Raised when a resource quantity cannot be parsed."""


def _parse_cpu(raw: str) -> Decimal:
    if raw.endswith("m"):
        body = raw[:-1]
        try:
            return Decimal(body) / Decimal(1000)
        except InvalidOperation:
            raise QuantityError(f"invalid cpu quantity {raw!r}")
    try:
        return Decimal(raw)
    except InvalidOperation:
        raise QuantityError(f"invalid cpu quantity {raw!r}")


def _parse_memory(raw: str) -> Decimal:
    # Check binary suffixes first: "1Ki" ends with both "Ki" and "i" (not "k"),
    # while "1k" is decimal; order matters.
    for suffix, multiplier in _BINARY.items():
        if raw.endswith(suffix):
            body = raw[: -len(suffix)]
            try:
                return Decimal(body) * Decimal(multiplier)
            except InvalidOperation:
                raise QuantityError(f"invalid memory quantity {raw!r}")
    for suffix, multiplier in _DECIMAL.items():
        if raw.endswith(suffix):
            body = raw[: -len(suffix)]
            try:
                return Decimal(body) * Decimal(multiplier)
            except InvalidOperation:
                raise QuantityError(f"invalid memory quantity {raw!r}")
    try:
        return Decimal(raw)
    except InvalidOperation:
        raise QuantityError(f"invalid memory quantity {raw!r}")


def _parse_count(raw: str, key: str) -> Decimal:
    try:
        value = Decimal(raw)
    except InvalidOperation:
        raise QuantityError(f"extended resource {key!r} must be an integer, got {raw!r}")
    if value != value.to_integral_value() or value < 0:
        raise QuantityError(f"extended resource {key!r} must be a non-negative integer, got {raw!r}")
    return value


def parse_quantity(key: str, value: object) -> Decimal:
    """Parse one resource value according to its resource key.

    ``key`` is the resource name (e.g. ``cpu``, ``memory``, ``nvidia.com/gpu``).
    """
    if isinstance(value, bool):
        raise QuantityError(f"resource {key!r} must be a number or string, got bool")
    if isinstance(value, int):
        return Decimal(value)
    if isinstance(value, float):
        return Decimal(str(value))
    if isinstance(value, str):
        raw = value.strip()
        if not raw:
            raise QuantityError(f"resource {key!r} is empty")
        if key == "cpu":
            return _parse_cpu(raw)
        if key == "memory":
            return _parse_memory(raw)
        return _parse_count(raw, key)
    raise QuantityError(f"resource {key!r} has unsupported type {type(value).__name__}")


def parse_resource_map(data: dict[str, object], where: str) -> dict[str, Decimal]:
    """Parse a ``{resource_name: quantity}`` mapping, raising on bad entries."""
    result: dict[str, Decimal] = {}
    for key, value in data.items():
        try:
            result[key] = parse_quantity(key, value)
        except QuantityError as exc:
            raise QuantityError(f"{where}: {exc}") from None
    return result
