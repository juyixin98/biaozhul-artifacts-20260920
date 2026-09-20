"""IPv4 pool helpers.

Allocation *excludes* the network address, broadcast address and any address
the operator marks reserved (e.g. gateways, VIPs).
"""

from __future__ import annotations

import ipaddress
from collections.abc import Iterable

MIN_PREFIX = 24
MAX_PREFIX = 30


def parse_cidr(cidr: str) -> ipaddress.IPv4Network:
    try:
        net = ipaddress.ip_network(cidr, strict=True)
    except ValueError as exc:
        raise ValueError(f"invalid CIDR '{cidr}': {exc}") from exc
    if not isinstance(net, ipaddress.IPv4Network):
        raise ValueError("only IPv4 networks are supported")
    if not (MIN_PREFIX <= net.prefixlen <= MAX_PREFIX):
        raise ValueError(f"prefix length must be between /{MIN_PREFIX} and /{MAX_PREFIX}")
    return net


def expand_pool(
    cidr: str, reserved: Iterable[str] = ()
) -> list[tuple[str, str, bool]]:
    """Return (ip, kind, reserved) for every address in the pool.

    kind is "network", "broadcast" or "usable". Reserved addresses must be
    usable host addresses; the network/broadcast addresses are non-allocable
    by definition.
    """
    net = parse_cidr(cidr)
    reserved_set: set[ipaddress.IPv4Address] = set()
    for raw in reserved:
        addr = ipaddress.ip_address(raw)
        if not isinstance(addr, ipaddress.IPv4Address):
            raise ValueError(f"reserved address must be IPv4: {raw}")
        if addr not in net:
            raise ValueError(f"reserved address {raw} is outside {cidr}")
        if addr == net.network_address or addr == net.broadcast_address:
            raise ValueError(f"cannot reserve network/broadcast address {raw}")
        reserved_set.add(addr)

    rows: list[tuple[str, str, bool]] = []
    hosts = set(net.hosts())
    for addr in net:
        if addr == net.network_address:
            kind = "network"
            is_reserved = False
        elif addr == net.broadcast_address:
            kind = "broadcast"
            is_reserved = False
        elif addr in hosts:
            kind = "usable"
            is_reserved = addr in reserved_set
        else:  # pragma: no cover - every v4 address falls into one of the above
            kind = "usable"
            is_reserved = addr in reserved_set
        rows.append((str(addr), kind, is_reserved))
    return rows


def usable_count(cidr: str, reserved: Iterable[str] = ()) -> int:
    return sum(1 for _, kind, is_reserved in expand_pool(cidr, reserved) if kind == "usable" and not is_reserved)
