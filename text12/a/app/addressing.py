from __future__ import annotations

import ipaddress
from dataclasses import dataclass


@dataclass(frozen=True)
class AssignableAddress:
    ip: str
    pool_id: str
    cidr: str


def parse_network(cidr: str) -> ipaddress.IPv4Network:
    """解析 IPv4 CIDR，自动拒绝非规范写法（如 10.0.0.5/24）。"""
    net = ipaddress.IPv4Network(cidr, strict=True)
    if net.version != 4:
        raise ValueError("仅支持 IPv4 地址池")
    return net


def canonical_cidr(cidr: str) -> str:
    return str(parse_network(cidr))


def pool_hosts(cidr: str, reserved_first: int, reserved_last: int) -> list[ipaddress.IPv4Address]:
    """返回池中可分配主机地址，已排除网络地址、广播地址和保留地址。

    - ipaddress.IPv4Network.hosts() 自动排除网络地址与广播地址（/31、/32 例外，
      按 RFC 3021 语义返回其地址）；
    - reserved_first / reserved_last 从可用主机序列首尾再扣除保留地址。
    """
    net = parse_network(cidr)
    hosts = list(net.hosts())
    if reserved_first < 0 or reserved_last < 0:
        raise ValueError("保留地址数量不能为负")
    if reserved_first + reserved_last > len(hosts):
        raise ValueError("保留地址数量超过池中可用主机数")
    if reserved_last:
        return hosts[reserved_first : len(hosts) - reserved_last]
    return hosts[reserved_first:]


def pools_overlap(a_cidr: str, b_cidr: str) -> bool:
    a = parse_network(a_cidr)
    b = parse_network(b_cidr)
    return a.overlaps(b)


def choose_address(
    pools: list[tuple[str, str, int, int]],
    occupied: set[str],
) -> AssignableAddress | None:
    """在接入点的地址池中按顺序做 first-fit 分配。

    pools: [(pool_id, cidr, reserved_first, reserved_last), ...]（按创建顺序）
    occupied: 该接入点当前已被活动租约占用的 IP 字符串集合
    """
    for pool_id, cidr, reserved_first, reserved_last in pools:
        for ip in pool_hosts(cidr, reserved_first, reserved_last):
            ip_s = str(ip)
            if ip_s not in occupied:
                return AssignableAddress(ip=ip_s, pool_id=str(pool_id), cidr=cidr)
    return None


def count_assignable(pools: list[tuple[str, str, int, int]]) -> int:
    return len(
        [
            ip
            for _, cidr, rf, rl in pools
            for ip in pool_hosts(cidr, rf, rl)
        ]
    )
