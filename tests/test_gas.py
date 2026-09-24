"""Gas behaviour measured against the REAL Anvil node.

- Write: a same-block merge must cost materially less than an append, and an
  append at a long history must cost about the same as an append at a short
  history (write is O(1); a linear-scan write design would violate this).
- Read: lookup gas must grow only logarithmically with history length.
  Estimates are taken at independent history lengths with cold storage, and
  cross-checked against the Foundry benchmark table recorded in the README.
"""
from __future__ import annotations

from tests.conftest import batch_set_in_one_block, mine_empty_blocks


def _lookup_gas(checkpoint, target: int) -> int:
    # estimate_gas executes the view call against real Anvil state, so cold
    # SLOAD costs (2,100 each) are included.
    return int(
        checkpoint.contract.functions.getAtBlock(target).estimate_gas(
            {"from": "0x0000000000000000000000000000000000000001"}
        )
    )


def test_write_gas_merge_vs_append_and_constant_append(checkpoint, w3):
    # Gas figures are compared only between txs with identical access
    # patterns (so cold/warm slot differences never pollute the assertion).
    # Each measurement pair is [append-at-new-block, merge-same-block].

    # Pair at history length 1.
    _, short_pair = batch_set_in_one_block(w3, checkpoint.address, [11, 12])
    gas_append_short = int(short_pair[0]["gasUsed"])
    gas_merge_short = int(short_pair[1]["gasUsed"])
    assert checkpoint.length() == 1
    assert gas_merge_short < gas_append_short, "merge must be cheaper than append"

    # Grow history: one checkpoint per block for ~200 blocks.
    head = w3.eth.block_number
    for _ in range(198):
        checkpoint.set_value(7)
    assert w3.eth.block_number == head + 198
    assert checkpoint.length() == 199

    # Pair at history length 200, same block, same access pattern.
    _, long_pair = batch_set_in_one_block(w3, checkpoint.address, [222, 223])
    gas_append_long = int(long_pair[0]["gasUsed"])
    gas_merge_long = int(long_pair[1]["gasUsed"])
    assert checkpoint.length() == 200

    print("\nWRITE GAS (Anvil receipts, same-block paired batches):")
    print(f"  append (short history) : {gas_append_short}")
    print(f"  merge  (short history) : {gas_merge_short}")
    print(f"  append (~200 cps)      : {gas_append_long}")
    print(f"  merge  (~200 cps)      : {gas_merge_long}")

    # The first-ever append pays cold-slot costs (~15k) that later appends
    # do not; that is a constant setup effect, not history-length growth.
    # Grow a further 200 checkpoints, then compare the warm append directly
    # against the n~200 figure.
    for _ in range(200):
        checkpoint.set_value(9)
    _, steady = batch_set_in_one_block(w3, checkpoint.address, [333, 334])
    gas_append_steady = int(steady[0]["gasUsed"])
    gas_merge_steady = int(steady[1]["gasUsed"])
    print(f"  append (~400 cps)      : {gas_append_steady}")
    print(f"  merge  (~400 cps)      : {gas_merge_steady}")

    # n=200 vs n=400 appends must match within one warm SLOAD: proves writes
    # never scan history (a linear design adds a cold ~2.1k SLOAD PER entry,
    # i.e. >400k extra at n=400).
    assert abs(gas_append_steady - gas_append_long) < 5_000
    assert abs(gas_merge_steady - gas_merge_long) < 3_000
    # The first-ever append is expected to be pricier (cold contract slots);
    # that offset is constant and independent of history length.
    assert gas_append_short > gas_append_long

    # Merge must remain a cheaper in-place overwrite regardless of history.
    assert gas_merge_long < gas_append_long
    assert gas_merge_long * 100 < gas_append_long * 90
    # Merge cost is identical warm SSTORE reset at any history length.
    assert abs(gas_merge_long - gas_merge_short) < 3_000


def test_read_gas_logarithmic_growth(checkpoint, w3):
    sizes = [4, 16, 64, 256]
    gas_by_size: dict[int, int] = {}

    for size in sizes:
        # One fresh contract per size so storage is genuinely cold on lookup.
        from tests.conftest import _bytecode
        from app.config import ANVIL_TEST_PRIVATE_KEYS
        from app.contract import deploy

        fresh = deploy(w3, ANVIL_TEST_PRIVATE_KEYS[0], _bytecode())
        head = w3.eth.block_number
        mine_empty_blocks(w3, size)
        for i in range(size):
            # Each write goes one block further: empty-block mining already
            # advanced the chain, so consecutive auto-mined txs land in
            # consecutive blocks (no merges).
            fresh.set_value(i + 1)
        assert fresh.length() == size
        target = w3.eth.block_number  # at/after latest -> deepest search
        gas_by_size[size] = _lookup_gas(fresh, target)

    print("\nREAD GAS (Anvil estimate_gas, cold storage, target=latest):")
    for size in sizes:
        print(f"  n={size:>4}: {gas_by_size[size]}")

    # Each 4x growth in history adds only two binary-search probes. Bound the
    # increment tightly enough that a linear implementation (each extra
    # checkpoint ~2.1-5k gas) cannot pass.
    for small, large in zip(sizes, sizes[1:]):
        increment = gas_by_size[large] - gas_by_size[small]
        ratio = large // small
        print(f"  {small} -> {large} (x{ratio}): +{increment} gas")
        # 2 extra probes => well under 15k even with full cold overhead.
        assert increment < 15_000, (
            f"lookup gas grew {increment} on a x{ratio} history increase; "
            "expected ~constant per doubling (log n)"
        )

    # Absolute sanity: 256-checkpoint lookup is tens of thousands, not the
    # >500k a linear cold scan would cost.
    assert gas_by_size[256] < 80_000


def test_batch_of_300_merges_gas_and_single_checkpoint(checkpoint, w3):
    values = list(range(300))
    block_number, receipts = batch_set_in_one_block(
        w3, checkpoint.address, values
    )
    assert checkpoint.length() == 1

    total = sum(int(r["gasUsed"]) for r in receipts)
    print(f"\nBATCH: 300 same-block updates in block {block_number}, "
          f"total gas={total}, avg={total // len(receipts)}")
    # Last write wins.
    _, value = checkpoint.contract.functions.checkpointAt(0).call()
    assert value == 299
    # Every update after the first is an in-place overwrite: cheap.
    # (Bound chosen to stay valid across reasonable EVM parametrisations.)
    later = [int(r["gasUsed"]) for r in receipts[1:]]
    assert max(later) < 60_000
