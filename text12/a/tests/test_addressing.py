"""地址池分配的纯单元测试：网络/广播/保留地址排除、多池 first-fit。"""
from __future__ import annotations

import pytest

from app.addressing import choose_address, count_assignable, pool_hosts, pools_overlap


def test_24_excludes_network_and_broadcast():
    hosts = pool_hosts("10.0.0.0/24", 0, 0)
    assert len(hosts) == 254
    assert str(hosts[0]) == "10.0.0.1"
    assert str(hosts[-1]) == "10.0.0.254"


def test_30_excludes_network_and_broadcast():
    hosts = pool_hosts("192.168.1.0/30", 0, 0)
    assert [str(h) for h in hosts] == ["192.168.1.1", "192.168.1.2"]


def test_reserved_first_and_last_are_excluded():
    hosts = pool_hosts("10.0.0.0/29", reserved_first=1, reserved_last=1)
    # /29: 网络 .0 广播 .7；保留 .1 和 .6；可分配 .2-.5
    assert [str(h) for h in hosts] == ["10.0.0.2", "10.0.0.3", "10.0.0.4", "10.0.0.5"]


def test_reserved_must_not_exceed_hosts():
    with pytest.raises(ValueError):
        pool_hosts("10.0.0.0/30", reserved_first=2, reserved_last=2)


def test_31_point_to_point():
    hosts = pool_hosts("10.0.0.0/31", 0, 0)  # RFC 3021：两个地址都可用
    assert len(hosts) == 2


def test_choose_address_first_fit_skips_occupied():
    pools = [("p1", "10.0.0.0/30", 0, 0)]  # .1 .2
    assert choose_address(pools, set()).ip == "10.0.0.1"
    assert choose_address(pools, {"10.0.0.1"}).ip == "10.0.0.2"
    assert choose_address(pools, {"10.0.0.1", "10.0.0.2"}) is None


def test_choose_address_spills_to_next_pool():
    pools = [("p1", "10.0.0.0/30", 0, 0), ("p2", "10.0.0.4/30", 0, 0)]
    choice = choose_address(pools, {"10.0.0.1", "10.0.0.2"})
    assert choice.ip == "10.0.0.5"
    assert choice.pool_id == "p2"


def test_network_and_broadcast_never_chosen_when_full():
    pools = [("p1", "10.0.0.0/29", 0, 0)]  # .1-.6 可分配
    chosen = set()
    for _ in range(6):
        c = choose_address(pools, chosen)
        assert c is not None
        chosen.add(c.ip)
    assert chosen == {f"10.0.0.{i}" for i in range(1, 7)}
    assert choose_address(pools, chosen) is None  # .0/.7 不会被拿来凑数


def test_count_assignable():
    pools = [("p1", "10.0.0.0/24", 5, 3), ("p2", "10.0.0.0/30", 0, 0)]
    assert count_assignable(pools) == 246 + 2


def test_pools_overlap():
    assert pools_overlap("10.0.0.0/24", "10.0.0.128/25")
    assert not pools_overlap("10.0.0.0/24", "10.0.1.0/24")


@pytest.mark.parametrize("bad", ["10.0.0.5/24", "not-a-cidr", "10.0.0.0/33", "::1/128"])
def test_invalid_cidr_rejected(bad):
    with pytest.raises(ValueError):
        pool_hosts(bad, 0, 0)
