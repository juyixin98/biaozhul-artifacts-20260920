"""IPv4 pool math: network/broadcast/reserved are never assignable."""
from __future__ import annotations

import ipaddress


def host_addresses(cidr: str) -> list[str]:
    """All assignable host addresses in a CIDR, excluding network & broadcast."""
    net = ipaddress.ip_network(cidr)
    if net.prefixlen == 31:
        # RFC 3021 point-to-point links: both addresses are usable.
        return [str(ip) for ip in net.hosts()]
    return [str(ip) for ip in net.hosts()]


def pool_stats(cidr: str, reserved_ips: list[str]) -> tuple[int, int, int]:
    """Return (total_hosts, reserved_count, usable).

    Reserved addresses that are not hosts (network/broadcast) do not count.
    """
    net = ipaddress.ip_network(cidr)
    hosts = set(host_addresses(cidr))
    reserved = {ip for ip in reserved_ips if ip in hosts}
    return len(hosts), len(reserved), len(hosts) - len(reserved)


def pick_free_address(cidr: str, reserved_ips: list[str], used: set[str]) -> str | None:
    """Pick the lowest free host address, skipping reserved and in-use."""
    excluded = set(reserved_ips) | used
    for ip in host_addresses(cidr):
        if ip not in excluded:
            return ip
    return None
