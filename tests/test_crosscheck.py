"""随机化交叉验证：链上二分查找 vs Python 线性扫描参考实现。"""
from __future__ import annotations

import random

import pytest

from app.reference import ReferenceCheckpoints
from tests.conftest import fire_set_value


def _wait_value(chain_client, target_block: int) -> int:
    return chain_client.contract.functions.valueAt(target_block).call()


@pytest.mark.parametrize("seed", [1, 7, 42, 123, 2024, 99991])
def test_randomized_matches_reference(chain_client, chain, seed: int) -> None:
    rng = random.Random(seed)
    ref = ReferenceCheckpoints()
    n_ops = 80

    for _ in range(n_ops):
        # delta=0 表示同块更新；否则推进 delta 个「交易块」间隔
        delta = rng.randint(0, 3)
        value = rng.randint(1, 1_000_000)

        if delta == 0 and ref.checkpoints:
            # 同块：交易进池不挖，和下一笔一起在同一新块打包
            chain.set_automine(False)
            try:
                fire_set_value(chain_client, value)
                chain.batch_mine_pending()
            finally:
                chain.set_automine(True)
        else:
            # Anvil：交易在「最新块 + 1」打包。
            # delta>=1 时先留 (delta-1) 个空隙块，再发交易。
            if delta - 1 > 0:
                chain.mine(delta - 1)
            receipt = chain_client.set_value(value)
            assert receipt["status"] == 1

        ref.set_value(chain.block, value)

    current = chain.block
    # 链上检查点数量与参考实现一致
    on_chain = chain_client.history()
    assert len(on_chain) == len(ref.checkpoints)
    for (rb, rv), point in zip(ref.checkpoints, on_chain):
        assert rb == point["blockNumber"]
        assert rv == point["value"]

    # 在多个目标块上核对：边界 + 随机点
    targets = {0, current, current - 1, current // 2}
    for _ in range(20):
        targets.add(rng.randint(0, current))
    for target in targets:
        expected = ref.value_at(target, current)
        actual = _wait_value(chain_client, target)
        assert actual == expected, (
            f"seed={seed} target={target}: 链上 {actual} != 参考 {expected}"
        )


def test_same_block_merge_raw(chain_client, chain) -> None:
    """关闭 automine，同一块内 60 次更新，只产生一个检查点。"""
    target_block = chain.block + 1
    chain.set_automine(False)
    try:
        for i in range(1, 61):
            fire_set_value(chain_client, 1000 + i)
        mined_block = chain.batch_mine_pending()
    finally:
        chain.set_automine(True)

    assert mined_block == target_block
    assert chain.block == target_block
    history = chain_client.history()
    assert len(history) == 1
    assert history[0]["blockNumber"] == target_block
    assert history[0]["value"] == 1060
    assert chain_client.contract.functions.valueAt(target_block).call() == 1060
